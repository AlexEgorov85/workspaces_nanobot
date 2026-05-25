"""Режим: vector — семантический поиск по FAISS-индексу.

Использует прямое HTTP-обращение к embedding-ендпоинту из конфига
(tools.db_analyzer.embedding.base_url / model).
Зависимости: faiss, numpy, aiohttp — опциональны.
"""

import json
import os
import sys
from typing import Any, Optional

import aiohttp

from .config import get_vector_index_path, get_embedding_config


async def _get_embedding(text: str) -> Optional[list[float]]:
    """Получить эмбеддинг через HTTP POST к embedding-ендпоинту."""
    cfg = get_embedding_config()
    url = cfg.get("base_url")
    model = cfg.get("model", "mxbai-embed-large:latest")

    if not url:
        print("Embedding error: base_url не задан в tools.db_analyzer.embedding", file=sys.stderr)
        return None

    payload = {"model": model, "input": text}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                resp.raise_for_status()
                data = await resp.json()
    except Exception as e:
        print(f"Embedding error: {e}", file=sys.stderr)
        return None

    embeddings = data.get("embeddings")
    if not embeddings or not isinstance(embeddings, list) or len(embeddings) == 0:
        print(f"Embedding error: пустой ответ embeddings", file=sys.stderr)
        return None

    return embeddings[0]


def _load_index(index_dir: str, index_name: str) -> tuple[Any, Optional[dict]]:
    """Загрузить один FAISS-индекс и его метаданные."""
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

    # faiss C++ может не прочитать путь с кириллицей — копируем в temp
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
    except Exception as e:
        print(f"Warning: Failed to load index '{index_name}': {e}", file=sys.stderr)
        return None, None


async def run(
    query: str,
    index_name: str,
    index_path: Optional[str] = None,
    top_k: int = 5,
    threshold: Optional[float] = None,
) -> dict:
    """Поиск похожих документов по одному векторному индексу.

    Args:
        query: Запрос на естественном языке.
        index_name: Имя индекса (без .faiss).
        index_path: Путь к директории с FAISS-индексами.
        top_k: Количество результатов (по умолчанию 5, игнорируется при threshold).
        threshold: Порог схожести (0.0–1.0). Если задан, top_k игнорируется.
    """
    from pathlib import Path

    idx_path = str(Path(index_path or get_vector_index_path()).resolve())

    if not os.path.isdir(idx_path):
        return {
            "status": "error",
            "data": {"message": f"Директория индекса не найдена: {idx_path}"},
        }

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

    idx, meta = _load_index(idx_path, index_name)
    if idx is None:
        return {
            "status": "error",
            "data": {"message": f"Индекс '{index_name}' не найден в {idx_path}"},
        }

    embedding = await _get_embedding(query)
    if embedding is None:
        return {
            "status": "error",
            "data": {"message": "Не удалось получить эмбеддинг запроса."},
        }

    query_vec = np.array([embedding], dtype=np.float32)
    n = idx.ntotal if threshold is not None else min(top_k, idx.ntotal)
    scores, ids = idx.search(query_vec, n)

    meta_items = (meta or {}).get("metadata", {})
    results: list[dict] = []
    for score, doc_id in zip(scores[0], ids[0]):
        if doc_id < 0:
            continue
        if threshold is not None and score < threshold:
            continue
        item = meta_items.get(str(doc_id), {})
        results.append({
            "content": item.get("content", item.get("search_text", "")),
            "score": float(score),
            "source": item.get("source", index_name),
            "table": item.get("table", ""),
            "pk_value": item.get("pk_value", int(doc_id)),
        })

    results.sort(key=lambda r: r["score"], reverse=True)
    if top_k and threshold is None:
        results = results[:top_k]

    if not results:
        return {
            "status": "success",
            "data": {"message": "Документы не найдены", "results": [], "count": 0},
        }

    return {
        "status": "success",
        "data": {"results": results, "count": len(results)},
    }
