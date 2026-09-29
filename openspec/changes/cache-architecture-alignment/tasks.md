## 1. Contract (Phase 1 — DONE)

- [x] 1.1 Новая capability `data/cache-runtime-lifecycle`: `specs/data/cache-runtime-lifecycle/spec.md`
- [x] 1.2 Модификация `data/cache-provider`: `specs/data/cache-provider/spec.md`
- [x] 1.3 Удаление дубликатов cache-контракта из `runtime/entrypoints`:
      `specs/runtime/entrypoints/spec.md` (`## REMOVED Requirements`, 4 требования)

## 2. Cache runtime не зависит от способа запуска (Decision 1)

- [ ] 2.1 Разделить `_make_sync_services()` (`lib/core/application_context.py`) на `_make_cache_runtime()` + `_make_sync_service()`; обе идемпотентны при повторном вызове
- [ ] 2.2 `_make_cache_runtime()` вызывается при наличии `gateway.cache.local_path` независимо от `enable_audit`; снять гейт `if ctx.enable_audit:` (`application_context.py:340`)
- [ ] 2.3 `worker_id` строить без `ctx.role` (`application_context.py:1538`)
- [ ] 2.4 `tests/test_application_context_cache_lifecycle.py::TestCacheRuntimeIndependentOfEnableAudit`: 3 кейса (OWNER + READ_WRITE, READER + READ_ONLY, READER + READ_ONLY при `enable_audit=False`)
- [ ] 2.5 `tests/test_application_context_role.py` (`:96` перечисляет `cache_store`; `:127`, `:143` ставят alias) — привести к новому поведению; `role`-независимость ownership проверяется отдельным тестом
- [ ] 2.6 `python cli_agent.py --smoke` и `python gateway.py --profile=test --smoke` — exit 0 при `enable_audit=false`

## 3. Ownership lifecycle (Decision 4, 9)

- [ ] 3.1 `start_heartbeat()` / `stop_heartbeat()` / `_heartbeat_loop()` в `lib/services/cache_ownership.py`; сигнал остановки — `threading.Event`
- [ ] 3.2 Heartbeat стартует только при `try_claim().acquired=True`; READER heartbeat-worker'а не имеет
- [ ] 3.3 Heartbeat останавливается в `release()` и при обнаружении takeover (`heartbeat() == False` → лог `ownership_lost`, worker не producer)
- [ ] 3.4 Формальный порядок shutdown в `ApplicationContext.stop()` (`application_context.py:613-681`): `stop sync` → `stop heartbeat` → `close cache` (`:664-668`) → `release` (`:672-676`)
- [ ] 3.5 Тест shutdown-ordering: после `release()` worker sync не может записать
- [ ] 3.6 `tests/test_cache_ownership_lifecycle.py::TestHeartbeatWorker` — 4 сценария спеки: продление `expires_at`, takeover после TTL, READER не heartbeat'ит, graceful shutdown

## 4. Producer fencing (Decision 5, 6)

- [ ] 4.1 `_with_fence(work, label)` в `lib/services/pg_duckdb_sync_service.py`; fence + recheck generation до `work`
- [ ] 4.2 Обернуть `_do_initial_load()` (вызов с `:289`, вне fence)
- [ ] 4.3 Разделить callback-семантику: `set_on_sync_callback(cb, mutates_cache: bool = True)`; cache-mutating — под fence (вызовы на `:290` и `:304`, сейчас вне fence)
- [ ] 4.4 Перевести `_poll_changes()` (`:330`) на `_with_fence` вместо inline `with acquire_write_fence()`
- [ ] 4.5 Исправить docstring `_sync_cycle_with_fence` (`:321-322`): он описывает callback внутри fence, которого там нет
- [ ] 4.6 Тесты: `test_initial_load_under_fence`, `test_sync_callback_under_fence` (takeover посреди), observer-callback не берёт advisory lock; существующий `TestSyncServiceFencingBehavior` — без регрессий
- [ ] 4.7 Ветка `if self._ownership_coordinator is None` — только для изолированных тестов экземпляра; composition MUST передавать coordinator (тест composition-контракта)

## 5. Один файл — один интерфейс — одна реализация (Decision 7, 10, 11)

**Модель (принята 2026-09-29).** Существует **ровно один файл кэша**. К нему
имеют доступ **только** через интерфейс кэша (`CacheProvider`) — runtime,
skill и любой другой компонент. Какая реализация стоит за интерфейсом
(DuckDB, другая СУБД, файл в памяти) — **не важно и не видно вызывающим**:
компонент, назвавший конкретный класс хранилища, нарушает модель.

**Проверено по коду: сегодня этой модели нет.**

