## Why

Сейчас `cli_agent.py` и `gateway.py` запускают `ApplicationContext.create(...)` с **разными параметрами** (`enable_audit`, `enable_cron`, `print_llm_calls`, `storage_override`, `session_override`), и каждая точка входа по-своему публикует сообщения агента: CLI — напрямую в in-memory `MessageBus` (`bus.publish_inbound`), gateway — через `PostgresChannel` (worker pool, таблица `agent_messages`). Это значит, что:

- поведение агента (LLM, tools, skill'ы, runtime-патчи, хуки, сессии) на самом деле **одинаковое**, но его конфигурация размазана по двум entrypoint'ам;
- CLI не видит сообщения, прилетающие в gateway, и наоборот — нельзя, например, запустить задачу через HTTP и подхватить ответ в REPL, или наоборот;
- `ApplicationContext.create()` принимает `enable_*`-флаги как constructor-args, хотя они должны быть config-driven, а не различием между точками входа;
- инфраструктурные компоненты (Streamlit-сабпроцесс, проверка занятости WebSocket-порта) живут в gateway и не имеют смысла для CLI, но их присутствие делает gateway «толще» than a pure transport.

Цель change: **CLI и gateway отличаются только рендерером и сервисами, которые требуются серверу**. Один и тот же `ApplicationContext` (без `enable_*`-флагов в сигнатуре — они читаются из конфига), один и тот же transport-механизм (`PostgresChannel` через таблицу `agent_messages`), один и тот же пул воркеров.

## What Changes

- **`ApplicationContext.create()` принимает `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls`, `storage_override`, `session_override` через единый интерфейс** — все `enable_*` флаги **читаются из `SETTINGS["gateway"].*`** внутри самой фабрики (не из kwargs). CLI и gateway вызывают `ApplicationContext.create(...)` с **одной и той же сигнатурой** (только runtime-флаги, специфичные для CLI: `--storage`, `--session`).

  - `gateway.enable_db_logging` (default `true`)
  - `gateway.enable_audit` (default `true`) — **поведение зависит от роли процесса** (см. ниже «producer/consumer модель»)
  - `gateway.enable_cron` (default `true`)
  - `gateway.print_llm_calls` (default `false`)
  - CLI-специфичные runtime-флаги (остаются kwargs): `storage_override` (`--storage`), `session_override` (`--session`).

  *Сигнатура `create()` поэтапно сужается:* параметры `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls` помечаются как **deprecated** в kwargs (читают default из конфига, но поддерживаются для обратной совместимости с тестами и интеграционным кодом) и **удаляются в следующем MINOR релизе** после раскрытия.

- **CLI публикует сообщения через `PostgresChannel`**, а не напрямую в `MessageBus`. Внутри процесса `MessageBus` остаётся для работы `agent.run()` (он подписан на bus), но **input** идёт через таблицу `agent_messages` (через `PostgresChannel.publish_inbound`-обёртку, совместимую с worker pool), а **output** — через ту же таблицу (CLI подписан на outbound-строки, фильтрует по `chat_id`, рисует typewriter).

  Это означает, что `lib/cli/console_loop.py::run_repl` больше не зовёт `bus.publish_inbound(InboundMessage(channel="cli", ...))` напрямую; вместо этого используется единый `Channel` API (`start_all` / `publish_inbound` / `consume_outbound`), тот же, что в `lib/channels/postgres_channel.py`.

- **`gateway.py` больше не спавнит `streamlit_app.py`** как subprocess. Удаляется `lib/services/subprocess_manager.py::spawn_streamlit` (или весь метод), `gateway.py::_streamlit_enabled()`, упоминания `streamlit_app.py` в `AGENTS.md`, `README.md`, `streamlit.*`-секция в `project.json` (если она существует), `streamlit_app.py` сам файл, тесты, ссылающиеся на Streamlit-путь. Если в `project.json` есть `streamlit.enabled=true`, конфиг игнорируется, но **настройка не валидируется как unknown** (pydantic остаётся permissive для legacy-ключей).

- **CLI-флаг `--storage` остаётся**, но меняет семантику: `--storage=file` означает «не подключаться к PostgresChannel, писать ответы в локальный файл сессии» (это offline-режим, а не выбор PG-storage). `--storage=postgres` (по умолчанию) — обычный режим через таблицу.

- **`/compact` в CLI** продолжает работать: `lib/cli/console_loop.py::_run_cli_compact` остаётся, но вместо прямого `agent.compact_idle_session(...)` использует `ContextCompactionService` через ту же шину (это уже так после change `runtime-patcher-composition-cleanup`).

- **Audit sync (PG → DuckDB): producer/consumer модель.** Когда `enable_audit=True` И `storage_mode=="postgres"`:
  - Если процесс — **gateway**: запускается `PgDuckDbSyncService`, snapshot публикуется в `gateway.cache.local_path` (или default `~/.cache/nanobot/duckdb/cache.duckdb`);
  - Если процесс — **CLI**: запускается `PgDuckDbSyncService`, но snapshot идёт в **отдельный путь** `~/.cache/nanobot/duckdb/cli.duckdb`, чтобы избежать конфликта блокировок DuckDB, если CLI и gateway запущены одновременно;
  - Если процесс — **standalone utility** (`enable_audit=True`, но не gateway/CLI) — путь по умолчанию тот же, что у gateway.

  Решение о том, «кто продьюсер, кто консьюмер», принимается **по типу процесса** (gateway/CLI/utility), а не по пользовательскому вводу. CLI и gateway **никогда не пишут в один файл**.

- **Worker pool — общий.** Когда запущен и CLI, и gateway, любой воркер может забрать любую задачу из `agent_worker_claims`. Это даёт graceful degradation: если gateway упал, CLI подхватывает его очередь (chat_id `gateway:*` тоже обрабатывается, но CLI рендерит только `cli:*`).

- **WebSocket port check (`gateway.py::_check_websocket_port_available`) остаётся в gateway.** Это server-only проверка «не висит ли предыдущий процесс», она не имеет смысла для CLI.

## Capabilities

### New Capabilities

- `runtime/entrypoints`: контракт application entrypoint'ов (`cli_agent.py`, `gateway.py`). Описывает: единая сигнатура `ApplicationContext.create()` (без `enable_*`-kwargs), единый transport через `PostgresChannel` для обоих, разделение «renderer» (CLI = typewriter, gateway = нет) и «server-only services» (WebSocket port check, Streamlit — удалён). Producer/consumer модель для audit-sync (gateway = writer `cache.duckdb`, CLI = writer `cli.duckdb`). Это контракт, который должны соблюдать **оба** entrypoint'а и любые будущие точки входа.

### Modified Capabilities

- `runtime/context`: добавляется требование «`ApplicationContext.create()` MUST НЕ ДОЛЖЕН принимать `enable_*`-параметры в публичной сигнатуре; все runtime-флаги читаются из `SETTINGS["gateway"].*` внутри фабрики. Deprecated alias-параметры допустимы только как compatibility boundary, с явным deprecation marker и плановым удалением». Это формализует единую точку конфигурации.

- `data/cache-provider`: добавляется требование «`PostgresDuckDbProvider` (или эквивалент) MUST ДОЛЖЕН резолвить snapshot-путь через `resolve_publish_path(role: Literal["gateway","cli","utility"])`, где `role` определяется по типу процесса. Разные `role` MUST возвращать **разные пути** для избежания конфликта DuckDB flock». Это формализует producer/consumer модель для audit-sync.

## Impact

- `lib/core/application_context.py`:
  - Параметры `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls` помечаются как deprecated в kwargs;
  - Если kwarg передан — он используется (compat behavior);
  - Если kwarg НЕ передан — читается `SETTINGS["gateway"].<flag>` (новый default behavior);
  - Новая kwargs `role: Literal["gateway","cli","utility"] = "gateway"` (default для обратной совместимости). Используется `resolve_publish_path(role=role)` и для разделения producer-путей.

- `gateway.py`:
  - Удаляется блок `_streamlit_enabled()` + `subprocess_manager.spawn_streamlit(...)` + импорт `SubprocessManager`;
  - Удаляется `lib/services/subprocess_manager.py` (или метод, если модуль используется ещё где-то);
  - Удаляется блок `_check_websocket_port_available` (или переносится в отдельный helper, который зовется явно из `gateway.py` — server-only check);
  - `_entrypoint_main(...)` больше **не передаёт** `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls` в `ApplicationContext.create(...)`;
  - Передаёт `role="gateway"` явно (или `role` определяется автоматически по argv).

- `cli_agent.py`:
  - `_run_vanilla` / `_run_patched` больше **не передают** `enable_*`-флаги в `ApplicationContext.create(...)`;
  - Передают `role="cli"` явно;
  - `lib/cli/console_loop.py::run_repl` переписывается: вместо `bus.publish_inbound(InboundMessage(channel="cli", ...))` используется `cli_channel.publish_inbound(...)`, где `cli_channel = PostgresChannel(...)` (новый инстанс с теми же настройками, что у gateway).

- `lib/cli/console_loop.py`:
  - `run_repl` принимает дополнительный параметр `channel: Channel` (или подобный интерфейс);
  - Внутри `run_repl` подписывается на `channel.consume_outbound()` вместо `bus.consume_outbound()`;
  - Внутри REPL-цикла `bus.publish_inbound(...)` заменяется на `channel.publish_inbound(...)`;
  - Slash-команда `/compact` остаётся в `run_repl`, но не вызывает `agent` напрямую — отправляет специальное управляющее сообщение через `channel` (либо остаётся как локальный shortcut, см. design.md).

- `lib/channels/postgres_channel.py`:
  - Проверяется, что `publish_inbound` совместим с уже существующим `InboundMessage`-контрактом. Если нет — добавляется wrapper/Adapter;
  - Worker pool: `agent_worker_claims` уже принимает задачи от любого `chat_id` (нет фильтрации), так что CLI может подхватывать задачи gateway. Это уже работает, никаких изменений не нужно.

- `lib/services/cache_provider_impl.py` / `resolve_publish_path`:
  - `resolve_publish_path(workspace_path, cache_cfg, role="gateway")` — добавляется параметр `role`;
  - `role="cli"` возвращает `<local_path>/cli.duckdb` (или эквивалент);
  - `role="gateway"` и `role="utility"` возвращают `<local_path>/cache.duckdb`;
  - CLI-skills (`audit_analyzer`, `legal_summarizer`) используют `role="cli"` при чтении snapshot — это читает **свой** snapshot, который CLI сам и обновил.

- `lib/services/preload_service.py`:
  - Если процесс — CLI, preload FAISS-индексов для CLI-snapshot. Сейчас это делает только gateway; теперь делает и CLI (если `enable_audit=True`).

- `tests/`:
  - `tests/test_application_context.py` — обновляется под новые defaults (без `enable_*` в kwargs → читает из конфига);
  - `tests/test_cache_provider.py` (если есть) — добавляются сценарии `role="cli"` vs `role="gateway"`;
  - `tests/test_storage_hybridization.py` — без изменений (контракт о таблице не меняется);
  - Новый `tests/test_cli_over_postgres_channel.py` — интеграционный сценарий: CLI публикует сообщение → воркер забирает → CLI читает ответ → рендерит;
  - Существующие тесты, которые вызывают `ApplicationContext.create(enable_audit=True/False)`, продолжают работать (deprecated kwargs поддерживаются).

- Документация:
  - `AGENTS.md` (этот файл) — обновляется секция «Project Layout»: убирается упоминание `streamlit_app.py`, SubprocessManager; добавляется упоминание `TerminalChannel` (новый модуль);
  - `README.md` — обновляется упоминание Streamlit (удаляется как deployment-опция);
  - `docs/ARCHITECTURE.md` — секция «Запуск CLI / gateway» переписывается под «entrypoint contracts»;
  - `docs/INTERNAL_API.md` — удаляется секция про Streamlit, добавляется секция «Producer/Consumer для audit sync»;
  - `CHANGELOG.md` — категория `Changed`: BREAKING (Streamlit удалён; `--storage=file` меняет семантику; `--storage=postgres` теперь default в CLI); категория `Added`: producer/consumer snapshot для CLI;
  - `docs/PROFILES.md` — без изменений (профили не затронуты).

- **Streamlit**: `streamlit_app.py`, `subprocess_manager.spawn_streamlit`, `gateway._streamlit_enabled`, тесты, ссылающиеся на streamlit-путь, упоминания в AGENTS.md/README — удаляются. `streamlit.*` в `project.json` игнорируется runtime, но не валидируется как unknown (pydantic permissive).

## Open Questions

Переносятся в `design.md` (не блокируют proposal):

1. **TerminalChannel как отдельный класс или просто инстанс `PostgresChannel` с `chat_id="cli"`?** — design определит, существует ли отдельная обёртка или достаточно одного класса.

2. **`/compact` в CLI: shortcut через шину или отдельный control-flow?** — сейчас CLI ловит `/compact` локально через `_run_cli_compact`. Через таблицу это станет либо локальным shortcut'ом (вызов `ContextCompactionService` напрямую из CLI), либо маршрутизированной slash-командой (как в gateway, через `RuntimePatcher.patch_compact_command`).

3. **Preload FAISS-индексов в CLI: блокирующий startup-step или фоновый?** — gateway делает preload в фоне (`asyncio.create_task(_preload_and_report)`). В CLI пользователь уже ждёт, можно блокировать.

4. **`session_override` (`--session`) в CLI: сохраняется ли в `agent_messages` как `chat_id=cli:<session>`?** — нужно подтвердить, что CLI REPL пишет в `chat_id=cli:<session>` так же, как сейчас пишет в bus.

5. **CLI без gateway: работает ли `audit_analyzer`?** — CLI пишет свой snapshot, skills читают его. Если CLI запущен один — всё работает. Если CLI+gateway — каждый читает свой snapshot. Подтвердить, что это OK.

## Что принципиально НЕ делается в этом change

- Полный отказ от `enable_*` kwargs `ApplicationContext.create()` (deprecated-период сохраняется, чтобы не сломать существующие тесты).
- Изменение `PostgresChannel` worker pool API (он уже принимает задачи от любого `chat_id`).
- Изменение `nanobot.MessageBus` (CLI больше не использует bus напрямую для input/output, но `agent.run()` внутри процесса по-прежнему подписан на bus через `AgentLoop`).
- Рефакторинг `lib/services/subprocess_manager.py` (если у него есть другие методы, кроме `spawn_streamlit`).
- Изменение контракта `bus.publish_inbound` / `bus.consume_outbound` — это upstream `nanobot`, не наш слой.
- Изменение схемы `agent_messages` / `agent_worker_claims` / `agent_conversation_messages`.
- Полный отказ от in-memory bus (CLI использует bus для `agent.run()`, просто input/output идут мимо).
- Удаление `streamlit_app.py` без периода deprecated (если в репо есть ссылки — сначала переход на «ignore flag», потом удаление в MINOR).
- Изменение `gateway.compact.*` / `gateway.compact.notify_in_history` / других существующих `gateway.*` ключей, не упомянутых выше.