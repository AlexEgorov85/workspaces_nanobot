"""Обезличено — фаза 11 миграции `enterprise-mcp-platform`: sys.path навыка.

Единственная работа — `sys.path.insert` к
`workspace/skills/legal_summarizer/scripts/`, чтобы импортировать
навык из агентских тестов. Импортирующих больше не осталось; пути
убраны и из `pyproject.toml → pythonpath`.
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
git rm tests/benchmarks/_conftest.py
"""