| Ожидание | Факт |
|---|---|
| Реализация за интерфейсом одна | `DuckDbCacheStore` (`duckdb_cache_store.py:308`) объявлен как `class DuckDbCacheStore:` — **не наследует** `CacheProvider`; `PostgresDuckDbProvider(CacheProvider)` (`cache_provider_impl.py:800`) — второй провайдер, живущий в слое skill'а |
| Runtime работает через интерфейс | `ApplicationContext.cache_provider: Any \| None` (`application_context.py:151`), `cache_store: Any \| None` (`:152`) — на уровне типа интерфейса нет; DI-шов (`project_tool_loader.py:284-296`) отдаёт tool'ам `DuckDbCacheStore`, а docstring `:213` обещает `CacheProvider` |
| Файл один | `DuckDbCacheStore.open(path=publish_path)` → `_cache_path = path` (`duckdb_cache_store.py:457`), затем `application_context.py:1564` присваивает `_publish_path` **тот же** путь → `_cache_path == _publish_path`. Runtime уже пишет в этот файл напрямую |
| Снапшот — отдельный файл | `publish()` (`:920-1110`) копирует файл **сам на себя**: `ATTACH` tmp → `CREATE OR REPLACE TABLE … AS SELECT` → `close()` → `os.replace(tmp, target)` → reopen (`:1077-1102`). Расхождение `local_path` как «файл» (`:177-186`) против «каталог» (`resolve_publish_path`) — следствие того же раздвоения |
| Методов интерфейса хватает обеим сторонам | У `DuckDbCacheStore` реализовано 7 из 9 abstract-методов; нет `refresh`/`check_stale` — то есть класс **не мог** объявить наследование, не определив судьбу этих двух методов |

- [ ] 5.1 `class DuckDbCacheStore(CacheProvider)` (`duckdb_cache_store.py:308`).
      Объявить наследование — после решения по `refresh`/`check_stale` (5.2),
      иначе класс не инстанцируется
- [ ] 5.2 **Судьба `refresh()` / `check_stale()`** (решение). Оба метода
      описывают репликацию PostgreSQL → кэш, а не свойство файла кэша.
      Проверено: **ни одного production-вызова** — `refresh()`/`check_stale()`
      вызываются только из `PostgresDuckDbProvider` (`:870`, `:882`), а
      `load_cache_from_postgres` (`:677`) и `check_cache_stale` (`:736`) — только
      из `tests/test_cache_provider_meta.py`. Репликацией в runtime владеет
      `PgDuckDbSyncService`. → **Оба метода уходят из ABC**; репликация остаётся
      за sync-слоем, а не за интерфейсом файла. verify: `grep -n "refresh\|check_stale"
      lib/services/cache_provider.py` — 0 hit
- [ ] 5.3 **Поверхность интерфейса** после 5.2. Чтение: `is_ready`,
      `get_schema`, `query_sql`, `explain`, `search_vector`, `preload_indexes`,
      `close`. Запись: **`upsert_records`** — единственная мутация (ingestion от
      sync-слоя). Сейчас `upsert_records` вне ABC, хотя через него runtime
      пишет, — интерфейс не покрывает собственное основное назначение.
      verify: каждый метод ABC реализован в `DuckDbCacheStore`, и
      `issubclass(DuckDbCacheStore, CacheProvider)` → `True`
- [ ] 5.4 **Одна точка создания провайдера, реализация невидима.**
      Функция `open_cache_provider(*, mode) -> CacheProvider` в
      `lib/services/cache_provider.py` (слой интерфейса) — единственный
      способ получить провайдера в рантайме. Внутри (ленивый импорт, во
      избежание цикла `cache_provider` ⇄ `cache_provider_impl`) она
      разрешает путь через `resolve_publish_path()` и делегирует конкретной
      реализации. **Вызывающий код вне слоя реализации не называет
      `DuckDbCacheStore` ни разу** — ни gateway, ни skill, ни tools. verify:
      `grep -rn "DuckDbCacheStore" lib/ workspace/ tools/ benchmarks/ gateway.py cli_agent.py`
      даёт хит только в `lib/services/duckdb_cache_store.py` и в тестах самой
      реализации
- [ ] 5.5 **Удалить `PostgresDuckDbProvider`** (`cache_provider_impl.py:800`)
      целиком вместе с его дублем открытия файла (`:888` `open_cache`,
      `:897-905` `_open_cache` → `duckdb.connect(read_only=True)`) и
      `load_cache_from_postgres` (`:677`) / `check_cache_stale` (`:736`).
      Сегодня это **вторая реализация интерфейса**, доступная skill'у, —
      прямое нарушение модели «одна реализация»
