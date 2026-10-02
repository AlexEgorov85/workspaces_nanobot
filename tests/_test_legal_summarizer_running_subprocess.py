"""Обезличено — фаза 11 миграции `enterprise-mcp-platform`: контракт подпроцесса навыка.

Запускал `workspace/skills/legal_summarizer/scripts/cli.py` подпроцессом.
Подпроцесса с Python-кодом навыка в агенте больше нет: tool
`legal_summarizer_query` зовёт операцию `query_operation` у процесса
`enterprise-mcp`. Контракт подпроцесса проверять нечем.
Тестировал Python-код навыка `legal_summarizer` в
`workspace/skills/legal_summarizer/scripts/`. Код переехал в домен
платформы — `mcp-platform/libs/legal_summarizer/` — и покрыт там
собственными тестами (`mcp-platform/tests/legal_summarizer/`). Агент
ходит в домен операцией `query_operation` через tool
`workspace/tools/legal_summarizer_query.py` и сам Python-код домена
не импортирует, поэтому проверять его со стороны агента больше нечего.
Навык обезличен: `workspace/skills/_legal_summarizer` (+ `REMOVED.md`).

Файл физически удалить нельзя: политика безопасности требует служебный
лаунчер mavis-trash. Код вырезан, имя начинается с подчёркивания, поэтому
pytest этот файл не собирает. Удалить вручную:
git rm tests/_test_legal_summarizer_running_subprocess.py
"""
