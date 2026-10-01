"""Удалено — фаза 9 миграции ``enterprise-mcp-platform``: точка входа навыка (`python scripts/cli.py --mode ...`).

Единственная точка доступа агента к данным навыка: три режима (``predefined`` / ``generated_sql`` / ``vector``) поверх ``CacheProvider``. Заменена tool'ом агента ``workspace/tools/audit_analyzer_query.py``, который ходит в capability ``audit`` платформы по MCP.

Файл физически удалить нельзя: политика безопасности требует служебный
лаунчер mavis-trash. Код вырезан, имя начинается с подчёркивания, поэтому
ни один импорт его не подхватывает. Удалить вручную:
git rm workspace/skills/audit_analyzer/scripts/_cli.py
"""
