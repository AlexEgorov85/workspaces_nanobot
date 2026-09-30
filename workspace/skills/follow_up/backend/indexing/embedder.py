"""
Follow Up 2.0 — Embedder.

Исправленная версия: использует SentenceTransformer API (не FlagEmbedding).
Синглтон с ленивой загрузкой.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import List

import numpy as np
import torch
from sentence_transformers import SentenceTransformer

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

_bge_model: SentenceTransformer | None = None


def get_bge_model() -> SentenceTransformer:
    global _bge_model
    if _bge_model is None:
        cfg = get_settings()
        device = _resolve_device()
        logger.info(f"[Embedder] Загрузка BGE-M3 из {cfg.bge_model_path} на {device}...")
        _bge_model = SentenceTransformer(cfg.bge_model_path, device=device)
        logger.info("[Embedder] BGE-M3 загружена.")
    return _bge_model


def embed_texts(
    texts: List[str],
    batch_size: int | None = None,
    normalize: bool = True,
) -> np.ndarray:
    """
    Вычисляет dense-эмбеддинги через SentenceTransformer.
    Возвращает float32 массив [N, dim].
    """
    cfg = get_settings()
    model = get_bge_model()
    bs = batch_size or cfg.bge_batch_size

    embeddings = model.encode(
        texts,
        batch_size=bs,
        normalize_embeddings=normalize,
        show_progress_bar=len(texts) > 50,
        convert_to_numpy=True,
    )
    return np.asarray(embeddings, dtype="float32")
