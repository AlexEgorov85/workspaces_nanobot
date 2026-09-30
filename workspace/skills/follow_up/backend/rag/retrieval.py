"""
Follow Up 2.0 — Hybrid Retrieval with Metadata Filtering.

Поиск: semantic (FAISS) + keyword (BM25) → RRF fusion → фильтрация по метаданным.
Метаданные берутся из SQLite (не из parquet).
"""
from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional

import faiss
import numpy as np

from backend.config import get_settings
from backend.indexing.embedder import embed_texts
from backend.indexing.index_builder import load_bm25, load_faiss
from backend.storage.database import get_db, Chunk, Document
from sqlalchemy.orm import joinedload

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────
# Tokenizer (копируем из index_builder для консистентности)
# ──────────────────────────────────────────────────────────────────

def _tokenize(text: str, lexicon_version: Optional[int] = None) -> List[str]:
    """Токенизация запроса — тем же кодом, что и корпус.

    Если пикл BM25 собран ДРУГОЙ версией лексикона, запрос токенизируется
    по-старому: стеммированный запрос против нестеммированного индекса не
    находит ничего, и это был бы молчаливый отказ поиска, а не ошибка.
    Пересборка индекса вернёт стеммер автоматически.
    """
    from backend.indexing.lexicon import LEXICON_VERSION, tokenize
    if lexicon_version is not None and lexicon_version != LEXICON_VERSION:
        return tokenize(text, stem_words=False)
    return tokenize(text)


# ──────────────────────────────────────────────────────────────────
# Single-mode search
# ──────────────────────────────────────────────────────────────────

def _semantic_search(query: str, top_k: int) -> List[int]:
    """Возвращает список faiss_id в порядке убывания релевантности."""
    index = load_faiss()
    q_emb = embed_texts([query], normalize=True)
    faiss.normalize_L2(q_emb)
    scores, ids = index.search(q_emb, min(top_k, index.ntotal))
    return [int(i) for i in ids[0] if i >= 0]


def _keyword_search(query: str, top_k: int) -> List[int]:
    """Возвращает список faiss_id в порядке убывания BM25 score."""
    data = load_bm25()
    bm25 = data["bm25"]
    # Позиция в BM25-корпусе → стабильный faiss_id (новые пиклы хранят ids;
    # старые — позиционные, для них позиция и есть id)
    ids = data.get("ids") or list(range(len(data["corpus"])))
    lex_v = data.get("lexicon_version")
    if lex_v is None:
        _warn_stale_lexicon()
    tokens = _tokenize(query, lex_v)
    scores = bm25.get_scores(tokens)
    top_idx = np.argsort(scores)[::-1][:top_k]
    return [int(ids[i]) for i in top_idx if scores[i] > 0]


_warned_lexicon = False


def _warn_stale_lexicon() -> None:
    """Индекс собран до появления стеммера — сказать один раз, но сказать."""
    global _warned_lexicon
    if _warned_lexicon:
        return
    _warned_lexicon = True
    logger.warning(
        "[Retrieval] Пикл BM25 собран без версии лексикона: запрос "
        "токенизируется по-старому, стеммер не работает. Пересоберите индекс "
        "(Admin → переиндексация), иначе «лимитов» и «лимит» остаются разными "
        "термами.")


# ──────────────────────────────────────────────────────────────────
# RRF Fusion
# ──────────────────────────────────────────────────────────────────

def _rrf(rank_lists: List[List[int]], k: int = 60) -> List[int]:
    scores: Dict[int, float] = {}
    for ranks in rank_lists:
        for rank, doc_id in enumerate(ranks):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    return [doc_id for doc_id, _ in sorted(scores.items(), key=lambda x: x[1], reverse=True)]


# ──────────────────────────────────────────────────────────────────
# Metadata enrichment
# ──────────────────────────────────────────────────────────────────

def _fetch_km_direct(km_filter: List[str], top_k: int) -> List[Dict]:
    """
    Прямой запрос чанков из БД по KM-фильтру, когда семантический поиск
    не нашёл нужные документы в top_k результатах.
    """
    with get_db() as db:
        chunks = (
            db.query(Chunk)
            .options(joinedload(Chunk.document))
            .join(Document)
            .filter(Document.check_id.in_(km_filter))
            .order_by(Chunk.chunk_index)
            .limit(top_k * 2)
            .all()
        )
    results = []
    for c in chunks:
        doc = c.document
        results.append({
            "faiss_id": c.faiss_id if c.faiss_id is not None else -1,
            "chunk_id": c.id,
            "chunk_index": c.chunk_index,
            "text": c.text,
            "header_path": c.header_path or "",
            "filename": doc.filename if doc else "",
            "check_id": doc.check_id if doc else "",
            "title": doc.filename if doc else "",
            "original_path": doc.original_path if doc else "",
        })
    return results


