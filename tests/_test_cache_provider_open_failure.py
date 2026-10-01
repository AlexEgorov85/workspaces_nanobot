"""Удалено — 2026-10-01, фаза 5 миграции ``enterprise-mcp-platform``.

ошибки открытия — mcp-platform/libs/enterprise_data/snapshot/store.py

Кластер снимка удалён из агента: снимком владеет capability `data` платформы.
Файл физически удалить нельзя: политика безопасности требует служебный
лаунчер mavis-trash. Содержимое вырезано (128 строк), имя начинается с
подчёркивания, поэтому pytest его не собирает.
Удалить вручную: git rm tests/test_cache_provider_open_failure.py
"""
