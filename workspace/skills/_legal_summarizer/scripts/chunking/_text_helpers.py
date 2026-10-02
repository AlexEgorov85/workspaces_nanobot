"""Text-helper утилиты для chunk layer.

Маленькие чистые функции для разметки chunk-блоков и truncate.
Без зависимостей от downstream subsystems. Раньше жили
в ``application/_text_helpers.py`` — переехали сюда, потому что
работают над ``Chunk`` (логически принадлежит chunking).
"""

from __future__ import annotations

import re
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chunking.chunks import Chunk


_LOCAL_HEADING_RE = re.compile(
    r"^\s*(?:Раздел|Подраздел|Глава|Статья|Часть|§)\b[^\n]{0,120}",
    re.IGNORECASE | re.MULTILINE,
)


def local_structure_label(text: str) -> str:
    """Extract first structural heading from text chunk (inline replacement).

    Находит первую строку, начинающуюся с legal-префикса
    (Раздел/Подраздел/Глава/Статья/Часть/§), усечённую до 120 символов.
    Возвращает ``""``, если не нашёл.
    """
    if not text:
        return ""
    m = _LOCAL_HEADING_RE.search(text)
    return m.group(0).strip()[:120] if m else ""


def chunk_structure_label(chunk: "Chunk") -> str:
    """Структурная метка чанка: global heading, иначе локальная из текста."""
    heading = getattr(chunk, "section_heading", "") or ""
    if heading:
        return heading
    return local_structure_label(getattr(chunk, "text", "") or "")


def format_chunk_block(chunk: "Chunk", summary: str) -> str:
    """Подписать блок чанка его структурной меткой при сборке ответа."""
    label = chunk_structure_label(chunk)
    if label:
        return f"[Chunk {chunk.chunk_id} | {label}]\n{summary}"
    return f"[Chunk {chunk.chunk_id}]\n{summary}"


def fit_input(text: str, budget: int) -> str:
    """Урезать text до budget символов стратегией head + tail."""
    if len(text) <= budget:
        return text
    head = budget * 2 // 3
    tail = budget - head - 200
    if tail < 0:
        tail = 0
    skipped = len(text) - head - tail
    if tail:
        return (
            text[:head]
            + f"\n\n[...пропущено {skipped} символов...]\n\n"
            + text[-tail:]
        )
    return text[:head] + f"\n\n[...пропущено {skipped} символов...]"


def progress(msg: str) -> None:
    """Прогресс ТОЛЬКО в stderr."""
    line = f"[legal_summarizer] {msg}"
    print(line, file=sys.stderr, flush=True)


__all__ = [
    "local_structure_label",
    "chunk_structure_label",
    "format_chunk_block",
    "fit_input",
    "progress",
]