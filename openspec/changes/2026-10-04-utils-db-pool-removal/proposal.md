# Пул `workspace/utils/db.py`: инвентаризация потребителей и снос надгробий

## Статус этого change'а

Change создан **после** того, как выяснилось, что id
`2026-10-04-task-queue-drop-direct-sql` занят параллельной работой над
журналом (`DbLoggingService`). Здесь зафиксирована **только** инвентаризация
пула и разбор шага 3, чтобы два разных изменения не затирали друг друга.
Файлы удалённых надгробий — общий результат, он описан в обоих change'ах.

## Why

Шаги 1 и 2 change `2026-10-02-task-queue-into-mcp` сделаны: очередь задач
живёт на платформе, канал ходит в её операции, в `lib/channels/` не осталось
ни одного `execute`. Остались шаги 3 и 4.

**Цифры старой постановки неверны.**
`openspec/changes/2026-10-02-task-queue-into-mcp/proposal.md:5-12` называет
«~5900 строк прямого SQL». Измерение по текущему дереву этого не подтверждает:

- `workspace/utils/db.py` — **1143 строки**, строк с SQL-конструкциями **8**,
  вызовов `.execute(`/`executemany(` — **13**. Это фабрика пула
  (`DBManager` + `_Worker` + `_CursorProxy` + `_ConnectionProxy`), а не простыня
  SQL.
- Упоминаний `lib.utils.db` в дереве — **35 файлов**, а не 7. Реальных
  (не docstring) импортов в продакшн-коде — **4 файла**, и один из них
  (`gateway.py`) не входит в владение ни одного из соседних change'ов.

## What Changes

### Сделано (шаг 4)

Удалены два надгробия платформы (`git rm`, staged):

- `mcp-platform/servers/enterprise/capabilities/data/tools/_claim_task.py`
- `mcp-platform/servers/enterprise/capabilities/data/tools/_update_task_status.py`

### Не сделано (шаг 3)

Удаление `workspace/utils/db.py` **не выполнено**. Три независимые причины,
каждая достаточна:

1. **Пул обязателен нормативно.**
   `openspec/specs/runtime/startup-schema-validation/spec.md:180-187`
   (Requirement «Проверка выполняется через пул соединений БД») требует, чтобы
   проверка шла **через тот же пул**, что и остальные сервисы
   (`lib.utils.db` / `get_pool`), а `:55-59` («Must not depend on») запрещает
   зависеть от сетевых ресурсов вне пула `lib.utils.db`. Канон правится только
   дельтой через `openspec archive`; дельты против этого capability не
   объявлялась.

2. **Нет пула, на который можно переехать.** После шага 2 в агенте не осталось
   второго владельца соединений: у канала пула нет
   (`lib/channels/postgres_channel.py` работает через `MessageExchange` /
   `QueueOps`), а `SanitizingSessionStore` наследует `JsonlSessionStore` —
   PostgreSQL в сессиях это только холодное зеркало (`lib/gateway/mirror/`).
   `workspace/utils/db.py` остался **единственным** владельцем соединения в
   рантайме агента.

3. **Платформа недостижима из места вызова.** Единственная альтернатива —
   операция `schema_check` платформы, которая уже умеет проверять произвольный
   список: `DataService.schema_check(expected=...)`
   (`capabilities/data/service/main.py:1410-1442`) возвращает
   `{expected, found, missing, ok, tables}`. Но обе точки вызова живут в
   `ApplicationContext.start()`, а `start()` выполняется **до** `asyncio.run`:
   `gateway.py:179` и `cli_agent.py:244` — `ctx.start()`, затем
   `asyncio.run`. `EnterpriseMcpClient.call()` и `_ensure_session()` — `async`
   и требуют живого loop (`enterprise_mcp_client.py:998, 1168, 1207`),
   синхронного моста в клиенте нет. Перенос проверки в живой loop означает
   правку `gateway.py` и `cli_agent.py`.

## Карта потребителей (измерено)

Упоминаний `lib.utils.db` — 35 файлов:

