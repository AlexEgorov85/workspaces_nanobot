"""Удалено — 2026-10-01, фаза 5 миграции ``enterprise-mcp-platform``: CacheLoadService — разовая синхронная загрузка снимка из PostgreSQL.

Живой код: mcp-platform/libs/enterprise_data/loader.py.

Чем заменено у вызывающего: Загрузкой снимка занимается capability `data`

Файл физически удалить нельзя: политика безопасности требует служебный
лаунчер mavis-trash. Код вырезан (472 строк), имя начинается с
подчёркивания, поэтому ни один импорт его не находит.
Удалить вручную: git rm lib/services/cache_load_service.py
"""
