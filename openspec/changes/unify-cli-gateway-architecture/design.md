## Context

См. `proposal.md` — почему нужна унификация. Здесь — технические решения.

Текущая архитектура (фактическая):

```text
                              ApplicationContext.create(...)
                                       │
                  ┌────────────────────┴────────────────────┐
                  │                                          │
            cli_agent.py                              gateway.py
        (enable_audit=False,                      (enable_audit=True,
         enable_cron=True)                          enable_cron=False)
                  │                                          │
        bus.publish_inbound(                     PostgresChannel (worker pool)
        InboundMessage(channel="cli"...))        bus.publish_inbound(
                  │                              InboundMessage(channel="postgres"...))
                  └──────────┬───────────────────────────────────┘
                             │
                          AgentLoop (общий)
                             │
                       MessageBus (in-memory, общий)
                             │
                          skills / tools / memory / logging
```

Проблема не в transport layer (он уже разный и правильно разный), а в composition layer:
1. `ApplicationContext.create()` принимает 5 `enable_*`-kwargs как constructor-args.
2. DuckDB-кэш разделён по ролям (`cli.duckdb` vs `cache.duckdb`) — два независимых data lake'а.
3. `gateway.py` спавнит Streamlit subprocess (server-only зависимость зашита в entrypoint).

## Goals / Non-Goals

**Goals:**
- Единая сигнатура `ApplicationContext.create(role=..., ...)` для CLI и gateway.
- Все `enable_*`-флаги читаются из конфига, а не из kwargs.
- Единый `cache.duckdb` для всех процессов; ownership через PG-level claim.
- Streamlit удалён полностью.
- Transport layer (CLI = in-memory bus, gateway = PostgresChannel) НЕ меняется.

**Non-Goals:**
- Изменение `nanobot.MessageBus`, `nanobot.AgentLoop`, `BaseChannel`.
- Заставлять CLI использовать `PostgresChannel` для I/O.
- Создание `TerminalChannel` или другого нового transport-класса.
- Изменение схемы `agent_messages` / `agent_worker_claims` для обычных task-claim'ов.

## Decisions

### D1. `ApplicationContext.create(role=...)` — единая сигнатура

**Решение:** Обязательный kwarg `role: Literal["gateway", "cli", "utility"] = "gateway"` (default для backward compat). Параметры `enable_db_logging`, `enable_audit`, `enable_cron`, `print_llm_calls` помечаются deprecated в kwargs; если kwargs не передан — читается из `SETTINGS["gateway"].*`; если передан — используется kwargs + `warnings.warn(..., DeprecationWarning, stacklevel=2)`.

CLI-runtime-флаги (kwargs `storage_override`, `session_override`) остаются как опциональные — gateway их не передаёт, CLI передаёт по необходимости.

**Альтернативы:**
- Полностью удалить `enable_*` kwargs в одном релизе — отвергнуто: 30+ существующих тестов их передают. Breaking change без transitional period нарушает backward compatibility contract.
- Не вводить `role`, а определять по argv — отвергнуто: `ApplicationContext` не должен знать о CLI/gateway entrypoint; явный параметр делает контракт прозрачным.

### D2. `role` определяет composition инфраструктуры, не AgentLoop

**Решение:** Composition matrix:

| Сервис | `role="gateway"` | `role="cli"` | `role="utility"` |
|---|---|---|---|
| `AgentLoop` (с hooks, runtime patches, skills, tools, memory) | ✅ | ✅ | ✅ |
| `DbLoggingService` (если `gateway.enable_db_logging=True`) | ✅ | ✅ | ✅ |
| `SessionManager` / `PGSessionManager` | ✅ | ✅ | ✅ |
| `RuntimeEventsSubscriber` | ✅ | ✅ | ✅ |
| `RuntimePatcher.apply_all()` | ✅ | ✅ | ✅ |
| `DuckDbCacheStore` (открывает `<local_path>/cache.duckdb`) | ✅ | ✅ | ❌ |
| `PostgresChannel` (worker pool) | ✅ | ❌ | ❌ |
| `PgDuckDbSyncService` (sync — если claim выдан) | ✅ | ✅ | ❌ |
| `CronService` (если `gateway.enable_cron=True`) | ✅ | ✅ | ❌ |
| WebSocket port check (вызывается из entrypoint) | ✅ | ❌ | ❌ |
| Console I/O (in-memory bus) | ❌ | ✅ | ❌ |

