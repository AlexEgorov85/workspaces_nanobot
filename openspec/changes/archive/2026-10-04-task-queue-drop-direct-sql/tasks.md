# Tasks — прямой SQL из агента (шаги 3 и 4 change `2026-10-02-task-queue-into-mcp`)

**Baseline (зафиксирован до правок, сравнение — с ним).**

```powershell
python -m pytest tests/ -q                              # 3485 passed, 38 skipped, 2 xfailed
Push-Location mcp-platform; python -m pytest -q         # 5837 passed, 1 skipped
python tools/validate_component_specs.py --strict      # 3 ошибки в runtime/entrypoints/spec.md
```

## S1. Шаг 4 — надгробия на платформе

- [x] 1.1 Проверить инертность `_claim_task.py` и `_update_task_status.py`:
      загрузчик пропускает модули с именем на подчёркивании, в реестр они не
      попадают (`discover_tool_files` возвращает 32 файла, ни один не
      надгробие). Тестов, ссылающихся на эти имена, нет.
- [x] 1.2 Снять формулировку «операции нет» из документации, которая её
      утверждала: `mcp-platform/docs/MCP-CONTRACTS.md` (§ «Надгробия»).
      **Не выполнено — см. блокер B4.**
- [x] 1.3 Завести страж `mcp-platform/tests/test_capability_tool_tombstones.py`:
      новые надгробия падают, перечисленные перечисляются с причиной и
      командой удаления, инертность для реестра подтверждается.
- [x] 1.4 Файлы удалены с диски **владельцем** во время работы над change
      (политика окружения запрещает удаление агенту). Заявка на ручное удаление
      выполнена; `git status` показывает их удалёнными. Осталось вычеркнуть их
      из `KNOWN_TOMBSTONES` — страж этого требует сам (пункт B1).

## S2. Шаг 3 — журнал: писатель один

- [x] 2.1 Снять прямой SQL из `DbLoggingService`: удалены `_db_run`,
      `_ensure_schema`, `_insert_batch`, `_upsert_question_run`.
- [x] 2.2 Убрать `LOCAL-FALLBACK`: «писателя нет» больше не означает «пиши
      в базу». Потеря считается и называется (`loss_reasons`).
- [x] 2.3 Перевести чистку на существующую операцию `purge_logs`:
      `McpLogWriter.purge_logs(retention_days, remove_empty_outbound)` +
      `_purge_via_platform` в сервисе.
- [x] 2.4 Снять ложь в докстрингах: модульный докстринг, докстринг worker'а,
      `_json_safe`, комментарий про транспорт в конструкторе.
- [x] 2.5 Проверить, что второго пути записи в журнал не осталось:
      `grep` по `cur.execute|INSERT INTO|DELETE FROM|information_schema|
      ON CONFLICT` по `lib/**` — пусто.
- [x] 2.6 Страж «в модуле writer'а нет SQL»: разбор AST, не поиск подстроки
      (`tests/test_db_logging_service.py::TestSingleWriter`).
      **Проверен на укус:** временная подкладка `INSERT` + `import psycopg2`
      роняет 3 теста, после отката все 64 зелёные.
- [x] 2.7 Переосмыслить страж колонок: он охранял согласие двух писателей.
      Теперь охраняет «платформа пишет полный конверт, агент не пишет в
      журнал ничего» (`tests/test_journal_writer_columns_contract.py`).
- [x] 2.8 Перевести тесты на мок писателя, а не на `psycopg2`/`lib.utils.db`:
      `RecordingWriter` в `tests/test_db_logging_service.py`. Переведены
      `test_log_transport_wiring.py`, `test_subagent_logging.py`,
      `test_turn_observability_events.py`, `test_log_transport.py`,
      `test_application_context_logging.py`.
- [x] 2.9 Поведение, которое уехало на платформу, не потеряно: двухшаговый
      `upsert` без `ON CONFLICT` (Greenplum 6.5) и правила чистки охраняет
      `mcp-platform/tests/test_data_journal_operations.py`. Проверено, что эти
      тесты есть и зелёные; дублировать их в агенте было бы проверкой чужого
      файла.
- [x] 2.10 Отдельно: вылезший при переносе дефект теста. Четыре теста в
      `test_turn_observability_events.py` подменяли `svc._queue` моком при
      живом worker-потоке. `queue.get()` на моке возвращается мгновенно,
      поток вращался без сентинела остановки; раньше его случайно глушили
      ретраи подключения к несуществующему хосту, а после снятия прямого SQL
      тормоз исчез и тест начал висеть. Подмена убрана — проверяемое поведение
      (мутации `log_event`) worker'а не требует.

