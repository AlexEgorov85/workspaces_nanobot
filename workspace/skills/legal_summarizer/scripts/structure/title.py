"""Document title resolution (PLAN §14, Этап 14).

Источники title (по приоритету):

1. ``DOCX core_properties.title`` → ``source="metadata"``.
2. ``PDF metadata /Title`` → ``source="metadata"``.
3. ``PDF /Info Title`` → ``source="metadata"``.
4. ``DOCX Title`` style → ``source="visual"``.
5. Первая strong title candidate (DOCX Heading 1 с маленьким уровнем
   и коротким текстом) → ``source="inferred"``.
6. Первая непустая строка → fallback (для обратной совместимости с
   ``_pick_title_from_text``).

Это **детерминированный** resolve (без LLM).

Результат — ``DocumentTitle`` из ``structure/models.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from workspace.skills.legal_summarizer.scripts.structure.models import (
    DocumentTitle,
)
from workspace.skills.legal_summarizer.scripts.structure.physical import (
    DocumentBlock,
    PhysicalDocument,
)


def _metadata_title(path: Path) -> tuple[str | None, str]:
    """Прочитать title из метаданных формата (DOCX/PDF/ПPPTX).

    Возвращает ``(title, source)`` где ``source`` — ``"metadata"`` или
    пустая строка.
    """
    fmt = path.suffix.lower().lstrip(".")
    if fmt == "docx":
        try:
            from docx import Document

            doc = Document(str(path))
            t = doc.core_properties.title
            if t and t.strip():
                return t.strip(), "metadata"
        except Exception:
            pass
    if fmt == "pdf":
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(path))
            md = reader.metadata or {}
            t = md.get("/Title") or md.get("/Info:Title")
            if t and str(t).strip():
                return str(t).strip(), "metadata"
        except Exception:
            pass
    if fmt == "pptx":
        try:
            from pptx import Presentation

            pres = Presentation(str(path))
            t = pres.core_properties.title
            if t and t.strip():
                return t.strip(), "metadata"
        except Exception:
            pass
    return None, ""


def _docx_title_style_block(blocks: tuple[DocumentBlock, ...]) -> DocumentTitle | None:
    """Найти первый DOCX Title-style block → ``source="visual"``."""
    for b in blocks:
        style = b.block_metadata.get("style", "")
        if not style:
            continue
        if style.lower().startswith(("title", "subtitle")):
            return DocumentTitle(
                value=b.content.strip(),
                source="visual",
                confidence=0.95,
                block_ordinal=b.ordinal,
            )
    return None


def _first_strong_candidate(blocks: tuple[DocumentBlock, ...]) -> DocumentTitle | None:
    """Первая heading-кандидат с минимальным level → ``source="inferred"``."""
    for b in blocks:
        if b.block_type == "table":
            continue
        style = b.block_metadata.get("style", "")
        if style.lower().startswith("heading"):
            return DocumentTitle(
                value=b.content.strip(),
                source="inferred",
                confidence=0.7,
                block_ordinal=b.ordinal,
            )
    return None


def _first_nonempty_line_fallback(text: str) -> DocumentTitle | None:
    """Fallback — первая непустая строка (для обратной совместимости)."""
    for line in (text or "").splitlines():
        line = line.strip()
        if len(line) >= 3:
            return DocumentTitle(
                value=line[:200],
                source="inferred",
                confidence=0.5,
                block_ordinal=None,
            )
    return None


def resolve_title(
    doc: PhysicalDocument,
    *,
    text: str | None = None,
) -> DocumentTitle | None:
    """Извлечь title из ``PhysicalDocument``.

    Приоритет источников — по плану §14 (см. docstring модуля).

    Args:
        doc: ``PhysicalDocument`` (обязателен — нам нужны ``blocks``).
        text: полный текст документа (для ``_first_nonempty_line_fallback``,
            если ничего лучше не нашлось). ``None`` → пропустить fallback.

    Returns:
        ``DocumentTitle`` или ``None``.
    """
    title, _ = _metadata_title(Path(doc.path))
    if title:
        return DocumentTitle(value=title, source="metadata", confidence=1.0)

    t = _docx_title_style_block(doc.blocks)
    if t is not None:
        return t

    t = _first_strong_candidate(doc.blocks)
    if t is not None:
        return t

    if text is not None:
        return _first_nonempty_line_fallback(text)
    return None


__all__ = ["resolve_title"]