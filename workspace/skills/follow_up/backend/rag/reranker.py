"""
Follow Up 2.0 — Cross-Encoder Reranker.

Использует bge-reranker-v2-m3 (или аналог) для точного ранжирования
результатов поиска.

При недоступности reranker — graceful fallback (возвращает оригинальный порядок).
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from backend.config import get_settings


def _resolve_device() -> str:
    """Куда класть модель: настройка важнее автоопределения.

    `auto` берёт GPU, если она видна. Там, где рядом крутится vLLM, это
    тихая диверсия: карта занята под LLM почти целиком, наши веса
    доедают остаток, и падает не Follow Up, а модель всего подразделения.
    Настройка `MODELS_DEVICE=cpu` закрывает вопрос одной строкой в .env.
    """
    import torch
    from backend.config import get_settings

    want = getattr(get_settings(), "models_device", "auto")
    if want == "cpu":
        return "cpu"
    if want == "cuda":
        return "cuda"
    return "cuda" if torch.cuda.is_available() else "cpu"


logger = logging.getLogger(__name__)

_reranker_model = None
_reranker_available = None


def _get_reranker():
    global _reranker_model, _reranker_available
    if _reranker_available is not None:
        return _reranker_model if _reranker_available else None

    cfg = get_settings()
    if not cfg.reranker_enabled:
        _reranker_available = False
        return None

    try:
        from sentence_transformers import CrossEncoder
        import torch
        device = _resolve_device()
        logger.info(f"[Reranker] Загрузка cross-encoder из {cfg.reranker_model_path}...")
        _reranker_model = CrossEncoder(cfg.reranker_model_path, device=device, max_length=512)
        _reranker_available = True
        logger.info("[Reranker] Cross-encoder загружен.")
        return _reranker_model
    except Exception as e:
        logger.warning(f"[Reranker] Недоступен ({e}). Будет использован оригинальный порядок.")
        _reranker_available = False
        return None


def rerank(
    query: str,
    chunks: List[Dict],
    top_n: Optional[int] = None,
) -> List[Dict]:
    """
    Реранжирует список чанков по релевантности к запросу.

    Returns чанки в порядке убывания релевантности.
    """
    if not chunks:
        return chunks

    cfg = get_settings()
    n = top_n or cfg.reranker_top_n

    model = _get_reranker()
    if model is None:
        # Fallback: возвращаем первые N без реранжирования
        return chunks[:n]

    try:
        pairs = [(query, c["text"][:512]) for c in chunks]
        scores = model.predict(pairs, show_progress_bar=False)

        scored = sorted(
            zip(scores, chunks),
            key=lambda x: x[0],
            reverse=True,
        )
        result = [c for _, c in scored[:n]]
        logger.debug(f"[Reranker] Реранжировано {len(chunks)} → {len(result)} чанков")
        return result

    except Exception as e:
        logger.error(f"[Reranker] Ошибка: {e}")
        return chunks[:n]
