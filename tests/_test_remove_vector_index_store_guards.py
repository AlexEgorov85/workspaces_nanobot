"""Удалено — 2026-10-01, фаза 5 миграции ``enterprise-mcp-platform``.

TestComputeIndexHealthNewSources → mcp-platform/tests/test_vectors_index_health.py; TestBuildFaissIndexMinimalMeta → mcp-platform/tests/test_vectors_index_metadata.py; TestNoHardcodedTableNames перекрыт tests/test_no_hardcoded_table_names.py; остальные три класса стали вакуумными вместе с модулем

Кластер снимка удалён из агента: снимком, индексами и эмбеддингами владеют
capability ``data`` / ``vectors`` / ``llm`` платформы.
Файл физически удалить нельзя: политика безопасности требует служебный
лаунчер mavis-trash. Содержимое вырезано (289 строк), имя начинается с
подчёркивания, поэтому pytest его не собирает.
Удалить вручную: git rm tests/test_remove_vector_index_store_guards.py
"""
