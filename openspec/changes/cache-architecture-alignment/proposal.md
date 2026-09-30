## Why

Реализация cache runtime дрейфует от спецификации
`openspec/specs/data/cache-provider/spec.md` по одиннадцати конкретным
пунктам, часть из которых — runtime correctness (heartbeat
lifecycle отсутствует → owner становится stale через 60 сек, fencing
обходит initial_load), часть — архитектурная инвариантность
(`DuckDbCacheStore` не наследует `CacheProvider`, два разных cache API,
legacy alias `cache_store` в публичной поверхности, NFS check только
на Linux).

Эти дрейфы появились инкрементально в staged commits `789c60b` /
`2636499` / `85e962c` / `e1d0317` (`unify-cli-gateway-architecture`),
которые теперь archived в `openspec/changes/archive/`, но
соответствующая canonical spec `data/cache-provider` была обновлена
без полной реализации требований. Сейчас возврат env-fallback,
потеря cache runtime при `enable_audit=False`, или heartbeat-сбой
другого процесса не будут замечены архитектурными тестами.

## What Changes

Этот change фиксирует **одиннадцать конкретных drift-пунктов** между
`openspec/specs/data/cache-provider` и реализацией, и приводит код в
соответствие со спекой.

1. **`enable_audit` MUST NOT гейтить cache runtime.** Сейчас в
   `lib/core/application_context.py:320` `if ctx.enable_audit:`
   оборачивает весь `_make_sync_services()` — cache/ownership/provider
   НЕ создаются, если `enable_audit=False`. Cache — shared
   runtime-resource, не зависит от audit-lifecycle. Sync (pg →
   duckdb) — отдельная concern, управляется отдельно через
   `gateway.enable_audit`.
2. **`DuckDbCacheStore` MUST наследовать `CacheProvider`.** Сейчас
   `lib/services/duckdb_cache_store.py:308` объявляет `class
   DuckDbCacheStore:` без `(CacheProvider)`. Это нарушает инвариант
   «runtime consumers MUST зависеть только от `CacheProvider`»
   (`data/cache-provider` requirement #6).
3. **Единый cache API.** Сейчас существуют два разных класса с двумя
   разными `query_sql`: `PostgresDuckDbProvider` (через простую
   обёртку `run_query`) и `DuckDbCacheStore` (через
   `_assert_query_sql_allowed_locked`). Объединяем enforcement под
   общим `CacheProvider`-API.
4. **Fencing MUST покрывать `_do_initial_load()`.** Сейчас
   `pg_duckdb_sync_service.py:285-301` initial load выполняется ДО
   первого `_sync_cycle_with_fence()` — мутации в initial_load идут
   без ownership-проверки и могут clobber'ить данные при takeover.
5. **Fencing MUST покрывать `_fire_sync_callback()`.** Сейчас
   fence обёрнут только вокруг `_poll_changes()` (line 330);
   `_fire_sync_callback` (lines 290, 304) идёт в обход fence.
6. **Heartbeat lifecycle MUST быть запущен.** Сейчас
   `CacheOwnershipCoordinator.heartbeat()` существует
   (`lib/services/cache_ownership.py:245`), но **никто его не
   вызывает** (grep `heartbeat()` в `pg_duckdb_sync_service.py` →
   0 матчей). `DEFAULT_TTL_SECONDS = 60` → owner становится stale
   через минуту даже при живом процессе. Другое приложение
   перехватывает ownership → старый процесс продолжает писать.
   Heartbeat worker-thread должен запускаться при
   `try_claim().acquired=True` и останавливаться в `release()` /
   `stop()`.
7. **Удалить instance `open(self)` из `DuckDbCacheStore`.** Сейчас
   `duckdb_cache_store.py:381` имеет instance-метод `open(self)` рядом
   с classmethod `open(cls, path, mode)` (line 433). Это два метода
   с одним именем и разной сигнатурой — путаница. Instance-метод —
   legacy-артефакт из старого API.
8. **`cache_store` → `cache_provider` rename в публичной
   поверхности.** Сейчас `ctx.cache_store` — legacy alias на
   `ctx.cache_provider` (`application_context.py:333`). 13 ссылок в
   `gateway.py`, 4 в `benchmarks/runner.py`, 8 в `lib/services/
   project_tool_loader.py`. После rename удалить alias.
9. **NFS check MUST работать на не-Linux платформах.** Сейчас
   `duckdb_cache_store.py:152` имеет early-return для non-Linux:
   NFS detection работает только на Linux. macOS / Windows MUST тоже
   проходить проверку `mount`/`fsutil`-equivalent до открытия
   storage.
10. **`query_sql()` enforcement MUST быть идентичен для всех
    реализаций `CacheProvider`.** DuckDbCacheStore корректно
    использует `_classify_sql` + `_assert_query_sql_allowed_locked`;
    `PostgresDuckDbProvider.query_sql` — простая обёртка без
    enforcement. После пункта 3 это станет uniform.
11. **Тесты MUST проверять `CacheProvider`-контракт, не concrete
    class.** Сейчас `tests/test_cache_provider_mode.py:81`
    `assert isinstance(instance, DuckDbCacheStore)` — проверка должна
    быть `assert isinstance(instance, CacheProvider)`. Добавить
    `tests/test_cache_provider_layering.py` со статическим guard'ом:
    runtime-код MUST NOT импортировать `DuckDbCacheStore` (кроме
    composition root).

## Capabilities

### New Capabilities

Нет. Новых capability не вводится — это приведение реализации в
соответствие с уже существующими требованиями `data/cache-provider`.

### Modified Capabilities

- `data/cache-provider`: требования уже покрывают желаемое поведение
  по каждому из 11 пунктов. Этот change фиксирует
  имплементационный drift, добавляя сценарии-guard'ы (пункты 1, 2,
  3, 4, 5, 6, 9, 11) и явно прописанные требования для пунктов 7,
  8, 10.

