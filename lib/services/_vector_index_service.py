"""Удалено — 2026-10-01, фаза 5 миграции ``enterprise-mcp-platform``: VectorIndexBuildService — сборка FAISS из снимка в память.

Живой код: mcp-platform/libs/vectors/builder.py + indexing.py.

Чем заменено у вызывающего: Сборку запускает capability `vectors` и операторская утилита servers/enterprise/build_index.py

Файл физически удалить нельзя: политика безопасности требует служебный
лаунчер mavis-trash. Код вырезан (71 строк), имя начинается с
подчёркивания, поэтому ни один импорт его не находит.
Удалить вручную: git rm lib/services/vector_index_service.py
"""
