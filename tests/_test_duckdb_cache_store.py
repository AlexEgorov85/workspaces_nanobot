"""Удалено — 2026-10-01, фаза 5 миграции ``enterprise-mcp-platform``.

mcp-platform/tests/test_snapshot_store.py, test_snapshot_writer_records.py, test_snapshot_writer_garbage.py

Кластер снимка удалён из агента: снимком владеет capability `data` платформы.
Файл физически удалить нельзя: политика безопасности требует служебный
лаунчер mavis-trash. Содержимое вырезано (467 строк), имя начинается с
подчёркивания, поэтому pytest его не собирает.
Удалить вручную: git rm tests/test_duckdb_cache_store.py
"""
