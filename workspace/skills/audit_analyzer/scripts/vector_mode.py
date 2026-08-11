"""
Режим: vector — семантический поиск по FAISS-индексу.

Конвертирует текстовый запрос в эмбеддинг (через Ollama API),
ищет ближайшие векторы в FAISS-индексе, возвращает результаты
с метаданными.

Поддерживает два источника данных:
  - FAISS-файлы (.faiss + _metadata.json) — по умолчанию
  - PostgreSQL/Greenplum таблица (mode_vector_db_table в skills.audit_analyzer, project.json)

Pipeline:
    1. Получить эмбеддинг запроса (Ollama /api/embed)
    2. Загрузить FAISS-индекс (из файлов или БД)
    3. Поиск top_k ближайших векторов
    4. Фильтрация по threshold (если задан)
    5. Сборка результатов с метаданными

Зависимости: faiss, numpy, httpx, psycopg2.
"""

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

import httpx

# Путь к workspace/utils — для импорта utils.db
_SCRIPTS_DIR = Path(__file__).resolve().parent
_WORKSPACE_DIR = _SCRIPTS_DIR.parents[3]
for p in [str(_SCRIPTS_DIR), str(_WORKSPACE_DIR), str(_SCRIPTS_DIR.parents[4])]:
    if p not in sys.path:
        sys.path.insert(0, p)

from skill_config import get_vector_index_path, get_embedding_config, get_vector_db_table

# ---------------------------------------------------------------------------
# Кеш индекса из БД в памяти — живёт до перезапуска агента
# ---------------------------------------------------------------------------
# Ключ: source (имя индекса, например "audits_index")
# Значение: (faiss.Index, metadata_dict)
# Сброс: только при вызове invalidate_cache() (build_vectors после добавления)
#        или перезапуске процесса.
_INDEX_CACHE: dict[str, tuple[Any, Optional[dict]]] = {}


def rebuild_and_store_index(source: str, db_table: str) -> None:
    """
    Перестроить FAISS-индекс для source из audit_vectors и сохранить в store.

    Вызывается из build_vectors.py после синхронизации.
    """
    idx, meta = _load_vectors_from_db(db_table, source=source)
    if idx is not None:
        _save_index_to_store(source, idx, meta)
        _INDEX_CACHE.pop(source, None)
        print(f"[vector] Индекс '{source}' перестроен и сохранён в store "
              f"({idx.ntotal} векторов)", file=__import__('sys').stderr)


def invalidate_cache(source: Optional[str] = None) -> None:
    """
    Сбросить кеш индекса.

    Вызывается из build_vectors.py после добавления/обновления данных.
    При следующем поиске индекс перезагрузится из БД.

    Args:
        source: Если указан — сбросить только этот индекс.
                Если None — сбросить весь кеш.
    """
    global _INDEX_CACHE
    if source:
        _INDEX_CACHE.pop(source, None)
    else:
        _INDEX_CACHE.clear()


def preload_indexes(db_table: Optional[str] = None) -> list[dict]:
    """
    Прогреть кеш индексов из БД (vector_index_store / audit_vectors) в память.

    Загружает все активные индексы при старте агента (gateway.py / cli_agent.py).
    Имена индексов берутся из oarb.vector_index_config (skills.audit_analyzer)
    плюс из фактически присутствующих в vector_index_store (когда конфиг пуст).

    Args:
        db_table: Таблица сырых векторов (schema.table).
                  По умолчанию из skills.audit_analyzer.mode_vector_db_table.

    Returns:
        Список загруженных индексов: [{"index_name", "vectors"}, ...].
    """
    from skill_config import get_vector_indexes
    from utils.db import fetch

    table = db_table or get_vector_db_table()
    if not table:
        return []

    names: dict[str, bool] = {}
    cfg = get_vector_indexes() or {}
    for name, c in cfg.items():
        names[name] = not (isinstance(c, dict) and c.get("enabled") is False)
    try:
        for r in fetch(f"SELECT DISTINCT source FROM {STORE_TABLE}"):
            names.setdefault(r["source"], True)
    except Exception:
        pass

    loaded = []
    for name, enabled in names.items():
        if not enabled:
            continue
        idx, _ = _load_index("", name, table)
        if idx is not None:
            loaded.append({"index_name": name, "vectors": idx.ntotal})
    return loaded


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------


