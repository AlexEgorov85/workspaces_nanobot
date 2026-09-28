## Context

См. `proposal.md` — почему нужна унификация. Здесь — технические решения для реализации.

Текущая архитектура:
- `ApplicationContext.create()` принимает 5 `enable_*` kwargs, CLI и gateway передают их по-разному (см. `lib/core/application_context.py:82-89`).
- `cli_agent.py` через `lib/cli/console_loop.py` напрямую публикует в in-memory `MessageBus` (`bus.publish_inbound(InboundMessage(channel="cli", ...))`) и читает из `bus.consume_outbound()`.
- `gateway.py` создаёт `PostgresChannel` через `ChannelFactory`, который полл-ит `agent_conversation_messages`, клеймит задачи через `agent_worker_claims`, и пишет outbound обратно в таблицу.
- `streamlit_app.py` спавнится из `gateway.py` через `SubprocessManager.spawn_streamlit`.

## Goals / Non-Goals

**Goals:**
- `ApplicationContext.create()` имеет единую сигнатуру для CLI и gateway; все `enable_*` читаются из конфига.
- CLI использует тот же `PostgresChannel` (через worker pool), что и gateway; различие только в рендерере.
- Audit-sync producer/consumer модель: gateway пишет `cache.duckdb`, CLI пишет `cli.duckdb`.
- Streamlit удалён полностью (subprocess spawn, упоминания, `streamlit_app.py`).

**Non-Goals:**
- Изменение `nanobot.MessageBus` или `nanobot.AgentLoop`.
- Изменение схемы `agent_messages` / `agent_worker_claims`.
- Полный отказ от in-memory bus — `agent.run()` остаётся подписан на bus.
- Удаление WebSocket-канала как транспорта (WebSocket-клиенты по-прежнему принимают задачи через gateway).
- Изменение `gateway.compact.*` / других `gateway.*` ключей, не упомянутых в proposal.
- Рефакторинг `nanobot.channels.base.BaseChannel`.

## Decisions

### D1. `ApplicationContext.create()` — единая сигнатура + `role` параметр

**Решение:** Параметры `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls` помечаются deprecated в kwargs, читаются из `SETTINGS["gateway"].*` если kwargs не передан. Новый обязательный kwarg `role: Literal["gateway", "cli", "utility"] = "gateway"` (default для обратной совместимости). `role` используется для:
- `resolve_publish_path(role=role)` — выбор snapshot-пути (см. D4);
- логирования «process role: cli» в startup-баннере;
- (опционально) разной инициализации PostgresChannel (разный `worker_id`).

**Альтернативы:**
- Полностью удалить `enable_*` kwargs в одном релизе — отвергнуто: 30+ существующих тестов и интеграционных скриптов (`tools/build_vectors.py`, `tools/check_worker_pool_integrity.py` и др.) их передают. Breaking change без transitional period нарушает backward compatibility contract.
- Не вводить `role`, а определять по argv — отвергнуто: `ApplicationContext` не должен знать о CLI/gateway entrypoint; явный параметр делает контракт прозрачным.

### D2. CLI использует существующий `PostgresChannel`

**Решение:** CLI запускает `PostgresChannel` (через `ChannelFactory`) точно так же, как gateway, с `worker_id="cli_<pid>"` и `chat_id_pattern="cli:*"`. Внутри CLI-процесса `PostgresChannel` уже подписан на in-memory `MessageBus`, поэтому `agent.run()` в том же процессе подхватывает задачи через bus (как и в gateway). Никаких новых классов не требуется.

CLI REPL:
- На user-input: `INSERT INTO agent_messages` напрямую через `utils.db.execute(...)` со `status='pending'`, `channel='cli'`, `chat_id='cli:<session>'`. Это имитирует работу web-сервера (HTTP-handler Streamlit делает то же).
- На output: `bus.consume_outbound()` с фильтром по `_final_turn=True` (уже реализовано в текущем `console_loop.py::consume_outbound`). Это работает, потому что `agent.publish_outbound()` идёт в тот же in-memory bus, что и в gateway.

**Альтернативы:**
- Новый `TerminalChannel` класс, обёртывающий `PostgresChannel` с упрощённым API — отвергнуто: лишняя сущность без собственной ответственности. REPL может напрямую INSERT'ить (это просто SQL) и читать из bus.
- CLI REPL читает outbound из `agent_messages` таблицы (как gateway web-handler) — отвергнуто: REPL находится в том же процессе, что агент, in-memory bus быстрее (~1ms vs ~50ms DB poll). Metadata для typewriter/reasoning/tool-events доступна только через bus (PostgresChannel.send() пишет в таблицу только финальные сообщения).

### D3. `resolve_publish_path(role)` — разные snapshot-пути

