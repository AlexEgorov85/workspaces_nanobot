"""
Вспомогательные функции слоя кэша, общие для реализации ``CacheProvider``.

Модуль **не** является реализацией интерфейса: она живёт в
``lib/services/duckdb_cache_store.py``. Здесь осталось то, что нужно и
реализации, и потребителям конфигурации:

  * чтение конфигурации эмбеддингов и векторных индексов
  * вычисление эмбеддинга запроса
  * подпись конфигурации индекса и её проверка
  * сохранение комментариев таблиц/колонок в DuckDB-кэш
  * каталог runtime-векторных индексов

Реализаций интерфейса в рантайме ровно одна; вторая (``PostgresDuckDbProvider``)
удалена как дубликат, расходившийся с основной по enforcement'у и диагностике.
Репликация PostgreSQL → кэш (``load_cache_from_postgres``,
``check_cache_stale``) тоже удалена: ею владеет sync-слой
(``PgDuckDbSyncService``), а не интерфейс файла кэша.

Тяжёлые зависимости (duckdb, psycopg2, faiss, numpy, httpx) — только
лениво, внутри функций.
импортируются лениво внутри методов, чтобы импорт модуля оставался лёгким
и gateway мог управлять жизненным циклом без побочных эффектов.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Literal

# Пути к проекту и workspace — чтобы `from utils.db import ...` работал
# независимо от рабочего каталога.
_ROOT = Path(__file__).resolve().parents[2]        # корень проекта


# Канонический список полей, по которым вычисляется signature индекса.
# Любое изменение этих параметров между сборками → индекс устарел.
_INDEX_SIGNATURE_FIELDS = (
    "src_table",
    "pk_column",
    "content_cols",
    "embedding_cols",
    "track_column",
    "embedding_model",
    "embedding_dimension",
    "chunk_size",
    "chunk_overlap",
    "metric",
)


# Параметры подключения к эмбеддер-сервису (Ollama /api/embed и совместимые).
# Захардкожены в теле ``get_embedding()`` (задача «embedding-параметры в код»);
# секция ``gateway.vector.embedding`` и ``EmbeddingSettings`` удалены.
# Токен берётся из переменной окружения OS ``EMBED_TOKEN`` (если не задана —
# запросы без Authorization).
_EMBED_BASE_URL = "http://localhost:11434/api/embed"
_EMBED_MODEL = "mxbai-embed-large:latest"
_EMBED_DIMENSION = 1024
_EMBED_TIMEOUT_SEC = 60.0
_EMBED_RETRIES = 3
_EMBED_TOKEN_ENV = "EMBED_TOKEN"

# Дефолтные chunk-параметры сборки индекса (fallback, когда в конфиге индекса
# ``gateway.vector.index.indexes.<name>`` не заданы chunk_size / chunk_overlap).
_DEFAULT_CHUNK_SIZE = 500
_DEFAULT_CHUNK_OVERLAP = 80


def compute_index_signature(cfg: dict[str, Any]) -> str:
    """SHA256-хеш канонической конфигурации индекса.

    Вход: dict, где ключи — поля из ``_INDEX_SIGNATURE_FIELDS`` (неполный
    допустим; отсутствующие трактуются как ``""``). Выход: 64-char hex.

    Детерминирована: одинаковый вход → одинаковый выход на любой платформе.
    Используется для проверки ``verify_index_signature``: подпись текущего
    конфига сравнивается с подписью, сохранённой в metadata индекса
    (в штатном пути metadata строится в памяти, подпись вычисляется inline).
    """
    parts: list[str] = []
    for key in _INDEX_SIGNATURE_FIELDS:
        val = cfg.get(key)
        if isinstance(val, (list, tuple)):
            val = ",".join(str(v) for v in val)
        parts.append(f"{key}={val if val is not None else ''}")
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def verify_index_signature(
    stored_meta: dict[str, Any] | None,
    current_cfg: dict[str, Any],
) -> Literal["CURRENT", "STALE", "INVALID"]:
    """Сравнить signature в сохранённом metadata с текущим конфигом.

    После change ``remove-vector-index-store`` persisted metadata с
    signature больше не существует (FAISS-индекс собирается в памяти
    из DuckDB-снапшота). Стандартный путь — провайдер передаёт
    ``stored_meta=None`` или ``{}`` (нет persisted-signature), что
    трактуется как **CURRENT**: сигнатура вычисляется inline и
    гарантированно совпадает с текущим конфигом.

    Returns:
        ``CURRENT`` — ``stored_meta`` пуст/None (новый путь без persisted
            signature) ИЛИ signature в нём совпадает с текущим конфигом.
        ``STALE``  — signature присутствует и не совпадает с текущим
            (legacy-путь: изменилась модель эмбеддингов, chunk параметры,
            columns).
        ``INVALID`` — stored_meta есть, signature присутствует, но
            повреждена (не hex / не sha256 длиной 64).
    """
    if stored_meta is None:
        return "CURRENT"
    stored_sig = stored_meta.get("signature")
    if not stored_sig:
        return "CURRENT"
    if not isinstance(stored_sig, str) or len(stored_sig) != 64:
        return "INVALID"
    current_sig = compute_index_signature(current_cfg)
    return "CURRENT" if stored_sig == current_sig else "STALE"


def list_runtime_vector_indexes(
    store_table: str | None = None,
    *,
    fetch_fn=None,
) -> list[dict[str, Any]]:
    """Прочитать runtime-артефакты vector-индексов из DuckDB-снапшота.

    После change ``remove-vector-index-store`` persisted FAISS-кеш
    (``agent_vector_index_store``) удалён. Runtime-состояние индексов
    — это просто набор ``source`` (index_name), присутствующих в
    таблице сырых эмбеддингов (``gateway.vector.index.storage_table``,
    синхронизированной в DuckDB через ``PgDuckDbSyncService``).

    Возвращает список dict'ов с полями:
      ``source``         — имя индекса (= ``gateway.vector.index.indexes.<name>``)
      ``dimension``      — размерность (из DuckDB-схемы storage_table)
      ``vector_count``   — количество чанков в индексе (DuckDB COUNT(*))
      ``updated_at``     — ``None`` (нет persisted-метаданных с timestamp;
                            обновление отслеживается по ``synced_at`` в
                            ``storage_table`` если нужно — caller'ы могут
                            читать напрямую)

    Не вычисляет signature_status (это делает вызывающий через
    :func:`verify_index_signature`).

    **Источник данных обязателен.** Функция НЕ открывает файл кэша сама и
    НЕ разрешает путь к нему: единственный доступ — уже полученный
    ``CacheProvider`` (или его соединение) через ``fetch_fn``. Раньше
    здесь был собственный ``duckdb.connect`` плюс self-resolve
    ``gateway.cache.local_path``, из-за чего: относительный ``local_path``
    ронял ``ImportError`` (несуществующий ``_WORKSPACE_ROOT``) и молча
    превращался в пустой каталог индексов, а ``local_path`` трактовался
    как файл вместо каталога.

    ``[]`` означает «индексов нет», а не «кэш недоступен»: недоступность
    MUST подниматься вызывающим (см. ``CacheBusyError``), а не глотаться
    здесь.
    """
    from config import SETTINGS

    if store_table is None:
        idx = ((SETTINGS.get("gateway") or {}).get("vector") or {}).get("index") or {}
        store_table = idx.get("storage_table") or ""

    if not store_table:
        return []

    schema, name = (
        store_table.split(".", 1)
        if "." in store_table else ("", store_table)
    )
    full = f'"{schema}"."{name}"' if schema else f'"{name}"'

    if fetch_fn is None:
        raise ValueError(
            "list_runtime_vector_indexes() требует fetch_fn: функция не "
            "открывает файл кэша самостоятельно. Передайте провайдера "
            "(или его соединение), полученный из open_cache_provider()."
        )
    conn = fetch_fn

    try:
        rows = conn.execute(
            f"SELECT source, COUNT(*) AS vector_count "
            f"FROM {full} GROUP BY source ORDER BY source",
        ).fetchall()
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning(
            "list_runtime_vector_indexes(%s) failed: %s", store_table, exc
        )
        return []
    finally:
        if fetch_fn is None:
            try:
                conn.close()
            except Exception:
                pass

    out: list[dict[str, Any]] = []
    for row in rows:
        out.append({
            "source": row[0] if hasattr(row, "__getitem__") else row.get("source"),
            "dimension": None,
            "vector_count": (
                row[1] if hasattr(row, "__getitem__") else row.get("vector_count")
            ),
            "updated_at": None,
            "metric": None,
            "signature": None,
            "metadata": {},
        })
    return out


_WORKSPACE = _ROOT / "workspace"
for _p in (str(_ROOT), str(_WORKSPACE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from lib.services.cache_provider import CacheProvider, SearchResult  # noqa: E402

# Внутренняя таблица метаданных схемы (комментарии таблиц/колонок, PG-типы).
# Та же структура, что в cache_store, но в файле SQL-кэша навыка.
_META_SCHEMA = "__nanobot_meta"
_META_TABLE = "__schema_meta"


# =============================================================================
# СЛУЖЕБНЫЕ МОДУЛЬНЫЕ ФУНКЦИИ (используются провайдером и клиентами: gateway, навык)
# =============================================================================


def get_embedding(text: str) -> list[float] | None:
    """Единая точка получения эмбеддинга текста через Ollama /api/embed.

    Параметры подключения (``base_url`` / ``model`` / ``timeout_sec`` /
    ``retries``) захардкожены модульными константами
    (``_EMBED_BASE_URL`` / ``_EMBED_MODEL`` / ``_EMBED_TIMEOUT_SEC`` /
    ``_EMBED_RETRIES``). ``auth_token`` (bearer) читается из переменной
    окружения OS ``EMBED_TOKEN``; если не задана — запрос без
    ``Authorization`` (не ломает локальный Ollama без токена).

    Это generic инфраструктурный слой — ``lib/`` не зависит от конкретного
    навыка (TARGET §4, §22.9).
    """
    base_url = _EMBED_BASE_URL
    model = _EMBED_MODEL
    timeout_sec = _EMBED_TIMEOUT_SEC
    retries = _EMBED_RETRIES
    auth_token = os.environ.get(_EMBED_TOKEN_ENV, "").strip()

    def _embed() -> list[float] | None:
        import httpx

        payload = {"model": model, "input": text}
        headers = {}
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
        with httpx.Client(timeout=timeout_sec) as client:
            resp = client.post(base_url, json=payload, headers=headers or None)
            resp.raise_for_status()
            data = resp.json()
        embeddings = data.get("embeddings")
        if embeddings and isinstance(embeddings, list) and embeddings:
            return embeddings[0]
        return None

    from lib.utils.retry import retry_on_exception

    try:
        return retry_on_exception(
            _embed,
            exceptions=(Exception,),
            max_retries=retries,
            base_delay=1.0,
            max_delay=16.0,
            label="embedding",
        )
    except Exception as e:
        print(f"[vector] Ошибка эмбеддинга после {retries} попыток: {e}",
              file=sys.stderr)
        return None


def read_embedding_config() -> dict[str, Any]:
    """Параметры эмбеддера — из захардкоженных констант.

    Секция ``gateway.vector.embedding`` удалена; параметры подключения
    прописаны в теле ``get_embedding()`` (``_EMBED_*``-константы). Эта
    функция — единая точка чтения тех же значений для signature-механики
    (``_read_current_index_config`` /
    ``DuckDbCacheStore._check_index_integrity``), чтобы build- и verify-стороны
    не расходились.
    """
    return {
        "base_url": _EMBED_BASE_URL,
        "model": _EMBED_MODEL,
        "dimension": _EMBED_DIMENSION,
        "http_timeout_sec": _EMBED_TIMEOUT_SEC,
        "auth_token": os.environ.get(_EMBED_TOKEN_ENV) or None,
    }


def read_embedding_defaults() -> dict[str, Any]:
    """Дефолтные chunk-параметры сборки (``_DEFAULT_CHUNK_*``).

    Единая точка чтения для build- и verify-сторон: ``tools/build_vectors.py``,
    ``_read_current_index_config``,
    и ``_check_index_integrity`` берут одни и те же значения, когда в конфиге
    индекса нет per-index chunk-параметров.
    """
    return {
        "chunk_size": _DEFAULT_CHUNK_SIZE,
        "chunk_overlap": _DEFAULT_CHUNK_OVERLAP,
    }


def read_vector_index_config(cfg: dict) -> dict[str, Any]:
    """Конфиг векторных индексов из ``project.json::gateway.vector.index.indexes``.

    Единственный источник декларации индексов (раньше был PG-реестр
    ``public.agent_vector_index_config``). ``cfg`` игнорируется (API-compat
    с существующими вызовами) — конфиг читается из глобального ``SETTINGS``.

    Возвращает pythonic-формат: ``{имя: {table, pk, source_table,
    content_columns, embedding_columns, track_column, chunk_size,
    chunk_overlap, metric, enabled}}``.
    """
    from config import SETTINGS

    idx = ((SETTINGS.get("gateway") or {}).get("vector") or {}).get("index") or {}
    indexes = idx.get("indexes") or {}
    if not isinstance(indexes, dict):
        return {}
    result: dict[str, Any] = {}
    for name, c in indexes.items():
        if not isinstance(c, dict):
            continue
        result[name] = {
            "table": c.get("table", ""),
            "pk": c.get("pk", ""),
            "source_table": c.get("source_table"),
            "content_columns": list(c.get("content_columns") or []),
            "embedding_columns": c.get("embedding_columns") or [],
            "track_column": c.get("track_column"),
            "chunk_size": c.get("chunk_size"),
            "chunk_overlap": c.get("chunk_overlap"),
            "metric": c.get("metric"),
            "enabled": c.get("enabled", True),
        }
    return result



def _capture_schema_meta(
    conn: Any,
    pg_conn: Any,
    schema_pairs: list[tuple],
) -> None:
    """
    Сохранить комментарии таблиц/колонок и исходные PG-типы в DuckDB-кэш.

    ``schema_pairs`` — список ``(schema, [table, ...])``. Для каждой таблицы
    из PostgreSQL снимаются ``COMMENT ON TABLE``/``COMMENT ON COLUMN`` и
    ``data_type`` из ``information_schema``, результат кладётся в
    ``__nanobot_meta.__schema_meta`` (строка с ``column_name = NULL`` — это
    комментарий таблицы).

    ``build_schema()`` (lib/utils/duckdb_query.py) подставляет эти комментарии
    в промпт при формировании описания схемы, а ``pg_type`` (точнее инференса
    DuckDB из CSV) использует вместо типов, выведенных ``read_csv_auto``.
    """
    conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{_META_SCHEMA}"')
    conn.execute(f'DROP TABLE IF EXISTS "{_META_SCHEMA}"."{_META_TABLE}"')
    conn.execute(
        f'CREATE TABLE "{_META_SCHEMA}"."{_META_TABLE}" ('
        "schema_name TEXT, table_name TEXT, column_name TEXT, "
        "comment TEXT, pg_type TEXT)"
    )

    insert_rows: list[tuple] = []
    for schema, table_list in schema_pairs:
        if not table_list:
            continue
        cur = pg_conn.cursor()
        try:
            cur.execute(
                """
                SELECT
                    c.table_name,
                    c.column_name,
                    c.data_type,
                    c.character_maximum_length,
                    pgd.description AS column_comment,
                    obj_description(pc.oid) AS table_comment
                FROM information_schema.columns c
                JOIN pg_class pc
                    ON pc.relname = c.table_name
                   AND pc.relnamespace = (SELECT oid FROM pg_namespace WHERE nspname = %s)
                LEFT JOIN pg_catalog.pg_description pgd
                    ON pgd.objsubid = c.ordinal_position
                   AND pgd.objoid = pc.oid
                WHERE c.table_schema = %s
                  AND c.table_name = ANY(%s)
                ORDER BY c.table_name, c.ordinal_position
                """,
                [schema, schema, table_list],
            )
            rows = cur.fetchall()
        except Exception as e:
            print(f"[LOAD] Не удалось снять схему-мета для {schema}: {e}",
                  file=sys.stderr)
            continue
        finally:
            cur.close()

        per_table: dict[str, tuple] = {}
        for row in rows:
            tbl, col, data_type, max_len, col_comment, table_comment = row
            if tbl not in per_table:
                per_table[tbl] = (table_comment, [])
            col_type = data_type
            if max_len and col_type in ("character varying", "character"):
                col_type = f"varchar({max_len})"
            per_table[tbl][1].append((col, col_type, col_comment))

        for tbl, (table_comment, cols) in per_table.items():
            if table_comment:
                insert_rows.append((schema, tbl, None, table_comment, None))
            for col, col_type, col_comment in cols:
                insert_rows.append((schema, tbl, col, col_comment, col_type))

    if insert_rows:
        conn.executemany(
            f'INSERT INTO "{_META_SCHEMA}"."{_META_TABLE}" '
            "(schema_name, table_name, column_name, comment, pg_type) "
            "VALUES (?, ?, ?, ?, ?)",
            insert_rows,
        )