def _get_embedding(text: str, retries: int = 3) -> Optional[list[float]]:
    """
    Получить эмбеддинг текста через Ollama /api/embed.

    Args:
        text: Входной текст для векторизации.
        retries: Количество попыток (по умолч. 3).

    Returns:
        Список float (эмбеддинг) или None при ошибке.
    """
    cfg = get_embedding_config()
    url = cfg.get("base_url")
    model = cfg.get("model", "mxbai-embed-large:latest")

    if not url:
        return None

    payload = {"model": model, "input": text}

    for attempt in range(1, retries + 1):
        try:
            with httpx.Client(timeout=60) as client:
                resp = client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()

            embeddings = data.get("embeddings")
            if embeddings and isinstance(embeddings, list) and len(embeddings) > 0:
                return embeddings[0]

            return None

        except Exception as e:
            if attempt < retries:
                delay = 2 ** attempt
                print(f"[vector] Retry {attempt}/{retries} через {delay}с: {e}", file=__import__('sys').stderr)
                time.sleep(delay)
            else:
                print(f"[vector] Ошибка эмбеддинга после {retries} попыток: {e}", file=__import__('sys').stderr)
                return None

    return None


# ---------------------------------------------------------------------------
# Загрузка индекса из файлов (FAISS .faiss + _metadata.json)
# ---------------------------------------------------------------------------


def _load_index_from_files(index_dir: str, index_name: str) -> tuple[Any, Optional[dict]]:
    """
    Загрузить FAISS-индекс и его метаданные из файлов.

    Ожидает файлы:
        {index_dir}/{index_name}.faiss         — FAISS-индекс
        {index_dir}/{index_name}_metadata.json  — метаданные (опционально)

    Если прямой faiss.read_index() падает (кириллица в пути),
    копирует файлы во временную директорию с ASCII-путём.

    Args:
        index_dir: Путь к директории с индексами.
        index_name: Имя индекса (без расширения).

    Returns:
        (faiss.Index, dict_metadata) или (None, None) при ошибке.
    """
    import faiss
    import shutil
    import tempfile

    index_path = os.path.join(index_dir, f"{index_name}.faiss")
    meta_path = os.path.join(index_dir, f"{index_name}_metadata.json")

    if not os.path.exists(index_path):
        return None, None

    meta = None
    if os.path.exists(meta_path):
        with open(meta_path, encoding="utf-8") as mf:
            meta = json.load(mf)

    try:
        idx = faiss.read_index(index_path)
        return idx, meta
    except RuntimeError:
        pass

    tmp_dir = os.path.join(tempfile.gettempdir(), "audit_analyzer_vectors")
    os.makedirs(tmp_dir, exist_ok=True)
    tmp_idx = os.path.join(tmp_dir, f"{index_name}.faiss")
    tmp_meta = os.path.join(tmp_dir, f"{index_name}_metadata.json")
    shutil.copy2(index_path, tmp_idx)
    if meta_path and os.path.exists(meta_path):
        shutil.copy2(meta_path, tmp_meta)

    try:
        idx = faiss.read_index(tmp_idx)
        return idx, meta
    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# Сохранение/загрузка сериализованного FAISS-индекса (BYTEA)
# ---------------------------------------------------------------------------

STORE_TABLE = "oarb.vector_index_store"


def _save_index_to_store(source: str, index, metadata: dict) -> None:
    """Сериализовать FAISS-индекс и сохранить в vector_index_store."""
    import faiss

    from utils.db import execute, fetch

    blob = bytes(faiss.serialize_index(index))
    meta_json = json.dumps(metadata, ensure_ascii=False, default=str)
    dim = index.d
    ntotal = index.ntotal

    # Ручной UPSERT — совместимо с GP 6.25 (нет ON CONFLICT)
    exists = fetch(
        f"SELECT 1 FROM {STORE_TABLE} WHERE source = %s", source
    )
    if exists:
        execute(
            f"UPDATE {STORE_TABLE} SET index_binary = %s, metadata = %s::jsonb, "
            f"dimension = %s, vector_count = %s, updated_at = NOW() "
            f"WHERE source = %s",
            blob, meta_json, dim, ntotal, source,
        )
    else:
        execute(
            f"INSERT INTO {STORE_TABLE} (source, index_binary, metadata, dimension, vector_count, updated_at) "
            f"VALUES (%s, %s, %s::jsonb, %s, %s, NOW())",
            source, blob, meta_json, dim, ntotal,
        )