**Решение:** `resolve_publish_path(workspace_path, cache_cfg, *, role="gateway")` принимает параметр `role`. При `role="cli"` возвращает `<local_path>/cli.duckdb`. При `role="gateway"` или `role="utility"` — `<local_path>/cache.duckdb` (текущий).

CLI skills (`audit_analyzer`, `legal_summarizer`) и `CacheProvider` (через `lib/services/cache_provider_impl.py`) получают `role="cli"` через `_make_cache_provider(role=...)` → `PostgresDuckDbProvider(role="cli")` → открывает `<local_path>/cli.duckdb`.

**Альтернативы:**
- Единый snapshot `cache.duckdb` для CLI и gateway — отвергнуто: DuckDB ATTACH берёт эксклюзивный flock; одновременная работа двух процессов вызывает race condition.
- Lockfile на уровне ОС (flock через `fcntl`) — отвергнуто: NFS не поддерживает; усложняет запуск на разных FS.

### D4. Удаление Streamlit — единым коммитом

**Решение:**
- Удаляются файлы: `streamlit_app.py`, упоминания в `SubprocessManager` (`spawn_streamlit` метод удаляется; если модуль имеет другие методы — модуль сохраняется, метод удаляется).
- Удаляются упоминания: `gateway._streamlit_enabled()`, `gateway.py::_run` блок `if _streamlit_enabled() and subprocess_manager.spawn_streamlit(...)`, импорт `SubprocessManager` в gateway.
- `streamlit.*` секция в `project.json` — оставляется как permissive (pydantic extra="allow"), но runtime её игнорирует.
- `tests/test_streamlit_*` — удаляются (если есть).
- `AGENTS.md` секция «Project Layout» — обновляется: убирается упоминание `streamlit_app.py`.
- `README.md` — обновляется упоминание deployment-опций (Streamlit удаляется).
- `docs/INTERNAL_API.md` — удаляется секция «Streamlit».

**Альтернативы:**
- Deprecation period (флаг `streamlit.enabled=true` → warning, через MINOR удаляется) — отвергнуто: пользователь явно сказал «удалить полностью».
- Сохранить `streamlit_app.py` как legacy — отвергнуто: 2190 строк неиспользуемого кода = технический долг.

### D5. `_check_websocket_port_available` остаётся в gateway

**Решение:** WebSocket port check — server-only pre-startup проверка. Остаётся в `gateway.py::_entrypoint_main` ПОСЛЕ `_report_db_pool_startup` и ДО `GatewayRunner().run_forever(...)`. CLI НЕ выполняет эту проверку (нет WS-сервера, нет порта для проверки).

**Альтернативы:**
- Перенести в `ApplicationContext.start()` — отвергнуто: `ApplicationContext` не должен знать о транспортах (transport-specific check). Gateway вызывает явно.
- Удалить — отвергнуто: проверка ловит реальный баг (висящий процесс после kill -9), который ломает startup на полпути.

### D6. `--storage` в CLI: `auto`/`postgres`/`file`

**Решение:** `--storage=auto` (default) → `storage_override=None` → `SessionStorageService` решает автоматически (postgres если доступен, иначе file). `--storage=postgres` → явно postgres-storage. `--storage=file` → file-storage, CLI работает offline без `agent_messages` (всё через локальный session-файл).

Семантика меняется для случая `--storage=postgres`: CLI подключается к PostgresChannel и публикует через таблицу (как gateway). В default `auto` поведение остаётся совместимым (postgres если есть).

**Альтернативы:**
- Удалить `--storage` — отвергнуто: пользователь явно использует `--storage=file` для offline-режима.
- `--storage=postgres` → только PostgresChannel, не SessionStorage — отвергнуто: SessionStorage (PGSessionManager) и PostgresChannel используют разные таблицы (`agent_session_meta` vs `agent_conversation_messages`); оба нужны одновременно.

### D7. `/compact` в CLI — локальный shortcut

**Решение:** `lib/cli/console_loop.py::_run_cli_compact` остаётся без изменений. CLI вызывает `ContextCompactionService.compact(session_key="cli:<session>", idle=True, force=True)` напрямую. Это shortcut, не маршрутизация через `agent_messages` (компакт-команда не требует обработки LLM).

**Альтернативы:**
- `/compact` через `agent_messages` (как gateway slash-команды) — отвергнуто: `/compact` не нуждается в LLM-итерациях; direct-вызов сервиса быстрее и атомарнее.

### D9. Тесты — backward compatibility и новые сценарии

