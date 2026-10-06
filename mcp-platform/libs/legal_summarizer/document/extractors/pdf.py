"""Извлечение из PDF.

Формат отдаёт поток позиционированных глифов, а не абзацы, поэтому здесь нет
ничего, кроме текста страниц и таблиц из ``pdfplumber``. Где кончается
страница — не решается: разделка сама разберёт страницу на абзацы по общему
правилу.
"""

from __future__ import annotations

from pathlib import Path

from libs.legal_summarizer.document.extraction import (
    KIND_TABLE,
    ExtractionResult,
    RawUnit,
    render_table,
)

__all__ = ["PdfExtractor"]


class PdfExtractor:
    """PDF → сырые единицы (текст страниц + таблицы)."""

    format_name = "pdf"

    def extract(self, path: Path) -> ExtractionResult:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        page_count = len(reader.pages)
        page_to_tables = self._tables_by_page(path, page_count)

        units: list[RawUnit] = []
        for page_index, page in enumerate(reader.pages, start=1):
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            if text.strip():
                units.append(RawUnit(text=text, page_index=page_index))
            for table_index, rows in enumerate(page_to_tables.get(page_index, [])):
                table_text = render_table(rows)
                if not table_text.strip():
                    continue
                units.append(
                    RawUnit(
                        text=table_text,
                        page_index=page_index,
                        kind=KIND_TABLE,
                        table_index=table_index,
                        block_metadata={"row_count": len(rows)},
                    )
                )

        return ExtractionResult(
            units=tuple(units),
            page_count=page_count,
            title=self._title(reader),
        )

    @staticmethod
    def _tables_by_page(
        path: Path, page_count: int
    ) -> dict[int, list[list[list[str]]]]:
        """Таблицы по страницам. Нет ``pdfplumber`` / упал — таблиц нет.

        Отсутствие таблиц не должно ронять разбор: текст страниц при этом
        извлекается, а худший случай — чанк без ячеек.
        """
        try:
            import pdfplumber

            tables: dict[int, list[list[list[str]]]] = {}
            with pdfplumber.open(str(path)) as pdf:
                for page_index, page in enumerate(pdf.pages, start=1):
                    found = page.extract_tables() or []
                    if found:
                        tables[page_index] = [
                            [[cell or "" for cell in row] for row in table]
                            for table in found
                        ]
            return tables
        except Exception:
            return {}

    @staticmethod
    def _title(reader) -> str | None:
        try:
            title = (reader.metadata or {}).get("/Title")
        except Exception:
            return None
        if not title:
            return None
        return str(title).strip() or None