"""Удалено — 2026-10-01, фаза 5 миграции ``enterprise-mcp-platform``: Слой интерфейса: ABC CacheProvider / CacheIngestion / CacheStore и open_cache_provider().

Живой код: mcp-platform/libs/enterprise_data/snapshot/reader.py + store.py.

Чем заменено у вызывающего: Единственная точка создания провайдера; в рантайме агента не нужна

Файл физически удалить нельзя: политика безопасности требует служебный
лаунчер mavis-trash. Код вырезан (442 строк), имя начинается с
подчёркивания, поэтому ни один импорт его не находит.
Удалить вручную: git rm lib/services/cache_provider.py
"""