**Решение:**
- Существующие тесты, вызывающие `ApplicationContext.create(enable_audit=True, ...)` — продолжают работать (deprecated kwargs поддерживаются).
- Новые тесты:
  - `tests/test_application_context.py` — добавляются сценарии: `create(role="cli")` vs `create(role="gateway")` создают идентичный набор сервисов (кроме `role`-зависимых).
  - `tests/test_cli_over_postgres_channel.py` (новый) — интеграционный сценарий: CLI процесс → INSERT в `agent_messages` → воркер забирает → агент обрабатывает → outbound в bus → REPL читает → рендерится.
  - `tests/test_cache_provider_role_paths.py` (новый) — `resolve_publish_path(role="cli") != resolve_publish_path(role="gateway")`.
  - `tests/test_streamlit_removed.py` (новый) — `grep` проверяет, что `streamlit_app.py` / `SubprocessManager` не импортируются в runtime-коде.

**Альтернативы:**
- Удалить все тесты, использующие deprecated kwargs — отвергнуто: ломает CI.

## Risks / Trade-offs

- **[Risk]** Существующие standalone-утилиты (`tools/build_vectors.py`, `tools/check_worker_pool_integrity.py`) вызывают `ApplicationContext.create(enable_audit=True/False, ...)` напрямую. Если забыть добавить `role="utility"` — snapshot пишется в `cache.duckdb` (default `role="gateway"`). → **Mitigation:** `role="utility"` default для standalone-утилит (явно передаётся); проверка в tasks.md.
- **[Risk]** Если CLI и gateway запущены одновременно и оба пишут в один snapshot (баг реализации) — race condition на DuckDB flock, оба snapshot'а corrupted. → **Mitigation:** задача в tasks.md «integration test: параллельная запись CLI + gateway → оба snapshot'а валидны, не совпадают».
- **[Risk]** Deprecated-период для `enable_*` kwargs затягивается, разработчики забывают удалить в следующем MINOR. → **Mitigation:** явная задача в tasks.md с конкретным MINOR-релизом (после раскрытия текущего MINOR).
- **[Risk]** REPL читает outbound из bus, но если agent в CLI не запустился — REPL зависает на `consume_outbound()`. → **Mitigation:** существующий `repl_idle_timeout_sec=1.0` уже обрабатывает (REPL выходит через Ctrl+C).
- **[Risk]** Удаление Streamlit ломает существующие deployment'ы, которые полагаются на `streamlit run`. → **Mitigation:** CHANGELOG.md BREAKING в категории `Removed`. Это сознательное решение, документированное в proposal.
- **[Risk]** Изменение `--storage=postgres` семантики ломает скрипты, которые использовали `--storage=postgres` для выбора storage, не для transport. → **Mitigation:** существующая семантика `--storage=postgres` уже означает «использовать postgres storage» (это поведение `SessionStorageService`). CLI просто теперь дополнительно использует `PostgresChannel` для transport (это ортогональные concerns). Тесты в `tests/test_cli_storage_modes.py` подтверждают, что storage и transport не путаются.
- **[Risk]** `_check_websocket_port_available` нужен только если WebSocket-канал включён. Если WebSocket отключён в конфиге, проверка всё равно выполняется. → **Mitigation:** обёрнуть в `if ctx.config.channels.websocket.enabled:`, не делать unconditional check. (Текущее поведение — всегда проверяет, что упрощает startup-последовательность.)

## Migration Plan

**Шаг 1 (текущий MINOR):** Реализация всех решений, deprecated-период для `enable_*` kwargs, Streamlit удаляется, `--storage=postgres` меняет семантику, тесты обновляются. DeprecationWarning в логи.

**Шаг 2 (следующий MINOR):** Удаление deprecated `enable_*` kwargs из `ApplicationContext.create()` (после deprecation period). Все вызовы мигрируют на `role=` kwarg.

**Шаг 3 (если потребуется):** Рефакторинг `PostgresChannel` для поддержки явного `chat_id_pattern` (сейчас он claim'ит все). Это отдельный OpenSpec change, не блокирует текущий.

**Rollback:** Если после раскрытия обнаружена критическая регрессия — revert на коммит до раскрытия. DeprecationWarning в текущем MINOR даёт операторам время заметить проблему.

## Open Questions

- Нужно ли поддержать `role="utility"` явно, или default `role="gateway"` подходит для standalone-утилит? — resolved в D9 (explicit `role="utility"` для утилит).
- Должен ли CLI preload FAISS-индексы синхронно (блокируя startup) или асинхронно (как gateway)? — решено: асинхронно, как в gateway (`asyncio.create_task(_preload_and_report)`).
- Что делать, если `agent_messages` имеет `chat_id_pattern` фильтр (т.е. нельзя claim'ить задачи другого процесса)? Текущая JVM-логика — claim'ит все задачи. Это OK для unified pool (worker pool arbitrate).