"""DocumentLoader — единственный canonical loader для legal_summarizer.

Создаёт ``PhysicalDocument`` за **один проход** по файлу (PLAN §12).

**PLAN §12 + Этап 12:** ``DocumentLoader.load()`` — единая production
загрузочная цепочка. Делает один проход для blocks, и оттуда же
извлекает text для title resolution. Никакого двойного парсинга PDF/DOCX.

Legacy ``load_physical_document`` удалён (Этап 12). Все consumers
используют ``DocumentLoader().load(path)``.
"""

from __future__ import annotations

from pathlib import Path

from workspace.skills.legal_summarizer.scripts.structure.physical import (
    PhysicalDocument,
    SUPPORTED_FORMATS,
    _iter_docx_blocks,
    _iter_pdf_blocks,
    _iter_txt_blocks,
    _pick_title_from_text,
)
from workspace.utils.office_files import detect_format


class DocumentLoader:
    """Canonical loader для ``PhysicalDocument`` (PLAN §4, §10, §12).

    Single-pass loading: ``_iter_*_blocks`` парсит файл один раз,
    и тот же blocks-iteration даёт текст для title resolution
    (через ``_pick_title_from_text``). Никакого второго вызова
    ``extract_text`` или повторного открытия файла.

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

        if fmt == "pdf":
            blocks, page_count = _iter_pdf_blocks(p)
        elif fmt == "docx":
            blocks, page_count = _iter_docx_blocks(p)
        elif fmt == "txt":
            blocks, page_count = _iter_txt_blocks(p)
        else:
            raise ValueError(f"Unsupported format: {fmt}")

        text = "\n\n".join(b.content for b in blocks)
        title = _pick_title_from_text(fmt, text, p)
        size_bytes = p.stat().st_size

        return PhysicalDocument(
            path=str(p.resolve()),
            format=fmt,
            title=title,
            size_bytes=size_bytes,
            blocks=tuple(blocks),
            page_count=page_count,
        )


__all__ = ["DocumentLoader"]