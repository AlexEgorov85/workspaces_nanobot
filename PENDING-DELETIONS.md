# Файлы, которые нельзя удалить самому

Ассистент работает в окружении, где удаление файла заблокировано политикой
безопасности: команда удаления не выполняется, потому что в системе нет
служебного лаунчера `mavis-trash` (есть только `mavis-trash.cmd` и
`mavis-trash.js`, а обёртки вызывать запрещено). Обойти это запрещено и не
нужно: файл, который больше не нужен, просто перестаёт участвовать в работе
(код вырезается, имя меняется на `_`-префикс, если на это смотрит загрузчик).

Поэтому удаление сводится к списку ниже. **После удаления строку убрать отсюда.**

## Обязательно удалить

| Файл | Почему | Команда |
|---|---|---|
| `mcp-platform/servers/enterprise/capabilities/data/tools/_claim_task.py` | Заглушка на месте удалённой операции `claim_task` (коммит `944535e`). Код операции вырезан, файл оставлен пустым намеренно: загрузчик по соглашению пропускает модули с именем, начинающимся с `_`, поэтому операция не публикуется. Сам файл не нужен | `git rm mcp-platform/servers/enterprise/capabilities/data/tools/_claim_task.py` |
| `mcp-platform/servers/enterprise/capabilities/data/tools/_update_task_status.py` | То же для `update_task_status`: обе операции над очередью задач удалены решением владельца, в capability `data` очередь не осталась | `git rm mcp-platform/servers/enterprise/capabilities/data/tools/_update_task_status.py` |
| `mcp-platform/_live_audit_tables.py` | Черновой прогон по таблицам аудита, в git не отслеживается, к миграции не относится | удалить вручную (файл не отслеживается, `git rm` не подходит) |
| `mcp-platform/.sessions_demo/` | Демонстрационный каталог файлов сессии, оставшийся после прогона `SessionWorkspace` вручную. Содержит только синтетические артефакты `sess-DEMO-1`, к проекту не относится | удалить вручную (каталог не отслеживается) |
| `sql/vectors/create_vector_index_config.sql` | Мёртвый DDL: самая конфигурация индексов живёт в `project.json`. `migrate.py` обходит только `sql/migrations/`, файл не исполняется. Код вырезан, осталась заглушка (фаза 5, п. 5.9) | `git rm sql/vectors/create_vector_index_config.sql` |
| `sql/vectors/create_vector_index_store.sql` | Мёртвый DDL: persisted FAISS-кеш удалён ещё change `remove-vector-index-store`, таблица снесена миграцией `V003`. Не исполняется. То же, что и предыдущий (фаза 5, п. 5.9) | `git rm sql/vectors/create_vector_index_store.sql` |

## Python-слой навыка `audit_analyzer` (фаза 9)

Навык перестал владеть данными: запросы строит и проверяет capability `audit`
платформы, агент ходит до неё инструментом `audit_analyzer_query`. Всё
перечисленное не импортируется ничем, но лежит на диске и убирается вручную.
Код внутри каждого файла вырезан, осталась заглушка-описание: загрузчик по
соглашению пропускает модули с именем, начинающимся с `_`.

| Файл | Что было | Строк |
|---|---|---|
| `lib/utils/_sql_safety.py` | SQL Security Guard агента; копия живёт в `mcp-platform/libs/enterprise_data/sql_safety.py` | 425 |
| `workspace/skills/audit_analyzer/scripts/_cli.py` | CLI навыка (`--mode predefined/vector/generated_sql`) | 411 |
| `workspace/skills/audit_analyzer/scripts/_generated_sql_mode.py` | NL→SQL в агенте; порт в `mcp-platform/libs/audit/` | 424 |
| `workspace/skills/audit_analyzer/scripts/_llm.py` | HTTP-клиент модели; выбора модели у агента больше нет | — |
| `workspace/skills/audit_analyzer/scripts/_skill_config.py` | Обёртка над `lib.core.skill_config` для одного навыка | — |
| `workspace/skills/audit_analyzer/scripts/_output.py` | Сериализация вывода CLI | — |
| `workspace/skills/audit_analyzer/scripts/_package_init.py` | `__init__.py` пакета `scripts` (Python не даёт файлу с `_` быть пакетом) | — |
| `workspace/skills/audit_analyzer/scripts/_removed_predefined/` | 6 модулей DB-first реестра скриптов; порт в `mcp-platform/libs/audit/predefined.py` + `registry_loader.py` | 931 |
| `workspace/skills/audit_analyzer/_removed_tests/` | 4 файла тестов навыка (не в `tests/`, а внутри скилла) | ~1784 |

