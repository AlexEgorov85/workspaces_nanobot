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
| `tests/test_user_stop_signal.dump`, `tests/test_user_stop_signal_priority.dump` | ассистент |

## Чего делать не надо

Не удалять и не «чинить» файлы соседнего воркера, даже если они выглядят
недоделанными: у него идёт своя работа по фазе 8 (`execution/`, `session/`,
`eventing/`, контракт вызова). Его незакоммиченные правки живут в
`mcp-platform/platform.json`, `mcp-platform/pyproject.toml`,
`mcp-platform/requirements.txt`, `mcp-platform/docs/MCP-CONTRACTS.md` и
`mcp-platform/libs/enterprise_common/settings.py` — их не коммитить, даже если
часть правок сделана ассистентом.
