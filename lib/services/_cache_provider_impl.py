"""Удалено — 2026-10-01, фаза 5 миграции ``enterprise-mcp-platform``: Общие помощники слоя интерфейса, включая resolve_cache_path().

Живой код: mcp-platform/libs/enterprise_data/snapshot/store.py.

Чем заменено у вызывающего: Путь снимка объявляет платформа: platform.json → data.snapshot_path

Файл физически удалить нельзя: политика безопасности требует служебный
лаунчер mavis-trash. Код вырезан (488 строк), имя начинается с
подчёркивания, поэтому ни один импорт его не находит.
Удалить вручную: git rm lib/services/cache_provider_impl.py
"""