Тесты агента на снятый код (pytest не собирает `_test_*.py`):

| Файл | Строк |
|---|---|
| `tests/_test_audit_analyzer_cli.py` | 411 |
| `tests/_test_audit_analyzer_mode_selection.py` | 311 |
| `tests/_test_audit_analyzer_generated_sql.py` | 424 |
| `tests/_test_skill_cache_boundary.py` | 146 |
| `tests/_test_skill_tool_integration.py` | 211 |
| `tests/_test_sql_safety.py` | 250 |

Удалить одним проходом:

```bash
git rm lib/utils/_sql_safety.py \
  workspace/skills/audit_analyzer/scripts/_cli.py \
  workspace/skills/audit_analyzer/scripts/_generated_sql_mode.py \
  workspace/skills/audit_analyzer/scripts/_llm.py \
  workspace/skills/audit_analyzer/scripts/_skill_config.py \
  workspace/skills/audit_analyzer/scripts/_output.py \
  workspace/skills/audit_analyzer/scripts/_package_init.py \
  tests/_test_audit_analyzer_cli.py \
  tests/_test_audit_analyzer_mode_selection.py \
  tests/_test_audit_analyzer_generated_sql.py \
  tests/_test_skill_cache_boundary.py \
  tests/_test_skill_tool_integration.py \
  tests/_test_sql_safety.py
git rm -r workspace/skills/audit_analyzer/scripts/_removed_predefined \
          workspace/skills/audit_analyzer/_removed_tests
```

Не отслеживается git, `git rm` не подходит:

| Файл | Что это |
|---|---|
| `workspace/skills/audit_analyzer/err1.log` | Мусорный лог, оставшийся от ручных прогонов CLI |

## Черновики в корне

Отладочные пробы и скрипты, оставшиеся от прошлых заходов. Всё это не часть
проекта, но перед удалением стоит убедиться, что соседний воркер сейчас не
работает с ними.

| Файл | Кто оставил |
|---|---|
| `.tmp_call_contract_block.md` | ассистент |
| `.tmp_design_211.md` | ассистент |
| `.tmp_registry_block.md` | ассистент |
| `.tmp_rename_calls.py` | ассистент |
| `.tmp_splice_registry.py` | ассистент |
| `.tmp_strip_claims.py` | ассистент |
| `mcp-platform/.tmp_contracts.py` | ассистент |
| `mcp-platform/.tmp_inventory.py` | ассистент |
| `mcp-platform/.tmp_rename_ctx.py` | ассистент |
| `mcp-platform/.tmp_set_policy.py` | ассистент |
| `mcp-platform/.tmp_container_fix.py` | ассистент (правки контейнера применены и закоммичены, скрипт больше не нужен) |
| `mcp-platform/.tmp_journal_demo.py` | ассистент (ручной прогон писателя журнала, роль изменилась) |
| `mcp-platform/.tmp_meta_probe.py` | ассистент (проба доставки `params._meta` по проводу, проверка стала тестом) |
| `tests/test_user_stop_signal.dump`, `tests/test_user_stop_signal_priority.dump` | ассистент |

## Чего делать не надо

Не удалять и не «чинить» файлы соседнего воркера, даже если они выглядят
недоделанными: у него идёт своя работа по фазе 8.

**Обновлено после того, как фаза 8 была завершена и закоммичена.** Раньше здесь
стояло «его незакоммиченные правки в `platform.json`, `pyproject.toml`,
`requirements.txt`, `docs/MCP-CONTRACTS.md` и `libs/enterprise_common/settings.py`
не коммитить». Это больше не так: работа лежит в истории, и трогать её заново не
нужно.

| Коммит | Что в нём |
|---|---|
| `baad75f` | контейнер без мёртвого поля, сервис замыкается операцией |
| `b8a1fdc` | слой исполнения: `execution/`, `session/`, `eventing/`, контракт вызова |

Что осталось за соседним воркером и по-прежнему не его: спека change'а
(`openspec/changes/enterprise-mcp-platform/`) и `mcp-platform/tests/
test_dependency_declaration.py` (страж объявлений зависимостей, п. 5.14). Их
не коммитить и не переписывать, пока их автор сам этого не сделает.
