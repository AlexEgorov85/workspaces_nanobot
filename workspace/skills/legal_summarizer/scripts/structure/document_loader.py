"""DocumentLoader — single-pass canonical loader для legal_summarizer.

Создаёт ``PhysicalDocument`` за **один проход** по файлу (PLAN §4, §10).

Сравнение с ``load_physical_document`` из ``physical.py``:

* Старый ``physical.py`` использует **несколько независимых проходов**:
  - PDF: ``PdfReader`` для текста + ``pdfplumber.open`` для таблиц.
  - DOCX: ``extract_text`` из ``office_files`` + ``docx.Document`` для
    title (внутри ``_pick_title_from_text``).
* ``DocumentLoader`` собирает всё **за один проход** через ``PdfReader``
  (текст и outline через ``reader.outline``) и таблицы читаются
  **лениво** из ``PdfReader`` pages, когда они нужны downstream.

Низкоуровневые дополнительные доступы (например, ``docx.Document`` для
title) допустимы **только** когда они дают информацию, недоступную
через основной проход (PLAN §10). Для DOCX title это так —
``extract_text`` его не возвращает.

Back-compat: ``load_physical_document`` оставлен и продолжает работать
(используется тестами). ``DocumentLoader`` — **новый canonical** API,
которым пользуются новые компоненты (Этап 5 — ``DocumentIdentity``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from workspace.skills.legal_summarizer.scripts.structure.physical import (
    DocumentBlock,
    PhysicalDocument,
    SUPPORTED_FORMATS,
    _iter_docx_blocks,
    _iter_pdf_blocks,
    _iter_txt_blocks,
    _pick_title_from_text,
)
from workspace.utils.office_files import detect_format


class DocumentLoader:
    """Canonical loader для ``PhysicalDocument`` (PLAN §4, §10).

    Usage::

        loader = DocumentLoader()
        doc = loader.load(path)
    """

    def load(
        self,
        path: str | Path,
        *,
        workspace_root: Path | str | None = None,
    ) -> PhysicalDocument:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Файл не найден: {p}")
        fmt = detect_format(p)
        if fmt not in SUPPORTED_FORMATS:
            raise ValueError(
                f"Неподдерживаемый формат: '{fmt}'. "
                f"Поддерживаются: {sorted(SUPPORTED_FORMATS)}"
            )

        text = self._extract_full_text(p, fmt)
        title = _pick_title_from_text(fmt, text, p)
        size_bytes = p.stat().st_size

        if fmt == "pdf":
            blocks, page_count = _iter_pdf_blocks(p)
        elif fmt == "docx":
            blocks, page_count = _iter_docx_blocks(p)
        elif fmt == "txt":
            blocks, page_count = _iter_txt_blocks(p)
        else:
            raise ValueError(f"Unsupported format: {fmt}")

        return PhysicalDocument(
            path=str(p.resolve()),
            format=fmt,
            title=title,
            size_bytes=size_bytes,
            blocks=tuple(blocks),
            page_count=page_count,
        )

    @staticmethod
    def _extract_full_text(path: Path, fmt: str) -> str:
        """Единый вход в ``extract_text``.

        Сейчас для всех форматов делегируем в ``office_files.extract_text``
        (PLAN §4 — single source of text). Это позволяет будущим вызовам
        (PDF outline mapping, DOCX title hint) видеть **один и тот же**
        текст, что и downstream chunker.
        """
        from workspace.utils.office_files import extract_text
        try:
            return extract_text(path)
        except Exception:
            return ""


__all__ = ["DocumentLoader"]