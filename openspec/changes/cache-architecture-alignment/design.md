## Context

Текущее состояние задокументировано в `proposal.md` (раздел "Why").
Здесь — архитектурные решения, через которые 11 drift-пунктов
приводятся в соответствие со спекой `data/cache-provider`.

Ключевое ограничение: `CacheSyncService` (= `PgDuckDbSyncService`)
уже использует `CacheOwnershipCoordinator.acquire_write_fence()` для
инкрементального polling (Stage E, commit `85e962c`). Fencing
**инфраструктура есть**, но её применение непоследовательно: только
poll-cycle обёрнут в fence, initial_load и sync_callback — нет. То
же для heartbeat — метод существует, но никем не вызывается.

## Goals / Non-Goals

**Goals:**

- Cache runtime создаётся при наличии `gateway.cache.local_path`,
  независимо от `enable_audit`. Sync — отдельно (только при
  `enable_audit=True` и `acquired=True`).
- `DuckDbCacheStore` наследует `CacheProvider` (ABC); runtime
  consumers остаются на `CacheProvider` interface, без изменений.
- Fence обёрнут вокруг initial_load, polling, и sync_callback —
  единый critical section per producer mutation.
- Heartbeat worker-thread живёт ровно столько, сколько процесс —
  OWNER. Stop / takeover / release останавливают worker.
- NFS check работает на Linux / macOS / Windows через единый
  абстрактный `detect_network_filesystem(path)`.

**Non-Goals:**

- Изменение default `enable_cron=False → True` (см. proposal
  "Не входит в scope"). Этот change трогает ТОЛЬКО cache-архитектуру.
- Удаление `streamlit_app.py`.
- Изменение default ownership TTL `60 → N`.
- Изменение Postgres-таблицы `agent_cache_ownership` (её схема
  уже соответствует требованиям; нужно только использование
  heartbeat).
- Переименование `PostgresDuckDbProvider` — оставляем как
  legacy-имя, чтобы не делать mass-rename в стороннем коде, который
  импортирует его напрямую (если такой есть). Только унифицируем
  enforcement под общий ABC.

## Decisions

### Decision 1: Cache runtime gate logic в `_make_sync_services`

**Что:** вынести gate для cache runtime на `gateway.cache.local_path`
вместо `enable_audit`. Sync продолжает гейтиться `enable_audit`.

**Альтернативы:**
- (A) Перенести `_make_sync_services` целиком из `enable_audit` в
  `gateway.cache` (текущая попытка, обсуждаемая здесь).
- (B) Разделить `_make_sync_services` на `_make_cache_runtime()` +
  `_make_sync_service()`. Cache — всегда при `gateway.cache`,
  sync — при `enable_audit=True` И `acquired=True`.

**Выбор:** (B). Это позволяет:
- cache/ownership/provider создаются ровно когда есть
  `local_path` (текущая логика "если enable_audit" — drift);
- sync — отдельная factory, гейтится `enable_audit` И ownership;
- ownership-coordinator регистрируется как producer ресурс
  (`cache_provider`) в shutdown coordinator, sync — отдельно.

**Результат:** `lib/core/application_context.py:308-340`
переписывается: gate на `cache.local_path`, не на `enable_audit`.

### Decision 2: `DuckDbCacheStore(CacheProvider)` — минимальная правка

**Что:** добавить `(CacheProvider)` к `class DuckDbCacheStore:` и
реализовать abstract-методы `query_sql`, `get_schema`, `search_vector`,
`explain`, `execute_readonly`, `preload_indexes`, `preload_errors`,
`close` уже есть. Нужно проверить signature-match.

**Альтернативы:**
- (A) Сделать DuckDbCacheStore subclass CacheProvider +
  сохранить существующие instance-методы как делегаты к ABC.
- (B) Refactor DuckDbCacheStore в чистый CacheProvider, удалить
  legacy-методы.

**Выбор:** (A). Покрытие ABC проверяется через `isinstance(...)`
и контрактные методы. Legacy instance-методы (`upsert_records`,
`replace_records`, `publish`, `connect`) остаются как deprecated
wrappers, делегирующие к ABC-методам.

**Почему не (B):** `publish` использует `self._conn` (внутреннее
DuckDB-connection), которое не входит в `CacheProvider` ABC.
Чистый CacheProvider не имеет `_conn`. Должен быть либо subclass
helper, либо явный duck-typing. Subclass approach минимально
инвазивен.

