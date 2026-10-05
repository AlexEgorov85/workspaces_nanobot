"""Детерминированный operation_id (canonical id для manifest)."""

from __future__ import annotations

import hashlib


def make_operation_id(
    text: str,
    length: str,
    *,
    document_path: str | None = None,
    question: str | None = None,
    focus: str | None = None,
) -> str:
    """Стабильный operation_id.

    Детерминированно зависит от:

    * полного текста (sha256 hex);
    * ``length`` (brief / detailed);
    * ``document_path`` (если передан);
    * ``question`` (если передан);
    * ``focus`` (если передан).

    Не использует wall-clock или ``monotonic_ns`` — два прогона с
    одинаковыми аргументами дают одинаковый ``operation_id``, что
    необходимо для idempotency manifest.

    Полный текст хешируется (не префикс): изменение **хвоста**
    документа (например, правка последней статьи) меняет
    ``operation_id`` — иначе idempotency-кэш мог бы вернуть
    устаревший результат для изменённого документа.

    ``focus`` — не украшение промпта, а инструкция LLM, меняющая
    сам текст сводки (``llm/prompts_runtime.py``, вызовы в
    ``execution/map_reduce.py``), поэтому он входит в идентичность
    наравне с путём, ``length`` и ``question``. Без этого сегмента
    повторный разбор того же документа с другим фокусом молча
    возвращал прежнюю сводку: короткозамыкание на ``completed``
    в ``application/service.py`` срабатывало по старому
    ``operation_id``.

    Сегменты именованные и разделители — перевод строки, поэтому
    подстановка значения в один сегмент не может слиться с
    содержимым другого. Отсутствующее значение даёт пустой сегмент
    ровно как у ``path`` и ``q`` — старые вызовы без ``focus``
    остаются детерминированными.
    """
    text_blob = text.encode("utf-8", errors="replace")
    h = hashlib.sha256(text_blob).hexdigest()[:12]
    extras = (
        f"\npath:{document_path or ''}"
        f"\nfocus:{focus or ''}"
        f"\nlen:{length}"
        f"\nq:{question or ''}"
    )
    extras_hash = hashlib.sha256(extras.encode("utf-8")).hexdigest()[:8]
    return f"op_{h}_{extras_hash}_{length}"


__all__ = ["make_operation_id"]