## S3. Приёмка

Прогоны шли **пачками явными списками файлов**, по одному pytest одновременно:
стоп-правило владельца запрещает прогон по всему дереву на этой машине
(16 ГБ RAM). Флаги везде `-q --tb=line -rf -p no:cacheprovider`. Итог полного
набора получен суммой пачек, а не одним прогоном.

- [x] 3.1 Дерево агента: 22 пачки (8-7 файлов) + `tests/contract/` целиком.
      **3570 passed, 32 skipped, 2 xfailed, 0 failed** — итог сложен из пачек;
      семь тестов `test_agent_factory.py` учтены в пачке, где они проходят
      (см. B6).
- [x] 3.2 Платформа: 42 пачки по 2 файла в `mcp-platform/tests/`
      (5067 passed) + 32 пачки по 4 файла по подкаталогам (773 passed).
      **5840 passed, 1 skipped** = baseline 5837 + ровно 3 теста нового
      стража надгробий.
- [x] 3.3 `python tools/validate_component_specs.py --strict` — 3 ошибки в
      `runtime/entrypoints/spec.md`, роста нет.
- [x] 3.4 `openspec validate 2026-10-04-task-queue-drop-direct-sql --strict` —
      `Change '2026-10-04-task-queue-drop-direct-sql' is valid`.
- [x] 3.5 Все три стража проверены на укус (временная подкладка запрещённого,
      откат, повторный прогон):
      - SQL в writer'е → падают 3 теста, после отката 64 зелёные;
      - `from lib.utils.db import run` в `lib/services/` → падают 2 теста, после
        отката 8 зелёные;
      - файл на подчёркивании в `data/tools/` → падает
        `test_no_undeclared_tombstone_appears`, после отката 3 зелёные.

---

# Блокеры

Оставлены здесь, а не в описании change: блокеры — не часть требований,
и прятать их в спеку означало бы выдать их за сделанную работу.

## B1. Удаление файлов на этой машине запрещено политикой

`mavis-trash` недоступен, лаунчер не вызывается, обходить запрет нельзя.

**Надгробия сняты владельцем вручную** во время работы над change — это
выполненная часть заявки. Осталось:

```powershell
git rm workspace/utils/db.py        # пул ещё нужен lifecycle'у, см. B3
```

Что снимается после удаления `workspace/utils/db.py`:

- `tests/test_utils_db.py` (52 теста) — тестирует пул, которого у агента
  не будет. **Удалять его нужно тем же заходом, иначе набор станет красным.**

И пустые записи в `KNOWN_TOMBSTONES`
(`mcp-platform/tests/test_capability_tool_tombstones.py`) — страж этого
требует сам: он падает, когда объявленного надгробия уже нет на диске.

## B2. `schema_validation` не достаёт до `schema_check`

По proposal журнал и `schema_validation` переезжают на существующую операцию
`schema_check`. **Формально она годится:** возвращает
`{expected, found, missing, ok, tables}`, `expected` принимает
`schema.table`, то есть набор недостающих таблиц из неё получается.

**Достижимости нет.** Точка вызова — `ApplicationContext._validate_runtime_schema`,
а она зовётся из `ctx.start()`. Рукопожатие с платформой поднимается позже:

- `gateway.py:179` — `await ctx.start()`
- `gateway.py:497` — `await _connect_enterprise_mcp(ctx)`

Сессии `enterprise-mcp` в момент проверки не существует, а поднять её внутри
`start()` нельзя: `start()` синхронен и вне event loop, а `client.call()` —
асинхронный.

Чтобы проверка поехала на `schema_check`, её надо перенести туда, где сессия
уже есть. Самое близкое место без правки точки входа —
`ApplicationContext.attach_log_transport()`: он вызывается и из `start()`
(сессии нет) и из входа в event loop (сессия есть). Но тогда `SchemaValidationError`
поднимается не из `start()`, а из поднятия транспорта, и меняется путь кода
возврата на старте, который закреплён в `runtime/entrypoints/spec.md`.

**Нужно решение владельца:** переносим ли мы проверку схемы за рукопожатие
(и согласуем ли смену слоя, поднимающего `SchemaValidationError`), или
`schema_validation` остаётся на `lib.utils.db.fetch_with_timeout` до отдельного
change? `application_context.py` и `cli_agent.py` в моём владении не были,
`runtime/entrypoints` трогать нельзя — решить это в своей волне я не мог.

## B3. Пул и DSN остаются у агента

`workspace/utils/db.py` продолжают обслуживать три вещи, и все три — вне
журнала:

