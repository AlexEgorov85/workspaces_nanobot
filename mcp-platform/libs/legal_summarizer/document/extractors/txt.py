"""Извлечение из TXT.

Файл приносит ровно одну порцию текста — весь файл. Абзацев в нём нет, и
заводящий их следом раздел не должен считать это особым случаем: правило
раздела общее, а «особый случай» TXT был ровно тем дефектом, из-за которого
структура схлопывалась в один корень.

``page_index`` остаётся ``None``: у .txt нет страниц, и выдумывать их значило
бы сообщить ложную точность — тем же доводом ранее снят fake pagination в
DOCX.
"""

from __future__ import annotations

from pathlib import Path

from libs.legal_summarizer.document.extraction import ExtractionResult, RawUnit

__all__ = ["TxtExtractor"]


class TxtExtractor:
    """TXT → одна сырая единица (весь файл)."""

    format_name = "txt"

    def extract(self, path: Path) -> ExtractionResult:
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")

        units = (RawUnit(text=text),) if text.strip() else ()
        return ExtractionResult(units=units, page_count=1, title=None)