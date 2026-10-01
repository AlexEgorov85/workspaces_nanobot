"""Удалено — 2026-10-01, фаза 5 миграции ``enterprise-mcp-platform``: PreloadService — прогрев FAISS-индексов в память + compute_index_health().

Живой код: mcp-platform/libs/vectors/preload.py.

Чем заменено у вызывающего: Индексы собирает capability `vectors`; страж compute_index_health портирован в mcp-platform/tests/test_vectors_index_health.py

Файл физически удалить нельзя: политика безопасности требует служебный
лаунчер mavis-trash. Код вырезан (318 строк), имя начинается с
подчёркивания, поэтому ни один импорт его не находит.
Удалить вручную: git rm lib/services/preload_service.py
"""