def _enrich_from_db(faiss_ids: List[int], km_filter: Optional[List[str]] = None) -> List[Dict]:
    """
    По списку faiss_id достаёт чанки из SQLite и обогащает метаданными документа.
    Опционально фильтрует по check_id.
    """
    if not faiss_ids:
        return []

    with get_db() as db:
        query = (
            db.query(Chunk)
            .options(joinedload(Chunk.document))
            .filter(Chunk.faiss_id.in_(faiss_ids))
        )
        if km_filter:
            # Фильтрация по КМ через JOIN
            query = query.join(Document).filter(
                Document.check_id.in_(km_filter)
            )
        chunks = query.all()

    # Словарь для быстрого доступа по faiss_id
    chunk_map: Dict[int, Chunk] = {c.faiss_id: c for c in chunks}

    results = []
    for fid in faiss_ids:
        c = chunk_map.get(fid)
        if c is None:
            continue
        doc = c.document

        # Применяем мягкий фильтр по КМ (если faiss_id не попал в JOIN)
        if km_filter and doc and doc.check_id not in km_filter:
            continue

        results.append({
            "faiss_id": fid,
            "chunk_id": c.id,
            "chunk_index": c.chunk_index,
            "text": c.text,
            "header_path": c.header_path or "",
            "filename": doc.filename if doc else "",
            "check_id": doc.check_id if doc else "",
            "title": doc.filename if doc else "",
            "original_path": doc.original_path if doc else "",
        })

    return results


# ──────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────

def retrieve(
    query: str,
    top_k: Optional[int] = None,
    km_filter: Optional[List[str]] = None,
    mode: str = "hybrid",
) -> List[Dict]:
    """
    Основная функция поиска.

    Args:
        query:     поисковый запрос
        top_k:     кол-во результатов
        km_filter: список КМ для фильтрации (None = без фильтра)
        mode:      'hybrid' | 'semantic' | 'keyword'
    """
    cfg = get_settings()
    k = top_k or cfg.top_k
    fetch_k = k * 3  # Берём больше для последующей фильтрации/reranking

    # 1. Пробуем семантический + BM25 поиск
    ids: List[int] = []
    try:
        if mode == "semantic":
            ids = _semantic_search(query, fetch_k)
        elif mode == "keyword":
            ids = _keyword_search(query, fetch_k)
        else:
            # Hybrid: пробуем оба, при отказе семантики — только BM25
            try:
                sem_ids = _semantic_search(query, fetch_k)
            except Exception as sem_err:
                logger.warning(
                    f"[Retrieval] Семантический поиск недоступен "
                    f"(модель не загружена?): {sem_err}. Использую только BM25."
                )
                sem_ids = []

            try:
                kw_ids = _keyword_search(query, fetch_k)
            except Exception as bm25_err:
                logger.warning(f"[Retrieval] BM25 поиск недоступен: {bm25_err}.")
                kw_ids = []

            if sem_ids and kw_ids:
                ids = _rrf([sem_ids, kw_ids], k=cfg.rrf_k)
            elif kw_ids:
                ids = kw_ids  # fallback: только BM25
            else:
                ids = sem_ids  # fallback: только FAISS

    except FileNotFoundError as e:
        logger.warning(f"[Retrieval] Индекс не найден: {e}")
    except Exception as e:
        logger.error(f"[Retrieval] Ошибка поиска: {e}")

    # 2. Обогащаем из БД
    results = _enrich_from_db(ids[:fetch_k], km_filter=km_filter) if ids else []

    # 3. Если km_filter задан, но поиск не нашёл нужные KM в top_k —
    #    прямой DB-запрос как фолбек (KM всегда должен отвечать независимо от индекса)
    if km_filter and not results:
        logger.info(
            f"[Retrieval] Индексный поиск не нашёл чанки для {km_filter}, "
            f"делаю прямой DB-запрос"
        )
        results = _fetch_km_direct(km_filter, k)

    # 4. Если km_filter НЕ задан и results всё ещё пустой — BM25 fallback без фильтра
    if not km_filter and not results and ids:
        results = _enrich_from_db(ids[:fetch_k], km_filter=None)

    return results[:k]