### Decision 3: Fencing scope — единый helper `_sync_cycle_with_fence`

**Что:** инкапсулировать ВСЕ producer mutations
(initial_load, polling, sync_callback) в один helper, который
оборачивает acquire_write_fence + try_claim-recheck.

```python
def _with_fence(self, work: Callable[[], None], label: str) -> None:
    with self._ownership_coordinator.acquire_write_fence():
        # recheck внутри fence на случай take over между claim
        # и worker start
        current = self._ownership_coordinator.read_current()
        if current.owner_id != self._worker_id or \
           current.generation != self._my_generation:
            raise OwnershipLostError(...)
        work()
```

**Альтернативы:**
- (A) Каждое mutation-место оборачивается вручную (`_do_initial_load`
  → acquire_write_fence → recheck → work; `_poll_changes` → то же;
  `_fire_sync_callback` → то же). Дублирование boilerplate.
- (B) Helper (выбрано). Один источник истины для fence-протокола.

**Выбор:** (B) helper. Покрывает все 11 пунктов fencing: 4, 5 +
существующий polling.

### Decision 4: Heartbeat worker — отдельный thread на стороне OWNER

**Что:** при `try_claim().acquired=True` запускается
`threading.Thread(target=heartbeat_loop, daemon=True)`, который
каждые 30 сек обновляет `expires_at`. При `release()` /
`stop()` / takeover — worker останавливается через `threading.Event`.

**Альтернативы:**
- (A) Heartbeat в основном polling thread'е (sync worker делает
  heartbeat между poll cycles). Простой, но timing-зависим: если
  poll задержался на 70 сек (большой initial load), claim может
  истечь.
- (B) Отдельный daemon-thread (выбрано). Timing-независимо.
- (C) asyncio task в основном event loop'е. Минимально инвазивно,
  но требует активного loop'а; в standalone sync thread'е loop'а
  нет.

**Выбор:** (B). Daemon thread, чтобы процесс мог завершиться даже
если worker'у не пришёл stop-сигнал. Heartbeat loop идёт в
`lib/services/cache_ownership.py:heartbeat_loop()` как standalone
function. В `CacheOwnershipCoordinator` новые методы
`start_heartbeat()`, `stop_heartbeat()`, internal `_heartbeat_loop()`.

### Decision 5: NFS detection — единый `detect_network_filesystem(path)`

**Что:** новый helper `lib/utils/filesystem_prober.py` с функцией
`detect_network_filesystem(path) -> bool`. Использует:

- На Linux: `/proc/mounts` (как сейчас в `_warn_if_publish_path_on_nfs`).
- На macOS: `mount` shell command или `psutil.disk_partitions()`.
- На Windows: `psutil.disk_partitions()` + проверка `DriveType` через
  `ctypes.GetDriveTypeW` (DRIVE_REMOTE / DRIVE_REMOVABLE).

`DuckDbCacheStore.__init__` (или `.open()`) вызывает
`detect_network_filesystem` после `_reject_unsupported_filesystem`,
fail-fast на `True`.

**Альтернативы:**
- (A) Положиться на `psutil.disk_partitions()` на всех
  платформах. Кросс-платформенно, но `psutil` не всегда доступен.
- (B) Custom platform-specific detection (выбрано). Лучший контроль,
  нет новых зависимостей.

**Выбор:** (B). `psutil` уже есть в `requirements.txt`, но для NFS
проще использовать `/proc/mounts` / `mount` / `GetDriveTypeW`
напрямую. Меньше surface area.

### Decision 6: `cache_store` rename — механический rename + alias-deprecation cycle

**Что:** 13 ссылок в `gateway.py`, 4 в `benchmarks/runner.py`,
8 в `lib/services/project_tool_loader.py`. Rename
`ctx.cache_store` → `ctx.cache_provider`. Alias
`ctx.cache_store = ctx.cache_provider` остаётся в
`ApplicationContext.__init__` deprecated на один релиз, удаляется
в следующем.

**Альтернативы:**
- (A) Hard rename без alias (BREAKING для external callers, если
  есть). Не наш случай — `cache_store` нигде не публикуется как
  API.
- (B) Alias cycle (выбрано). Безопасно: внутренний rename, alias
  один релиз для safety net.

**Выбор:** (B). Alias удаляется в change `cache-store-alias-remove`
после первого MINOR релиза с этим change.

