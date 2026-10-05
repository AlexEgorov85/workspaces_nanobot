# Tasks: разбор юридического документа с доступом к папке сессии

Порядок обязателен: 1-3 создают условие, без которого 4-5 писать нечем.

Прогон тестов на этой машине — **пофайлово, явным списком**. НИКОГДА
`pytest tests/ -q`, НИКОГДА `-k` по всему дереву, НИКОГДА `-n auto`/xdist.
Флаги: `-q --tb=line -rf -p no:cacheprovider -x`. Перед стартом проверить
свободную память; меньше 4 ГБ — не стартовать.

## 1. Узкий доступ: выдать `SessionHandle` операции

- [ ] 1.1 Реализовать получение `SessionHandle` из `ToolExecutionContext` в
      `servers/enterprise/server.py` (точка сборки рядом с `read_result` и
      `session_files`, условие регистрации то же — `_needs_data(wanted)`)
- [ ] 1.2 Проверить, что `SessionHandle` отдаёт операции файлы **только** своей
      сессии: `session_id` вшит, выбора нет
- [ ] 1.3 Прогнать `mcp-platform/tests/test_session_workspace.py` пофайлово

## 2. Правило стража: capability не получает каталог ни в какой форме

- [ ] 2.1 **Список `FORBIDDEN` НЕ трогать.** Запреты `SessionWorkspace`,
      `ArtifactStore`, `mkdir`, `write_text`/`write_bytes`/`writelines`,
      `session_workspace` остаются как есть — это и есть содержание решения
- [ ] 2.2 Добавить в страж проверку, что capability не может получить
      `SessionHandle` (сейчас проверяется `session_workspace`, узкое
      представление в списке проверяемых форм не значится)
- [ ] 2.3 Добавить синтетическую пробу, что **и** подмена `session_id`
      аргументом ловится
- [ ] 2.4 Прогнать `mcp-platform/tests/test_tool_execution_boundaries.py`
      пофайлово; убедиться, что все его `ALLOWED`/`OWNERSHIP_ALLOWED` пробы
      остались зелёными — правило не должно задевать законные формы
- [ ] 2.5 Проверить пробой, что новое правило ловит нарушение: capability,
      импортирующая `SessionHandle`, обязана падать

## 3. Состояние операции: объявить, где оно живёт

- [ ] 3.1 Решить и записать в `platform.json`, чем объявляется расположение
      состояния: подкаталог сессии либо объявленный `cache_root` под ним.
      **Молчаливый вывод из `__file__` запрещён** — это и есть текущий дефект
- [ ] 3.2 Обновить `cache/manifest.py::skill_repo_root` и
      `cache/document_cache.py::_default_cache_root` так, чтобы запасной путь
      совпадал с объявленным
- [ ] 3.3 Прогнать `mcp-platform/tests/legal_summarizer/architecture/test_state_root_agreement.py`
      и `test_cache_root_source.py` пофайлово
- [ ] 3.4 Обновить `platform.json → legal_summarizer._about` — сейчас он врёт
      про «в файле», которого нет

## 4. Операция запуска разбора

- [ ] 4.1 Объявить операцию в `mcp-platform/servers/enterprise/tools/`
      (платформенная, не внутри `capabilities/legal_summarizer/`)
- [ ] 4.2 Принимать документ как `session://`-ссылку либо относительный путь
      внутри `files/`; отвергать абсолютный путь, букву диска и `..`
- [ ] 4.3 **Resumable по частям:** вернуть состояние после каждого шага, продолжить
      следующим вызовом с тем же `operation_id`. Основание:
      `execution_timeout_sec = 120` против оценки до 1000 с, а поток, не
      уложившийся в срок, не прерывается и его результат отбрасывается целиком
      (`execution/pipeline.py:331-349`)
- [ ] 4.4 Подтверждение: без явного аргумента возвращать `confirmation_required` и
      **не** делать ни одного LLM-вызова; `operation_id` возвращать, чтобы
      подтверждение относилось к тому же прогону
- [ ] 4.5 Результат отдавать ссылкой `session://`, не путём на машине
- [ ] 4.6 Добавить в `settings.py` (`CapabilitySettings`) и в
      `config.json → tools.mcpServers.enterprise.enabled_tools`
- [ ] 4.7 Прогнать `mcp-platform/tests/test_legal_summarizer_capability.py`
      пофайлово

## 5. Документация и приёмка

- [ ] 5.1 `workspace/TOOLS.md`: снять запрет «разбор недоступен», описать
      `analyze_document` вместо `query_operation`-only. **Это видимая модели
      сторона** — пока тут старое, модель будет отказывать пользователю
- [ ] 5.2 `mcp-platform/libs/legal_summarizer/skill/SKILL.md:100` — устаревший
      путь `workspace\skills\legal_summarizer\scripts\cli.py`; `:236` — ссылка на
      снесённый tool `legal_summarizer_query`. Файл вне зоны загрузки, но он
      противоречит сам себе
- [ ] 5.3 Прогнать `tests/test_agent_facing_docs_contract.py` пофайлово — страж
      проверяет правду инструкций, включая `KNOWN_MISPLACED_SKILLS`
- [ ] 5.4 Обновить `AGENTS.md`: запись про `legal_summarizer` («уехал на
      платформу») и про `enabled_tools` (8 операций)
- [ ] 5.5 Прогнать `tests/test_mcp_platform_declaration.py` пофайлово

## Приёмка

Считается выполненной, когда:

1. Разбор длинного документа, не уложившийся в один вызов, **продолжается**
   следующим вызовом, а не начинается заново и не отбрасывается по таймауту.
2. Модель получает `session://`-ссылку и читает результат существующим
   `read_result`; путь на машине платформы наружу не утекает.
3. `analyze_document` без явного подтверждения на длинном документе делает
   **ноль** LLM-вызовов.
4. `query_operation` на состоянии, созданном `analyze_document`, работает без
   ручной подсказки `operation_id`.
5. Список `FORBIDDEN` стража не изменён; все его пробы зелёные; новая проба
   ловит и `SessionHandle` в capability, и подмену `session_id` аргументом.
6. `workspace/TOOLS.md` больше не велит модели отказывать пользователю в разборе.

## Чего эта спека не делает

- Не возвращает агентские `SKILL.md` и tool-обёртки — снесено осознанно.
- Не трогает запреты стража.
- Не меняет `document_id` и контентный хеш; не переносит состояние старых
  операций (разрыв `data_store/cache/skills/…` → `<cache_root>/operations/…`
  уже случился и зафиксирован).