CLI REPL: `bus.publish_inbound(InboundMessage(channel="cli", ...))` + `bus.consume_outbound()` — текущее поведение `lib/cli/console_loop.py`. Никаких изменений в REPL-цикле.

**Альтернативы:**
- Заставить CLI использовать `PostgresChannel` для I/O — отвергнуто: усложняет, замедляет, не даёт преимущества.
- `TerminalChannel` как отдельный класс — отвергнуто: CLI = bus напрямую, обёртка не нужна.

### D3. Единый snapshot-путь

**Решение:** `<local_path>/cache.duckdb` для всех процессов. `resolve_publish_path(role)` сохранён в API (для backward compat существующих вызовов), но `role="cli"` и `role="gateway"` возвращают **один и тот же путь**. Утилиты получают тот же путь, но `CacheProvider` для `role="utility"` не открывается (см. D2).

**Альтернативы:**
- Role-based пути (`cli.duckdb` vs `cache.duckdb`) — отвергнуто: создаёт два независимых состояния data lake'а, противоречит принципу «единый operational data layer агента».
- Lockfile на уровне ОС — отвергнуто: NFS не поддерживает, усложняет.

### D4. PG-level ownership claim для DuckDB-sync producer

**Решение:** Новый модуль `lib/services/cache_ownership.py::try_claim_cache_ownership(worker_id, ...) -> bool`. Использует PG-транзакцию для атомарной вставки claim'а:

