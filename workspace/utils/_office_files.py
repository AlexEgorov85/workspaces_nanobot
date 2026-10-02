"""Удалено 2026-10-02, перенесено в ``mcp-platform/libs/office/parser.py`` (change ``enterprise-mcp-platform``, фаза 11).

Парсер офисных файлов (DOCX/XLSX/XLS/PDF/PPTX/CSV/TXT) жил в агенте, но
потребители уехали в платформу: скилл ``legal_summarizer`` — как
``libs/legal_summarizer/document/physical.py`` и
``libs/legal_summarizer/application/document_io.py``, tool ``document_read`` —
прямым импортом ``libs.office``. Вторая копия разбора разошлась бы с
платформенной при первой же правке разделителей таблиц или определения
кодировки, поэтому платформа — единственный владелец.

Прежняя запись: ``detect_format``, ``extract_text``, ``extract_tables``,
``read_xlsx_sheet``, ``summarize`` и приватные ``_extract_*``/``_summarize_*``/
``_read_text_auto``; ленивые импорты движков форматов, определение кодировки
через chardet с откатом на utf-8/cp1251/latin-1. Публичные имена сохранены
один в один — контракт потребителей не изменился, изменился только адрес.

Файл физически удалить нельзя: политика безопасности требует служебный
лаунчер mavis-trash. Код вырезан, имя начинается с подчёркивания, поэтому
ни один импорт его не подхватывает. Удалить вручную:
git rm workspace/utils/_office_files.py
"""
