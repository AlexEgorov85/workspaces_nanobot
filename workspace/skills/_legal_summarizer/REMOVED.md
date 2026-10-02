# Удалено — фаза 11 миграции `enterprise-mcp-platform`: навык `legal_summarizer`.

**Что здесь было.** Навык агента: разбор юридического документа (PDF/DOCX/TXT),
чанкинг, вызов LLM, кэш разбора. Агент запускал его подпроцессом
(`workspace/skills/legal_summarizer/scripts/cli.py`) через tool
`legal_summarizer_query`.

**Куда переехало.** Домен целиком живёт в `mcp-platform/libs/legal_summarizer/`,
его тесты — в `mcp-platform/tests/legal_summarizer/`, объявление capability —
`mcp-platform/servers/enterprise/capabilities/legal_summarizer/`. Настройки
capability живут в `mcp-platform/platform.json → legal_summarizer`.

**Как агент ходит в домен теперь.** Tool `workspace/tools/legal_summarizer_query.py`
вызывает операцию `query_operation` у процесса `enterprise-mcp`. Подпроцесса
с Python-кодом навыка в агенте больше нет.

## Почему подчёркивание, и почему `_SKILL.md`

Загрузчик навыков `nanobot/agent/skills.py::_skill_entries_from_dir` берёт
**любой** подкаталог `workspace/skills/`, в котором лежит `SKILL.md`:
проверка на префикс `_` там есть только у модулей (`project_tool_loader`,
`hook_loader`), но не у каталогов навыков. Поэтому одного переименования
каталога мало — `SKILL.md` переименован в `_SKILL.md`, и навык перестаёт
обнаруживаться. Навык не «выключен», а именно не найден.

## Что делать с оставшимися файлами

Файлы физически удалить нельзя: политика безопасности требует служебный
лаунчер `mavis-trash`. Код не вырезан — в отличие от tombstone'ов
`audit_analyzer/scripts/_*.py`, здесь 223 модуля, и они побайтово
дублируют `mcp-platform/libs/legal_summarizer/`. Инертны: ни один модуль
агента их не импортирует, пути в `pyproject.toml` убраны, секция
`project.json → skills.legal_summarizer` вырезана.

Удалить вручную:

```bash
git rm -r workspace/skills/_legal_summarizer
```