## Impact

**Runtime-код:**

- `lib/core/application_context.py` — переписать `_make_sync_services`
  gate-логику: cache/ownership/provider создаются всегда при
  наличии `gateway.cache`; sync — отдельно, гейтится
  `enable_audit` (пункт 1).
- `lib/services/duckdb_cache_store.py` — `class DuckDbCacheStore
  (CacheProvider):` (пункт 2); удалить instance-метод `open(self)`
  (пункт 7); NFS check для non-Linux через `psutil.disk_partitions()`
  (пункт 9).
- `lib/services/cache_provider_impl.py` — удалить/слить
  `PostgresDuckDbProvider` с `DuckDbCacheStore` (пункт 3) либо
  унифицировать enforcement в общем базовом классе (пункт 10).
- `lib/services/cache_ownership.py` — heartbeat helper для запуска
  worker-thread (пункт 6).
- `lib/services/pg_duckdb_sync_service.py` — обернуть
  `_do_initial_load()` и `_fire_sync_callback()` в
  `acquire_write_fence()` (пункты 4, 5); запустить heartbeat при
  `start()` (пункт 6).
- `lib/services/project_tool_loader.py` — переименовать параметр
  `cache_store` → `cache_provider` (пункт 8).
- `gateway.py` (13 ссылок), `benchmarks/runner.py` (4 ссылки),
  `lib/services/application_context.py` (alias) — rename +
  удаление alias (пункт 8).

**Тесты:**

- `tests/test_cache_provider_mode.py` — `isinstance` → `CacheProvider`.
- Новый `tests/test_cache_provider_layering.py` — AST guard для
  runtime-импортов `DuckDbCacheStore` (пункт 11) + integration
  тест для пунктов 1, 4, 5, 6.
- `tests/test_pg_duckdb_sync_service.py` — расширить
  `TestSyncServiceFencingBehavior` для покрытия initial_load и
  sync_callback под fence (пункты 4, 5).
- `tests/test_cache_ownership_lifecycle.py` (новый) — heartbeat
  worker-thread lifecycle (пункт 6): запускается при
  `try_claim().acquired=True`, перехватывается при takeover,
  останавливается в `release()` / `stop()`.
- `tests/test_duckdb_cache_store.py::TestNfsCheck` — добавить
  macOS / Windows сценарии через mock `platform.system()` /
  `psutil.disk_partitions` (пункт 9).

**Документация:**

- `docs/ARCHITECTURE.md` — обновить диаграмму cache runtime, отметить
  что `cache_store` alias удалён.
- `CHANGELOG.md` — запись в `[Unreleased]` блок с разбивкой по
  пунктам 1-11.

**Не входит в scope:**

- Изменение default `enable_cron=False` → `True` per spec line 47
  `runtime/entrypoints`. Это отдельный decision (см. комментарий в
  предыдущем коммите): cron job `dream` уже существует в
  `workspace/cron/jobs.json`, default `True` активирует его без
  явного opt-in. Требует отдельного user-approval.
- Удаление `streamlit_app.py` и `SubprocessManager.spawn_streamlit`
  — отдельный change `remove-streamlit-runtime`.
- Переименование `PostgresDuckDbProvider` → сохранение legacy-имени
  для совместимости со ссылками в `lib/services/cache_provider_impl.py`
  и тестах. Только enforce единый API (пункт 3, 10).