- Расширение таблицы `agent_worker_claims` колонкой `claim_type VARCHAR DEFAULT 'task' NOT NULL` (или создание отдельной таблицы `agent_cache_ownership` — design.md определит).
- Claim имеет `worker_id`, `acquired_at`, `last_heartbeat_at`, `status`.
- Логика:
  - При старте `PgDuckDbSyncService.start()`:
    1. `SELECT` active claim (where claim_type='cache' AND status='active' AND last_heartbeat_at > NOW() - INTERVAL '60 seconds');
    2. Если есть — наш worker_id сравнивается с active; если совпадает → already owner; иначе → consumer mode (без sync).
    3. Если нет — `INSERT` новый claim (с TTL=0, наш worker_id); если успех → producer mode; если unique constraint violation (другой процесс одновременно claim'нул) → consumer mode.
  - Heartbeat каждые 30 сек (UPDATE last_heartbeat_at).
  - На clean shutdown — DELETE claim или UPDATE status='released'.

**Сценарии:**
- Только CLI → CLI = producer.
- Только gateway → gateway = producer.
- И CLI, и gateway → первый запущенный = producer; второй = consumer.
- Producer умер → через 60 сек claim stale → next process может перехватить.

**Альтернативы:**
- Extend `agent_worker_claims` (один универсальный механизм аренды) — выбрано для переиспользования инфраструктуры.
- Новая таблица `agent_cache_ownership` — отвергнуто: дублирование схемы; существующая `agent_worker_claims` имеет TTL/heartbeat/lease-механизм, расширить проще.
- File lock на `cache.duckdb` — отвергнуто: DuckDB уже использует flock на файле; layering ещё одного flock поверх — хрупко.
- Жёсткое правило «gateway = producer» — отвергнуто: пользователь явно сказал CLI без gateway должен работать.

### D5. Удаление Streamlit — единым коммитом

**Решение:**
- Удалить `streamlit_app.py` (файл).
- Удалить `lib/services/subprocess_manager.py::spawn_streamlit`. Если других методов в модуле нет — удалить модуль целиком.
- Удалить `_streamlit_enabled()` и блок `if _streamlit_enabled() and subprocess_manager.spawn_streamlit(...)` из `gateway.py::run`. Удалить импорт `SubprocessManager`.
- `streamlit.*`-секция в `project.json` — оставляется как permissive (pydantic `extra="allow"`); runtime её игнорирует.
- Обновить `AGENTS.md`, `README.md`, `docs/INTERNAL_API.md`, `docs/ARCHITECTURE.md`.

**Альтернативы:**
- Deprecation period — отвергнуто: пользователь явно сказал «удалить полностью».
- Сохранить `streamlit_app.py` как legacy — отвергнуто: 2190 строк неиспользуемого кода = технический долг.

### D6. WebSocket port check остаётся в gateway

**Решение:** WebSocket port check — server-only pre-startup проверка. Остаётся в `gateway.py::_entrypoint_main`. CLI НЕ выполняет эту проверку. Проверка НЕ вызывается из `ApplicationContext.start()`.

### D7. `--storage` в CLI: `auto`/`postgres`/`file`

**Решение:** `--storage=auto` (default) → `storage_override=None`. `--storage=postgres` → явно postgres-storage. `--storage=file` → file-storage.

### D8. `/compact` в CLI — локальный shortcut

**Решение:** `lib/cli/console_loop.py::_run_cli_compact` остаётся без изменений. CLI вызывает `ContextCompactionService.compact(session_key="cli:<session>", idle=True, force=True)` напрямую.

### D9. Cron в обоих режимах (config-driven)

**Решение:** `CronService` создаётся, если `gateway.enable_cron=True`. Работает и в `role="gateway"`, и в `role="cli"`. `role="utility"` НЕ создаёт `CronService`. Документируется: cron fires из обоих процессов, если они одновременно работают.

### D10. Тесты — backward compatibility и новые сценарии

**Решение:**
- Существующие тесты с `ApplicationContext.create(enable_audit=True/False, ...)` — продолжают работать.
- Новые тесты:
  - `tests/test_application_context_role.py` — composition matrix (D2).
  - `tests/test_cli_uses_in_memory_bus.py` — REPL использует bus, не PostgresChannel.
  - `tests/test_cache_ownership_claim.py` — `try_claim_cache_ownership` testable: первый = producer, второй = consumer; stale claim takeover.
  - `tests/test_cache_provider_role_paths.py` — `resolve_publish_path(role="cli") == resolve_publish_path(role="gateway") == cache.duckdb`.
  - `tests/test_agent_loop_transport_agnostic.py` — AgentLoop работает только через bus.
  - `tests/test_streamlit_removed.py` — отсутствие Streamlit.

## Risks / Trade-offs

- **[Risk]** Standalone-утилиты (`tools/build_vectors.py` и др.) забывают передать `role="utility"`, получают default `role="gateway"` и пытаются открыть `cache.duckdb` без конфигурации. → **Mitigation:** `tools/build_vectors.py` обычно требует `gateway.enable_audit=True` для чтения существующего snapshot, что покрыто DEFAULTs в `ApplicationContext.create()`. Тест `test_utility_role_minimal_context` подтверждает, что `role="utility"` не создаёт `PostgresChannel` и `PgDuckDbSyncService`.
- **[Risk]** PG-level claim через `agent_worker_claims` — race condition если два процесса стартуют одновременно. → **Mitigation:** unique constraint на `(claim_type, status)`; один INSERT проходит, другой — `ON CONFLICT` или violation. Документировано в `cache_ownership.py`.
- **[Risk]** Если `cache_ownership` claim TTL = 60 сек, и producer kill'нут через `kill -9`, consumer ждёт 60 сек прежде чем может перехватить. → **Mitigation:** heartbeat каждые 30 сек; в тестах используется уменьшенный TTL (5 сек).
- **[Risk]** Deprecated-период для `enable_*` kwargs затягивается. → **Mitigation:** явная задача в tasks.md с привязкой к MINOR-релизу.
- **[Risk]** Удаление Streamlit ломает существующие deployment'ы. → **Mitigation:** CHANGELOG BREAKING в категории `Removed`.
- **[Risk]** Cron fires дважды при одновременной работе CLI и gateway. → **Mitigation:** документируется как known limitation.

## Migration Plan

**Шаг 1 (текущий MINOR):** Реализация всех решений, deprecated-период для `enable_*` kwargs, Streamlit удаляется, PG-level cache claim добавляется. DeprecationWarning в логи.

**Шаг 2 (следующий MINOR):** Удаление deprecated `enable_*` kwargs из `ApplicationContext.create()`.

**Rollback:** Если после раскрытия обнаружена критическая регрессия — revert на коммит до раскрытия.

## Open Questions

- Решены в proposal.md Open Questions:
  1. **PG-level claim mechanism: extend `agent_worker_claims` или новая таблица `agent_cache_ownership`?** — D4: extend `agent_worker_claims` через колонку `claim_type`.
  2. **Heartbeat интервал и TTL** — D4: 30 сек heartbeat, 60 сек TTL.
  3. **`role="utility"`** — D2: НЕ открывает `DuckDbCacheStore`.

- **Новые открытые вопросы** (после прочтения proposal.md v3):
  1. **Миграция БД для добавления колонки `claim_type`** — `agent_worker_claims` сейчас не имеет `claim_type`. Изменение схемы через `tools/migrate.py --apply`. Verify в tasks.md.
  2. **`cache_ownership_ttl_sec` конфигурация** — hardcoded 60 сек или в `project.json::gateway.cache.ownership_ttl_sec`? Пока hardcoded; вынести в конфиг — отдельный change.