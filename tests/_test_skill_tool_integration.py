"""Удалено — фаза 9 миграции ``enterprise-mcp-platform``: сквозные тесты workflow навыка.

Гоняли skill-workflow через ``CacheProvider.query_sql`` / ``search_vector`` и ``validate_sql``. Сценариев больше нет.

Файл физически удалить нельзя: политика безопасности требует служебный
лаунчер mavis-trash. Код вырезан, имя начинается с подчёркивания, поэтому
ни один импорт его не подхватывает. Удалить вручную:
git rm tests/_test_skill_tool_integration.py
"""