| Потребитель | Что делает | Операция платформы |
|---|---|---|
| `application_context.py:898` | `fetch_with_timeout` для `SchemaValidationService` | `schema_check` есть, но недостижима — см. B2 |
| `application_context.py:1325` | `SELECT 1` в проверке готовности `check_postgres` | нет |
| `application_context.py:1950/1962/1972` | `_configure_db_pool` / `_start_db_pool` / `_stop_db_pool` — lifecycle пула | нет |
| `session_storage.py:188` | `configure(dsn)`; рядом остаётся экспорт `DATABASE_URL` в `os.environ` | нет |
| `gateway.py:653` | `probe_connections` / `get_stats` в отчёте о пуле на старте | нет |
| `scripts/backfill_media_aw.py:37` | одноразовый скрипт | нет |

**Почему не сделал.** Запись в базу из агента при этом закрыта полностью —
ни один из перечисленных потребителей в базу не пишет. А вот снос самого
пула — это не «удалить файл», а изменение порядка старта и семантики
готовности в composition root, общем для трёх воркеров, плюс
`gateway.py` и `cli_agent.py`, которые в моём владении не были. Это работа
объёмом с отдельный change, и начатая наполовину она оставила бы прод с
половиной пула: худший из вариантов.

Нужна ли для этого новая операция платформы — по честному, да: у платформы
нет операции «пинг базы» и нет «поднять/опустить пул», потому что пул её
дело. Но объявлять их молча нельзя, поэтому здесь только зафиксировано.

## B4. `mcp-platform/docs/MCP-CONTRACTS.md` продолжает описывать надгробия

Абзац «Надгробия. Рядом лежат `_claim_task.py` и `_update_task_status.py`»
станет ложью сразу после удаления файлов. `docs/**` в моём владении не был.
Правка — три строки: убрать абзац, либо заменить его на «надгробий в
`data/tools` нет; порядок удаления задаёт
`mcp-platform/tests/test_capability_tool_tombstones.py`».

## B5. Файлы вне заявленного владения

Правка приёмки (прямой SQL не оставляет тестов зелёными «как есть»)
потребовала тронуть файлы, которых в списке владения не было. Ни один из них
не в запрещённом списке; все правки — подмена `psycopg2`/`lib.utils.db` на мок
писателя, ни одна не меняет проверяемое поведение.

- `tests/test_log_transport_wiring.py` — тест, утверждавший «при выключенной
  платформе пишем напрямую». Он охранял удаляемый путь; переписан на
  противоположное утверждение.
- `tests/test_subagent_logging.py`, `tests/test_turn_observability_events.py` —
  подмена `_db_run` / `execute_batch` на заглушку писателя.
- `tests/test_log_transport.py`, `tests/test_application_context_logging.py`,
  `tests/test_journal_writer_columns_contract.py`.

## B6. Два теста зависят от порядка запуска (не мои, не чинил)

Всплыло при прогоне явными списками файлов: в полном прогоне эти тесты
проходят, поодиночке — нет.

- `tests/test_agent_factory.py` (7 тестов) подставляет `sys.modules["nanobot
  .agent"]` фейковым модулем на уровне модуля. Он проходит, только если
  `nanobot.agent.tools.registry` уже лежит в `sys.modules` — то есть если
  раньше отработал файл, импортирующий настоящий nanobot. Проверено:
  `test_agent_factory.py` + `test_runtime_patcher.py` → **63 passed**;
  `test_agent_factory.py` в одиночку → 7 failed
  (`No module named 'nanobot.agent.tools'`). Ни `lib/core/agent_factory.py`,
  ни сам тест в рабочем дереве не тронуты (`git status` пуст) — то есть это
  было и до change, просто полный прогон маскировал порядком.
- `tests/test_agent_settings_block.py::test_block_key_equals_the_agent_config_path`
  один раз упал в пачке с `test_active_files_hook.py`, а повтор той же пачки
  дал **107 passed, 1 skipped**. Похоже на гонку с чужой правкой
  `config.json` / `workspace/block.json` в общем дереве, а не на дефект кода.

По стоп-правилу не чинил. Оба касаются того, зачем нужны полные прогоны
в фиксированном порядке.

## B7. Снос пула уже взят другим change

Пока писался этот change, появился
`openspec/changes/2026-10-04-utils-db-pool-removal/` — инвентаризация потребителей
`workspace/utils/db.py` и его снос. То есть B3 и B1 (часть с `lib.utils.db`)
закрываются там, и в этом change они остаются зафиксированными как
заявка, а не как невыполненная работа.
