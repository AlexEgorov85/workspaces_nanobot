"""Sanitize LLM responses — очистка chain-of-thought блоков и извлечение subject.

Некоторые LLM (DeepSeek/Qwen) выдают CoT прямо в финальном ответе:
``<think>reasoning</think>answer``. Без очистки ``extract_subject``
подберёт ``<think>`` как subject, а summary окажется рассуждением
вместо саммари (см. инцидент 2026-08-28 на ГК РФ).

Два варианта мусора:
    1. Закрытый блок ``<think>reasoning</think>answer`` — целиком
       вырезается regex'ом (lazy + DOTALL).
    2. **Незакрытый** ``<think>reasoning\n\nanswer`` — модель забыла
       ``</think>``. Решение: отрезать от ``<think>`` до первого
       абзацного разрыва (``\n\n``), сохраняя реальный ответ после.
       Если разрыва нет (весь текст — рассуждение) — отрезать до конца:
       пустой subject/summary лучше сырого ``<think>``.

NOTE: целевая структура плана предполагает ``llm/sanitize.py`` в пакете
``llm/``, но это имя зарезервировано существующим ``scripts/llm.py``
(LLM-клиент). Переименование ``llm.py`` → ``llm_client.py`` отложено в
отдельный архитектурный этап (требует менять импорты в нескольких
тестах и в ``summarizer.py``). Пока — top-level ``sanitize.py``.
"""
from __future__ import annotations

import re

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_THINK_OPEN = "<think>"
_THOUGHT_CLOSE = "</think>"


def strip_think_blocks(text: str) -> str:
    """Убрать ``<think>…</think>`` блоки из текста (CoT от моделей)."""
    if not text:
        return text
    cleaned = _THINK_BLOCK_RE.sub("", text)
    while _THINK_OPEN in cleaned:
        idx = cleaned.find(_THINK_OPEN)
        close_idx = cleaned.find(_THOUGHT_CLOSE, idx)
        if close_idx != -1:
            cleaned = cleaned[:idx] + cleaned[close_idx + len(_THOUGHT_CLOSE):]
            continue
        blank = cleaned.find("\n\n", idx)
        if blank == -1:
            cleaned = cleaned[:idx]
        else:
            cleaned = cleaned[:idx] + cleaned[blank + 2:]
    return cleaned.strip()


def extract_subject(summary: str) -> str:
    """Извлечь subject (первая непустая строка) из summary.

    Убирает markdown-заголовок (``# Тема`` → ``Тема``). Если строка
    >400 chars — обрезает до первого ``.!?`` (для очень длинных
    авто-сгенерированных subject'ов).

    Args:
        summary: текст саммари (или его первая строка).

    Returns:
        subject string.
    """
    lines = [ln.strip() for ln in (summary or "").splitlines()]
    subject = ""
    for ln in lines:
        if ln:
            subject = ln
            break
    # Убираем markdown-заголовок, если модель вернула "# Тема" как первую строку.
    subject = re.sub(r"^#{1,6}\s+", "", subject).strip()
    if len(subject) > 400:
        m = re.match(r"(.+?[.!?])\s", subject)
        if m:
            subject = m.group(1)
    return subject


__all__ = [
    "strip_think_blocks",
    "extract_subject",
    "_THINK_OPEN",
    "_THOUGHT_CLOSE",
    "_THINK_BLOCK_RE",
]
