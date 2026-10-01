"""Удалено — фаза 9 миграции ``enterprise-mcp-platform``: Поведение навыка: NL→SQL, NO_MATCH, DDL/DML-отказ.

mcp-platform/tests (контракт capability audit). Доступ агента к данным аудита идёт через tool ``workspace/tools/audit_analyzer_query.py`` в capability ``audit`` платформы.

Файл физически удалить нельзя: политика безопасности требует служебный
лаунчер mavis-trash. Код вырезан (545 строк), каталог переименован в
``_removed_*``, поэтому ни один импорт его не находит и pytest не собирает
лежащие рядом тесты. Удалить вручную:
git rm -r workspace/skills/audit_analyzer/_removed_tests
"""
