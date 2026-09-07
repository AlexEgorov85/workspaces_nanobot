"""Детерминированный operation_id (canonical id для manifest)."""

from __future__ import annotations

import hashlib


def make_operation_id(
    text: str,
    length: str,
    *,
    document_path: str | None = None,
    question: str | None = None,
) -> str:
    """Стабильный operation_id.

    Детерминированно зависит от:

    * полного текста (sha256 hex);
    * ``length`` (brief / detailed);
    * ``document_path`` (если передан);
    * ``question`` (если передан).

    Не использует wall-clock или ``monotonic_ns`` — два прогона с
    одинаковыми аргументами дают одинаковый ``operation_id``, что
    необходимо для idempotency manifest.

    Полный текст хешируется (не префикс): изменение **хвоста**
    документа (например, правка последней статьи) меняет
    ``operation_id`` — иначе idempotency-кэш мог бы вернуть
    устаревший результат для изменённого документа.
    """
    text_blob = text.encode("utf-8", errors="replace")
    h = hashlib.sha256(text_blob).hexdigest()[:12]
    extras = (
        f"\npath:{document_path or ''}"
        f"\nlen:{length}"
        f"\nq:{question or ''}"
    )
    extras_hash = hashlib.sha256(extras.encode("utf-8")).hexdigest()[:8]
    return f"op_{h}_{extras_hash}_{length}"


__all__ = ["make_operation_id"]
