"""Офисные документы: разбор текста, таблиц и метаданных.

Домен платформы. Логика — в :mod:`libs.office.parser`, наружу отдаётся только
этот реэкспорт: потребители берут парсер отсюда и не знают, в каком модуле он
лежит.

Кто пользуется:

* ``libs/legal_summarizer/document/physical.py`` и
  ``libs/legal_summarizer/application/document_io.py`` — adapter суммаризатора;
* tool агента ``document_read`` — импортирует парсер напрямую из этого пакета.
  Отдельная MCP-операция ради локального чтения файла была бы вторым путём к
  одному и тому же разбору, а не границей между доменами.

Парсер поднимает движок формата (docx/pypdf/pptx/openpyxl/xlrd/pdfplumber)
лениво, поэтому сам пакет импортируется без третьесторонних зависимостей.
"""

from __future__ import annotations

from libs.office.parser import (
    detect_format,
    extract_tables,
    extract_text,
    read_xlsx_sheet,
    summarize,
)

__all__ = [
    "detect_format",
    "extract_tables",
    "extract_text",
    "read_xlsx_sheet",
    "summarize",
]