def _load_index_from_store(source: str) -> tuple[Any, Optional[dict]]:
    """
    Загрузить FAISS-индекс из vector_index_store (десериализация).

    Returns:
        (faiss.Index, dict_metadata) или (None, None).
    """
    import numpy as np
    import faiss

    from utils.db import fetch

    rows = fetch(
        f"SELECT index_binary, metadata FROM {STORE_TABLE} WHERE source = %s",
        source,
    )
    if not rows:
        return None, None

    row = rows[0]
    blob = row["index_binary"]
    meta = row.get("metadata") or {}

    if isinstance(meta, str):
        meta = json.loads(meta)

    try:
        if isinstance(blob, memoryview):
            blob = bytes(blob)
        blob_array = np.frombuffer(blob, dtype=np.uint8)
        idx = faiss.deserialize_index(blob_array)
        return idx, meta
    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# Загрузка индекса из БД (PostgreSQL)
# ---------------------------------------------------------------------------


def _load_vectors_from_db(
    table_name: str,
    source: Optional[str] = None,
) -> tuple[Any, Optional[dict]]:
    """
    Загрузить векторы из PostgreSQL/Greenplum таблицы,
    построить FAISS-индекс в памяти.

    Ожидает структуру таблицы:
        id SERIAL,
        source TEXT,
        content TEXT,
        search_text TEXT,
        "table" TEXT,
        pk_value INTEGER,
        chunk_index INT,      -- номер чанка (0-based)
        chunk_count INT,      -- всего чанков в документе
        row_data JSONB,       -- полная строка исходной таблицы
        embedding REAL[]

    Args:
        table_name: Полное имя таблицы (schema.table).
        source: Фильтр по колонке source (имя индекса).

    Returns:
        (faiss.Index, dict_metadata) или (None, None) при ошибке.
    """
    import numpy as np
    import faiss

    from utils.db import fetch

    where = " WHERE source = %s" if source else ""
    params = [source] if source else []

    sql = (
        f'SELECT id, source, content, search_text, "table", pk_value, '
        f'chunk_index, chunk_count, row_data, embedding '
        f'FROM {table_name}{where} ORDER BY id'
    )

    try:
        rows = fetch(sql, *params)
    except Exception:
        return None, None

    if not rows:
        return None, None

    dimension = len(rows[0]["embedding"])
    vectors = np.zeros((len(rows), dimension), dtype=np.float32)
    metadata: dict = {"metadata": {}}

    for i, row in enumerate(rows):
        emb = row["embedding"]
        if isinstance(emb, (list, tuple)):
            vectors[i] = np.array(emb, dtype=np.float32)
        else:
            return None, None

        row_data = row.get("row_data")
        if isinstance(row_data, str):
            try:
                row_data = json.loads(row_data)
            except (json.JSONDecodeError, TypeError):
                row_data = {}

        metadata["metadata"][str(i)] = {
            "content": row.get("content") or row.get("search_text") or "",
            "search_text": row.get("search_text") or "",
            "source": row.get("source") or source or "",
            "table": row.get("table") or "",
            "pk_value": row.get("pk_value") or i,
            "chunk_index": row.get("chunk_index", 0),
            "chunk_count": row.get("chunk_count", 1),
            "row": row_data or {},
        }

    index = faiss.IndexFlatIP(dimension)
    index.add(vectors)

    return index, metadata


def _load_index(
    index_dir: str,
    index_name: str,
    db_table: Optional[str] = None,
) -> tuple[Any, Optional[dict]]:
    """
    Загрузить индекс: из БД если настроено, иначе из файлов.

    Приоритет:
      1. _INDEX_CACHE (in-memory)
      2. vector_index_store (десериализация BYTEA)
      3. _load_vectors_from_db (перестроение из строк audit_vectors + сохранение в store)
      4. FAISS-файлы

    Args:
        index_dir: Путь к FAISS-файлам (используется если db_table не задан).
        index_name: Имя индекса (source в БД или имя .faiss файла).
        db_table: Имя таблицы в БД (schema.table). Если None — берётся из конфига.

    Returns:
        (faiss.Index, dict_metadata) или (None, None) при ошибке.
    """
    table = db_table or get_vector_db_table()
    if table:
        cached = _INDEX_CACHE.get(index_name)
        if cached is not None:
            return cached

        # 1. Пробуем десериализовать готовый индекс
        idx, meta = _load_index_from_store(index_name)
        if idx is not None:
            n_vectors = idx.ntotal
            print(f"[vector] Индекс '{index_name}' скачан из БД (vector_index_store): "
                  f"{n_vectors} векторов, кеширован в памяти",
                  file=__import__('sys').stderr)
            _INDEX_CACHE[index_name] = (idx, meta)
            return idx, meta

        # 2. Перестраиваем из строк audit_vectors (fallback) и сохраняем в store
        idx, meta = _load_vectors_from_db(table, source=index_name)
        if idx is not None:
            _save_index_to_store(index_name, idx, meta)
            n_vectors = idx.ntotal
            print(f"[vector] Индекс '{index_name}' перестроен из БД (audit_vectors): "
                  f"{n_vectors} векторов, кеширован в памяти и сохранён в store",
                  file=__import__('sys').stderr)
            _INDEX_CACHE[index_name] = (idx, meta)
            return idx, meta

    return _load_index_from_files(index_dir, index_name)


