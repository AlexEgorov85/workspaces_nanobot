# Удалено — фаза 9: пайплайн готовых скриптов навыка

Живой код переехал в capability ``audit`` платформы: ``list_scripts`` (каталог и валидация параметров), ``run_script`` (исполнение), ``generate_sql`` (NL→SQL). Реестр скриптов — PostgreSQL ``public.agent_predefined_scripts``, его читает платформа.

Каталог физически удалить нельзя: политика безопасности требует служебный
лаунчер mavis-trash. Содержимое вырезано; импортировать нечего — пакет
переименован в `_removed_*`, поэтому ни один импорт его не находит.
Удалить вручную: git rm -r workspace/skills/audit_analyzer/scripts/_removed_predefined