### Decision 7: Test layering — новый `tests/test_cache_provider_layering.py`

**Что:** AST-grep runtime-кода на `from duckdb_cache_store import
DuckDbCacheStore` / `import DuckDbCacheStore`. Allowed:
`lib/services/duckdb_cache_store.py` (definition),
`lib/core/application_context.py` (composition),
`lib/services/cache_provider_impl.py` (legacy impl),
`tests/` (tests for DuckDB-specific behavior).

**Альтернативы:**
- (A) Renaming import-blocklist через
  `python_ta` / `ruff` linter. Не настроен в проекте.
- (B) Grep-тест (выбрано). Минимально, встроено в pytest.

**Выбор:** (B). Дополняет существующий pattern в
`tests/test_application_context_role.py` (AST-grep deprecated kwargs).

## Risks / Trade-offs

**[Risk]** Fencing scope расширяется (initial_load, sync_callback)
→ больше contention на `pg_advisory_xact_lock`. → Mitigation:
lock держится только на время реальных мутаций (миллисекунды),
не на время логики обработки. Polling уже работает так — pattern
proven.

**[Risk]** Heartbeat thread создаёт дополнительный open connection
к PG. → Mitigation: heartbeat thread переиспользует существующий
pool `utils.db.run()` (который уже thread-safe), не открывает свою
connection.

**[Risk]** `DuckDbCacheStore(CacheProvider)` — тип-расширение может
сломать существующие `isinstance(_, DuckDbCacheStore)` checks в
runtime-коде. → Mitigation: `isinstance(x, DuckDbCacheStore)`
продолжает работать после subclassing. Проверить AST-grep на
runtime-стороне.

**[Risk]** Mass-rename `cache_store` → `cache_provider` может
пропустить какие-то ссылки (например, в docs, или в sidecar
скриптах). → Mitigation: AST-grep в `tools/architecture_guard.py`
для `ctx.cache_store` после рефактора. Grep по всему `*.py`.

**[Risk]** NFS detection на macOS/Windows может быть неточной (mount
сложнее). → Mitigation: тест suite с mock'ом `platform.system()`
и `subprocess.run()`. Fail-loud на unknown FS type.

**[Risk]** `remove-profile-environment-selection` change
(отдельный, параллельно идёт через свой lifecycle) может
конфликтовать с этим по timing архивации. → Mitigation: archive
этот change (`cache-architecture-alignment`) ПОСЛЕ того, как
`remove-profile-environment-selection` archived. Или одновременно,
если они оба будут в одном коммите.

## Migration Plan

1. **Phase 1 — Spec-only** (этот change). Apply-фаза коммитит только
   spec deltas и changelog.
2. **Phase 2 — Implementation** (отдельные коммиты под этим change):
   - 2.1: Decision 1 (gate fix). Тесты: cache runtime работает при
     `enable_audit=False`.
   - 2.2: Decision 2 (CacheProvider inheritance). Тесты:
     `isinstance(store, CacheProvider)`.
   - 2.3: Decision 3 (fencing scope). Тесты:
     `TestSyncServiceFencingBehavior` расширен для initial_load.
   - 2.4: Decision 4 (heartbeat). Тесты:
     `test_cache_ownership_lifecycle.py::test_heartbeat_*`.
   - 2.5: Decision 5 (NFS check). Тесты: macOS / Windows scenarios.
   - 2.6: Decision 6 (rename). Тесты: AST-grep no `cache_store`.
   - 2.7: Decision 7 (test layering). Тесты:
     `test_cache_provider_layering.py`.
3. **Phase 3 — Archive**: `openspec.cmd archive
   cache-architecture-alignment` после прохождения всех tests
   и проверок.

**Rollback:** каждый phase — отдельный коммит. Если phase 2.3
(fencing) ломает production behavior, его можно revert'ить без
потери phase 2.1-2.2 (gate + inheritance).

## Open Questions

Нет открытых вопросов, которые блокируют implementation. Все
architectural decisions приняты в этом документе.

Возможные future-questions (за пределами этого change):

- Нужен ли `cache_store → cache_provider` rename в `streamlit_app.py`?
  Если `streamlit_app.py` остаётся до `remove-streamlit-runtime`,
  может временно использовать alias. Resolve при архивации этого
  change.
- Должен ли heartbeat thread быть видимым в diagnostics tools
  (`tools/diagnose_startup.py`)? Опционально, отдельная feature
  request.