# ---------------------------------------------------------------------------
# Основной поиск
# ---------------------------------------------------------------------------


def run(
    query: str,
    index_name: str,
    index_path: Optional[str] = None,
    top_k: int = 5,
    threshold: Optional[float] = None,
    db_table: Optional[str] = None,
) -> dict:
    """
    Семантический поиск по FAISS-индексу.

    Источник данных: БД (если задан mode_vector_db_table) или FAISS-файлы.

    Args:
        query: Запрос на естественном языке.
        index_name: Имя индекса. Для БД — значение колонки source.
                    Для файлов — имя .faiss файла (без расширения).
        index_path: Путь к директории с FAISS-файлами (из конфига по умолчанию).
        top_k: Максимум результатов (по умолчанию 5). Игнорируется при threshold.
        threshold: Минимальный порог схожести (0.0–1.0).
                   Если задан, выбираются ВСЕ документы выше порога.
        db_table: Имя таблицы в БД (schema.table). Переопределяет конфиг.

    Returns:
        dict с полями:
            status: "success" | "error"
            data:
                results: список найденных документов
                    [{content, score, source, table, pk_value}, ...]
                count: количество результатов
                (или message: сообщение если не найдено)
    """
    from pathlib import Path

    table = db_table or get_vector_db_table()
    idx_path = str(Path(index_path or get_vector_index_path()).resolve())

    try:
        import numpy as np
        import faiss
    except ImportError:
        return {
            "status": "error",
            "data": {
                "message": "Не установлены зависимости: faiss и numpy. Установите: pip install faiss-cpu numpy",
            },
        }

    idx, meta = _load_index(idx_path, index_name, db_table=table)
    if idx is None:
        if table:
            msg = (
                f"Индекс '{index_name}' не найден в таблице {table}"
            )
        else:
            msg = f"Индекс '{index_name}' не найден в {idx_path}"
        return {
            "status": "error",
            "data": {"message": msg},
        }

    embedding = _get_embedding(query)
    if embedding is None:
        return {
            "status": "error",
            "data": {"message": "Не удалось получить эмбеддинг запроса."},
        }

    query_vec = np.array([embedding], dtype=np.float32)
    n = idx.ntotal if threshold is not None else min(top_k, idx.ntotal)
    scores, ids = idx.search(query_vec, n)

    meta_items = (meta or {}).get("metadata", {})
    raw: list[dict] = []
    for score, doc_id in zip(scores[0], ids[0]):
        if doc_id < 0:
            continue
        if threshold is not None and score < threshold:
            continue
        item = meta_items.get(str(doc_id), {})
        chunk_idx = item.get("chunk_index", 0)
        chunk_total = item.get("chunk_count", 1)
        pk = item.get("pk_value", int(doc_id))
        tbl = item.get("table", "")
        src = item.get("source", index_name)
        content = item.get("content", item.get("search_text", ""))
        raw.append({
            "content": content,
            "score": float(score),
            "source": src,
            "table": tbl,
            "pk_value": pk,
            "chunk_index": chunk_idx,
            "chunk_total": chunk_total,
            "chunk": f"{chunk_idx + 1}/{chunk_total}" if chunk_total > 1 else "",
            "row": item.get("row", {}),
        })

    # Группировка чанков: один документ = одно место в top_k
    # Ключ группы: (source, table, pk_value)
    doc_groups: dict[tuple[str, str, Any], dict] = {}
    for r in raw:
        key = (r["source"], r["table"], r["pk_value"])
        if key not in doc_groups or r["score"] > doc_groups[key]["score"]:
            entry = dict(r)
            entry["matched_chunks"] = 1
            doc_groups[key] = entry
        else:
            doc_groups[key]["matched_chunks"] += 1

    results = sorted(doc_groups.values(), key=lambda r: r["score"], reverse=True)
    if top_k and threshold is None:
        results = results[:top_k]

    for r in results:
        r.pop("chunk_index", None)
        r.pop("chunk_total", None)

    if not results:
        return {
            "status": "success",
            "data": {"message": "Документы не найдены", "results": [], "count": 0},
        }

    return {
        "status": "success",
        "data": {"results": results, "count": len(results)},
    }
