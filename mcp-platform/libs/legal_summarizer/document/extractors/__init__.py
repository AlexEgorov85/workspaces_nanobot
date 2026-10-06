"""Извлекатели форматов: по одному модулю на формат.

Каждый модуль отдаёт *сырые единицы* (``document.extraction.RawUnit``) и
ничем больше не занимается: где кончается абзац, решает разделка
(``document.extraction.split_units_into_blocks``), а не извлекатель.

Добавление формата: новый модуль здесь + регистрация в
``document/registry.py``. Цепочка разбиения и весь анализ остаются как были.
"""

from __future__ import annotations

from libs.legal_summarizer.document.extractors.docx import DocxExtractor
from libs.legal_summarizer.document.extractors.pdf import PdfExtractor
from libs.legal_summarizer.document.extractors.txt import TxtExtractor

__all__ = ["DocxExtractor", "PdfExtractor", "TxtExtractor"]