- [ ] 5.6 `build_cache_provider` (`:366`) — удалить (он создавал вторую
      реализацию). `lib/core/skill_config.py:274-275` и
      `workspace/skills/audit_analyzer/scripts/skill_config.py:79` —
      **оставить**: тонкие делегаты в 5.4 с возвращаемым типом
      `CacheProvider`. Skill-side CLI сохраняет точку входа
      (`docs/skill-tool-architecture.md` §8)
- [ ] 5.7 `ApplicationContext` (`application_context.py:151-152`) —
      `cache_provider: CacheProvider | None` вместо `Any | None`;
      docstring `:151` («CacheProvider ABC instance») станет правдой. verify
      `typing.get_type_hints` на поле
- [ ] 5.8 **Удалить `publish()`-как-self-replace** (`duckdb_cache_store.py:920-1110`).
      Механизм temp+`os.replace` — наследие модели «снимок», сменившейся на
      ownership при переходе Stage C/D, но не доведённой до конца: файл
      публикуется **сам в себя**, а `close()`/reopen вокруг `os.replace`
      (`:1088-1102`) каждый цикл роняет и заново захватывает блокировку —
      источник недетерминированного окна для читателя на Windows.
      Следствия: `_publish_path` (`:335`, `:942`) и `store._publish_path =
      publish_path` (`application_context.py:1564`) удаляются; `force=` и счётчики
      `_publishes`/`_publish_errors`/`_last_publish_at` пересматриваются;
      `gateway.py:159-170, 177-186, 220-226, 270-275` (удаление «старого
      снапшота», ожидание первого publish) правятся под новую семантику
- [ ] 5.9 Удалить instance-метод `open(self)` (`:381-391`) — он **перекрыт**
      `@classmethod open(cls, path, mode)` (`:432-459`) в теле класса и потому
      мёртв, а его docstring (`:385-389`) лжёт, называя callers `gateway.py` и
      `benchmarks/runner.py`: оба зовут `connect()` (`gateway.py:158`,
      `benchmarks/runner.py:618`). verify `grep -n "def open" lib/services/duckdb_cache_store.py`
      даёт ровно одно определение
- [ ] 5.10 `list_runtime_vector_indexes` (`cache_provider_impl.py:123-224`) MUST
      NOT открывать DuckDB самостоятельно (`:189`) и MUST NOT разрешать путь сам
      (`:177-186`); собственный `except Exception: return []` (`:190`) маскирует
      недоступность файла под «каталог пуст», а импорт
      `from lib.core.skill_config import _WORKSPACE_ROOT` (`:182`) падает
      `ImportError`, т.к. символ в модуле **отсутствует**. Реализация — в
      change `fix-cache-process-boundary` §2
- [ ] 5.11 Guard чистоты интерфейса: `CacheProvider` не имеет `open` /
      `open_cache` / `try_claim` / `heartbeat` / `acquire_write_fence` /
      `release`; подклассы не добавляют lifecycle-методов; точка создания
      провайдера — **единственная** функция 5.4, а не метод ABC
- [ ] 5.12 **Cache-файл — process-exclusive ресурс (решение 2026-09-29).**
      Один активный владелец DuckDB-файла. Процесс, который не смог
      получить требуемый доступ к кэшу при инициализации, **не считается
      успешно запущенным**: startup завершается типизированной ошибкой.
      Правило одинаково для gateway, CLI, benchmark и любого будущего
      отдельного процесса — оно о свойстве ресурса, а не о способе запуска.
- [ ] 5.13 **Проверкой является сама попытка открыть файл, а не отдельный
      pre-check.** Отдельная проверка занятости (`if file.locked()`) и
      последующее открытие разделены во времени и дают гонку: два процесса
      могут оба увидеть «свободно» и оба упасть при открытии. Контракт
      поэтому: `startup → open_cache_provider() → duckdb.connect` и разбор
      результата. Никакого TOCTOU-окна между «проверили» и «открыли» в
      коде быть не MUST. verify: в `lib/` нет отдельного вызова проверки
      занятости файла перед `open`
- [ ] 5.14 Startup-путь перестаёт деградировать при конфликте. Сегодня
      `DuckDbCacheStore.open()` возвращает инстанс, а неудача скрывается
      до первого использования (`connect()` → `False` → «кэш недоступен»).
      Требуется: ошибка занятости файла другим процессом MUST подниматься на
      границе инициализации, с указанием, какой процесс держит файл, и MUST
      NOT выдаваться за «файл не найден» и MUST NOT приводить к тихому
      запуску без кэша
