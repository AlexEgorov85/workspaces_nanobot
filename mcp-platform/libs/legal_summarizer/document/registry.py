"""Реестр форматов: имя формата → извлекатель.

Единственное место, где перечислены поддерживаемые форматы. Добавление
нового типа файла — это **один файл** в ``document/extractors/`` плюс **одна
строка** здесь; ни разбиение, ни анализ, ни кеш не меняются.
"""

from __future__ import annotations

from libs.legal_summarizer.document.extraction import TextExtractor
from libs.legal_summarizer.document.extractors.docx import DocxExtractor
from libs.legal_summarizer.document.extractors.pdf import PdfExtractor
from libs.legal_summarizer.document.extractors.txt import TxtExtractor

__all__ = [
    "EXTRACTORS",
    "get_extractor",
    "register_extractor",
    "supported_formats",
]

EXTRACTORS: dict[str, TextExtractor] = {
    "pdf": PdfExtractor(),
    "docx": DocxExtractor(),
    "txt": TxtExtractor(),
}


def supported_formats() -> frozenset[str]:
    """Имена форматов, которые умеет читать домен."""
    return frozenset(EXTRACTORS)


def register_extractor(fmt: str, extractor: TextExtractor) -> None:
    """Зарегистрировать извлекатель нового формата.

    Единственная точка расширения цепочки. Извлекатель обязан только отдать
    сырые единицы (``document.extraction.RawUnit``): что считать блоком,
    решает разделка, и новый формат не получает права принести свою единицу
    чтения — именно этим и был плох каждый прежний формат.
    """
    key = (fmt or "").strip().lower().lstrip(".")
    if not key:
        raise ValueError("register_extractor: имя формата обязательно")
    if not hasattr(extractor, "extract"):
        raise TypeError(
            f"register_extractor: {extractor!r} не имеет extract() — это не "
            "извлекатель"
        )
    EXTRACTORS[key] = extractor


def get_extractor(fmt: str) -> TextExtractor:
    """Извлекатель для формата. Неизвестный формат — отказ со списком."""
    key = (fmt or "").strip().lower().lstrip(".")
    extractor = EXTRACTORS.get(key)
    if extractor is None:
        raise ValueError(
            f"Неподдерживаемый формат: '{fmt}'. Поддерживаются: "
            f"{sorted(supported_formats())}"
        )
    return extractor