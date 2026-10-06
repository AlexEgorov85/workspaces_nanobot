"""DocumentLoader — единственная точка входа цепочки загрузки.

Цепочка: **извлечение** (формат) → **разделение** (общее правило) →
**анализ** (дальше, вне этого модуля). Здесь она склеивается в одну
функцию, и ни одна из стадий не знает о существовании другой.

Loader ничего не решает про границы блоков и ничего не знает про форматы,
кроме имени: извлекатель берётся из реестра, поэтому добавление нового типа
файла этот модуль не трогает.

Один проход по файлу: извлекатель открывает файл один раз и отдаёт и текст,
и заголовок из метаданных (тот же объект ``PdfReader`` / ``Document``), так
что прежний второй проход на title resolution здесь больше не нужен.
"""

from __future__ import annotations

from pathlib import Path

from libs.legal_summarizer.document.extraction import (
    split_units_into_blocks,
    title_from_text,
)
from libs.legal_summarizer.document.physical import PhysicalDocument
from libs.legal_summarizer.document.registry import get_extractor

__all__ = ["DocumentLoader"]


class DocumentLoader:
    """Canonical loader для ``PhysicalDocument``.

    Usage::

        doc = DocumentLoader().load(path, workspace_root=...)
    """

    __slots__ = ()

    def load(
        self,
        path: str | Path,
        *,
        workspace_root: Path | str | None = None,
    ) -> PhysicalDocument:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Файл не найден: {p}")

        from libs.office import detect_format

        # Стадия 1: формат отдаёт сырые единицы.
        extracted = get_extractor(detect_format(p)).extract(p)

        # Стадия 2: общее правило решает, что является блоком.
        blocks = split_units_into_blocks(extracted.units)

        # Заголовок: метаданные формата, иначе первая содержательная строка.
        text = "\n\n".join(b.content for b in blocks)
        title = extracted.title or title_from_text(text)

        return PhysicalDocument(
            path=str(p.resolve()),
            format=detect_format(p),
            title=title,
            size_bytes=p.stat().st_size,
            blocks=tuple(blocks),
            page_count=extracted.page_count,
        )