- [ ] 5.15 `CacheOwnershipCoordinator` **сохраняется**, но его роль сужается
      до координации записи/синхронизации (claim, heartbeat, generation,
      write fence). Он MUST NOT превращаться в механизм передачи открытого
      файла между процессами: процесс, чей claim не получен, запрашивает
      `READ_ONLY` и открывает файл сам; если файл занят — он падает на
      5.12, а не ждёт освобождения
- [ ] 5.16 Типизированная ошибка занятости: отдельный класс (наряду с
      «файл отсутствует» и «неподдерживаемая FS» из §7.3), различающая
      «кэш держит другой процесс» и «кэш недоступен по другой причине».
      Сообщение MUST называть конфликтующий процесс и MUST NOT предлагать
      пользователю запустить тот процесс, который и создаёт конфликт

## 6. Удаление `cache_store` alias (Decision 12)

- [ ] 6.1 `gateway.py` — 13 ссылок (строки 157, 158, 162, 174, 184, 224, 226, 268, 270, 271, 274, 295, 297)
- [ ] 6.2 `lib/services/project_tool_loader.py` — 11 ссылок (144, 156, 189, 190, 201, 213, 267, 284, 287, 295, 296); параметр переименовать в `cache_provider`
- [ ] 6.3 `lib/services/runtime_patcher.py` — 2 ссылки (583, 602)
- [ ] 6.4 `benchmarks/runner.py` — 4 ссылки (615, 618, 620, 654)
- [ ] 6.5 `lib/core/application_context.py` — удалить поле-legacy (`:152`), присваивание (`:350-353`), прокидывание в `patch_project_tools`/`patch_subagent_logging` (`:468`, `:494`), `getattr(ctx, "cache_store", ...)` в readiness (`:1069`, `:1092`) и тексты docstring (`:1010-1011`, `:1073`, `:1082`, `:1096`, `:1101`)
- [ ] 6.6 `tests/test_application_context_role.py:96` — убрать `cache_store` из ожидаемого множества атрибутов
- [ ] 6.7 AST-тест `tests/test_cache_provider_layering.py::TestNoCacheStoreAliasInRuntime`: ни одной ссылки на `cache_store` в `lib/`, `workspace/`, `tools/`, `benchmarks/`, `tests/` (кроме исторического упоминания в docstring `release_v252.py`)
- [ ] 6.8 CHANGELOG: запись в `Removed` (не `Deprecated`) — alias удалён в этом change, без переходного релиза

## 7. Storage safety (Decision 8)

- [ ] 7.1 `lib/utils/filesystem_prober.py`: `detect_network_filesystem(path) -> bool` (Linux/macOS/Windows)
- [ ] 7.2 Подключить проверку в `DuckDbCacheStore` до открытия storage; сетевая FS → `UnsupportedFilesystemError`
- [ ] 7.3 Типизированные open-ошибки: «файла нет» / «файл залочен» / «неподдерживаемая FS» различимы
- [ ] 7.4 Тесты `tests/test_duckdb_cache_store.py`: macOS NFS, Windows SMB (`DRIVE_REMOTE`), локальная FS — ok

## 8. Верификация (Phase 8)

- [ ] 8.1 `python -m pytest tests/ -q` — без новых падений относительно baseline
- [ ] 8.2 `python tools/architecture_guard.py` — без новых нарушений
- [ ] 8.3 `python cli_agent.py --smoke`, `python gateway.py --profile=test --smoke` — exit 0
- [ ] 8.4 Обновить `docs/ARCHITECTURE.md`, `AGENTS.md` (§ «Project Layout»), `CHANGELOG.md`
- [ ] 8.5 `openspec.cmd validate cache-architecture-alignment`
- [ ] 8.6 `openspec.cmd archive cache-architecture-alignment` — спека `data/cache-runtime-lifecycle` переносится в canonical

## Вне scope этого change

- Протечки абстракции skill ↔ storage-реализация (`cache_provider_impl` в skill'е,
  `hasattr(open_cache)`, тип возврата фабрики, self-open DuckDB в
  `list_runtime_vector_indexes`) — `fix-cache-process-boundary`
- Кросс-процессная блокировка одного файла **не является открытым вопросом**:
  контракт решён в §5.12-5.16 — кэш process-exclusive, проверкой служит сама
  попытка открытия, конфликт фатален на старте, предварительная проверка
  занятости запрещена как TOCTOU
- `CliChannel`, native TUI, удаление `role`, расположение `CronService`, Gateway client protocol, typed outbound events — `unify-runtime-channels`
- `return_file_manager=not ctx.enable_cron` (`application_context.py:315`) — переносит `unify-runtime-channels`
- Изменение default `enable_cron`
- Удаление `streamlit_app.py` — `remove-streamlit-runtime`