| Категория | Файлов | Влияние удаления |
|---|---|---|
| Продакшн, реальный импорт | 4 | см. ниже |
| Продакшн, только docstring | 2 | нет |
| Тесты, реальный импорт | 3 | **падают** |
| Тесты, строковый `patch` / `monkeypatch.setattr` | 2 (8 мест) | **падают** |
| Тесты, подставной `sys.modules["lib.utils.db"]` | 6 | безвредны |
| Тесты, текст в фикстуре / docstring | ~18 | нет |

Исчерпывающий список реальных импортов в продакшн-коде:

- `lib/core/application_context.py` — 5 мест: `fetch_with_timeout` (стр. 898,
  проверка схемы на старте), `_get_manager` + `_Job` (стр. 1325, readiness-пинг
  `SELECT 1`), `set_pool_config` (1950), `start` (1962), `shutdown` (1972).
- `lib/services/session_storage.py:188` — `configure`.
- `gateway.py:653` — `probe_connections`, `get_stats`. Импорт обёрнут в
  `try/except Exception`, поэтому удаление модуля не уронит шлюз, а отчёт о
  пуле станет постоянно «DB pool: не удалось подключиться» — враньё на каждом
  старте.
- `scripts/backfill_media_aw.py:37` — `configure, execute, fetch, start,
  shutdown`. Вне заявленной области (`lib/`, `workspace/`, `tools/`).

## Вывод по остаточному прямому SQL

Перепроверено **построчно**: часть «SQL-вхождений» старой постановки — это
docstring'и и тексты сообщений об ошибках.

- `tools/migrate.py`, `tools/apply_test_profile_tables.py` — инструменты
  миграции БД, прямой доступ законен. Не трогались.
- `lib/services/schema_validation.py` — **1** реальный SQL
  (стр. 284-290, `information_schema.tables`), выполняется через внедрённый
  `fetch`. Остальные совпадения — docstring и текст сообщения.
- `lib/core/application_context.py` — **1** реальный `execute`:
  `cur.execute("SELECT 1")` (стр. 1329), readiness-пинг.
- `lib/gateway/mirror/session_mirror.py` — **0**. Совпадение (стр. 29) — docstring.
- `lib/core/project_settings.py` — **0**. Совпадение (стр. 79) — docstring.

Фактический остаток прямого SQL в `lib/` — **два SELECT'а**, оба на старте,
до подъёма платформы.

## Уточнение к надгробиям

Их собственный текст **ложен**: он утверждает, что операции `claim_task` /
`update_task_status` удалены, но на шаге 1 этого же change'а они введены
заново — батчевый `claim_tasks` (`service/main.py:1478`), одиночный
`claim_task` как его представление (`service/main.py:1606-1632`) и
`update_task_status` (`service/main.py:1634`). Файлы устарели, а не описывали
удалённое. На удаление это не влияет — кода в них не было и нет, — но читать
их как документацию нельзя.

Удаление проверено запуском, а не чтением: `discover_tool_files` на capability
`data` возвращает **23** файла операций, файлов с именем на `_` в выборке
загрузчика **ноль**, `claim_task.py` и `update_task_status.py` на месте.
Загрузчик пропускает имена на `_` по соглашению
(`libs/enterprise_common/loader.py:71`).

## Влияние

- Публичные контракты и набор операций платформы не меняются.
- `openspec/specs/**` не трогается, дельты нет.

## Открытый вопрос (требует владельца)

1. Кто снимает пул: дельта против `runtime/startup-schema-validation` с
   переносом проверки в живой loop (правка `gateway.py`, `cli_agent.py`),
   либо снос силами change'а, который эти файлы уже правит.
2. `scripts/backfill_media_aw.py` — вне заявленной области, но перестанет
   работать вместе с модулем.
3. `mcp-platform/docs/MCP-CONTRACTS.md:711-712` («На диске остались
   надгробия») после удаления неверен. Платформенные документы вне владения
   этого change'а, оставлены как есть.
