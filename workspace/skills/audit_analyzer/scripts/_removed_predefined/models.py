"""Удалено — фаза 9 миграции ``enterprise-mcp-platform``: Модели реестра: ScriptDefinition и параметры.

mcp-platform/libs/audit/models.py. Доступ агента к данным аудита идёт через tool ``workspace/tools/audit_analyzer_query.py`` в capability ``audit`` платформы.

Файл физически удалить нельзя: политика безопасности требует служебный
лаунчер mavis-trash. Код вырезан (77 строк), каталог переименован в
``_removed_*``, поэтому ни один импорт его не находит и pytest не собирает
лежащие рядом тесты. Удалить вручную:
git rm -r workspace/skills/audit_analyzer/scripts/_removed_predefined
"""
