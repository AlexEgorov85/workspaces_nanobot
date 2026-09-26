## Context

Текущее состояние (см. `proposal.md` для мотивации):

- `nanobot-ai==0.3.5` зафиксирован в `requirements.txt`. Upstream
  `nanobot.session.manager.SessionManager` использует
  `JsonlSessionStore` (горячее JSONL-хранилище), а не SQLite.
  Upstream `nanobot.llm_usage.store.LLMUsageStore` использует
  SQLite WAL для content-free metadata-only LLM usage.
- `lib/session/pg_session_manager.py` (~410 строк) — subclass
  `SessionManager`, который **подменяет** upstream JSONL-стор
  на прямой SQL `INSERT/UPDATE/DELETE` в `agent_session_meta` /
  `agent_session_messages`. Это нарушает контракт `logging-db`
  (который ещё не applied, но уже зафиксирован в change
  `unify-agent-event-logging-pipeline`).
- `lib/services/db_logging_service.py` — content-rich writer
  `agent_gateway_logs`, контракт source-of-truth для
  `history_search` (см. спеку `tools-history-search`).
- Существующая спека `runtime/context` (146 строк) говорит:
  «состояние сессии хранится в `PGSessionManager`» — это
  требует обновления под новую модель.
- `PGSessionManager` используется в 56 местах: `cli_agent.py`,
  `lib/services/runtime_patcher.py`, `lib/core/application_context.py`,
  `lib/services/context_compaction.py`, `workspace/utils/db.py`,
  `workspace/hooks/active_files_hook.py`, `lib/session/pg_session_manager.py`,
  `tools/generate_comments_sql.py`, `lib/services/session_storage.py`,
  `lib/services/channel_factory.py`, `lib/core/agent_factory.py`,
  `benchmarks/runner.py`, плюс 6 файлов тестов.

## Goals / Non-Goals

**Goals:**

1. Перенести hot-path запись/чтение сессий в upstream
   `SessionManager` (JSONL). Убрать прямой SQL `INSERT/UPDATE/
   DELETE` в hot path из `PGSessionManager`.
2. Сохранить PostgreSQL как cold-storage mirror через
   отдельный фоновый `SessionColdSyncService` с `last-write-wins`
   семантикой.
3. Подключить upstream `LLMUsageStore` через observer-pipeline
   nanobot 0.3.5; не подменять и не дублировать `DbLoggingService`.
4. Обновить спеку `runtime/context` под новые границы.
5. Сохранить все существующие таблицы (`agent_session_meta`,
   `agent_session_messages`, `agent_gateway_logs`,
   `agent_question_runs`) — это cold-storage mirror, не
   удаляемый.

**Non-Goals:**

- Изменение схемы PG-таблиц (DDL остаётся как есть; mirror
  пишется в существующие таблицы).
- Изменение `DbLoggingService` (контракт `logging-db`
  применяется отдельным change
  `unify-agent-event-logging-pipeline`; наш change только
  ссылается на него).
- Перенос `agent_vector_index_store`, `agent_worker_claims`,
  `history_search` FTS на другой стор (PG обязателен для
  BYTEA / `FOR UPDATE SKIP LOCKED` / FTS для русских текстов).
- Изменение `ContextCompactionService` (его `_write_history_notice`
  в `agent_conversation_messages` остаётся согласно
  спеке `logging-db`).
- Изменение domain skills (`audit_analyzer`,
  `legal_summarizer`, `office_files`).
- Изменение профилей `prod`/`test`.
- Изменение benchmark suite.
- Изменение observer-pipeline API в nanobot 0.3.5 (только
  использование того, что уже есть).

## Decisions

### D1: Hot path сессий через upstream `SessionManager`, не подмена

**Решение.** Использовать upstream `SessionManager` как
единственный hot-path владелец сессий. `PGSessionManager`
перестаёт писать в `agent_session_meta` /
`agent_session_messages` напрямую. Все hot-path операции
(`get_or_create`, `save`, `list_sessions`,
`read_session_metadata`, `fork_session_before_user_index`,
`rename_model_preset`) идут через upstream API → JSONL.

**Обоснование.**

- Upstream `SessionManager` уже имеет весь нужный API:
  `get_or_create(key) -> Session`, `save(session, *, fsync=False)`,
  `list_sessions() -> list[dict]`, `read_session_metadata(key)`,
  `fork_session_before_user_index(...)`, `rename_model_preset(...)`.
- Upstream `Session` (dataclass с полями `key`, `messages`,
  `created_at`, `updated_at`, `metadata`, `last_consolidated`,
  `provider_state`, `policy`) уже поддерживает всё, что нам
  нужно: `provider_state` хранит runtime-стейт провайдера,
  `metadata` — произвольные данные сессии,
  `policy` — поведенческие настройки.
- Удаление прямого SQL из hot path устраняет 56 call-sites
  зависимости от `PGSessionManager`-специфичных kwargs
  (`dsn`, `schema`, `messages_table`, `meta_table`).

**Альтернативы.**

- (а) Оставить `PGSessionManager` как единственный hot-path
  writer. Отклонено: блокирует upstream-совместимость
  (`runtime/context` спека говорит про upstream; `tools-history-search`
  спека говорит про upstream); требует monkey-patch на
  каждый апгрейд nanobot.
- (б) Заменить `PGSessionManager` на собственную обёртку
  поверх upstream `SessionManager` без PG. Отклонено: теряем
  cold-storage для multi-instance и disaster-recovery.

### D2: `SessionColdSyncService` — отдельный фоновый сервис в `daemon` потоке

**Решение.** Ввести `SessionColdSyncService` в
`ApplicationContext._make_*`. Сервис работает в **отдельном
daemon-потоке** (по образцу `PgDuckDbSyncService.start()`
→ `self._thread = threading.Thread(target=self._worker,
name="audit-sync", daemon=True)`).

**Почему отдельный поток, а не `asyncio.create_task`?**
Sync-код (psycopg2 + `threading.Lock`) **блокирует event
loop**, если вызывается напрямую из async-контекста.
`PgDuckDbSyncService` уже использует этот паттерн;
повторяем его для консистентности.

```python
# lib/services/session_cold_sync_service.py (новый файл)
import threading

class SessionColdSyncService:
    def __init__(self, ...):
        self._state_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._running = False

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(
            target=self._worker, name="session-cold-sync", daemon=True
        )
        self._thread.start()

    def _worker(self) -> None:
        while self._running and not self._stop_event.is_set():
            try:
                with self._state_lock:
                    self._sync_cycle()
            except Exception as exc:
                self._consecutive_failures += 1
                self._log_sync_failure(exc)
            delay = self._compute_delay()
            self._stop_event.wait(timeout=delay)

    def stop(self, timeout_sec: float = 30.0) -> None:
        self._running = False
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout_sec)
            self._thread = None
```

**Обязательный flush в `stop()`** (для D21 shutdown order):
если `_state_lock.acquire(timeout=timeout_sec)` успешен,
выполняется **финальный `_sync_cycle()`** перед полным
завершением — иначе последние dirty-сессии теряются.

**Обоснование.**

- Разделение concerns: hot path не блокируется на PG,
  cold-storage работает независимо и переживает
  временную недоступность PG.
- Существующий `PgDuckDbSyncService` — образец: уже
  реализует аналогичный паттерн для DuckDB-кэша, есть
  DI-инфраструктура в `ApplicationContext.start()` /
  `_lifecycle.shutdown`.
- `last-write-wins` по `updated_at` —
  минимально-инвазивная conflict-resolution для multi-instance.
  Колонка `updated_at` уже есть в DDL `agent_session_meta`
  (см. `sql/session/create_public_agent_session_meta.sql`),
  никаких изменений схемы не требуется.

**Альтернативы.**

- (а) Trigger-based sync (PG trigger на апдейт upstream
  JSONL-стора). Отклонено: nanobot JSONL — это не БД,
  триггеры не применимы.
- (б) WAL-mode replication. Отклонено: nanobot JSONL —
  это не PostgreSQL, нет WAL.
- (в) CRDT-based merge. Отклонено: overkill для нашего
  use-case (одноактантный writer — наш процесс).

### D3: `LLMUsageStore` подключается через observer-pipeline

**Решение.** `ApplicationContext.start()` создаёт
`LLMUsageStore(path)` и регистрирует `store.record` как
LLM-call observer через `provider_snapshot_loader`
callback. Это **единственный стабильный путь подключения**
в nanobot 0.3.5, потому что проект использует кастомный
`gateway.py` и `ApplicationContext`, а не upstream
`nanobot.cli.gateway_runtime` (где observer подключается
автоматически через `_observe_provider`).

**Реализация.**

```python
# lib/services/llm_observer.py (новый файл)
from nanobot.llm_usage.store import LLMUsageStore
from nanobot.providers.base import LLMProvider
from nanobot.providers.fallback_provider import FallbackProvider

def attach_llm_observer(provider: LLMProvider, store: LLMUsageStore) -> None:
    """Подключает observer к одному провайдеру.

    Подтверждено через интроспекцию nanobot 0.3.5:
    ``FallbackProvider.set_llm_call_observer(observer)``
    сам пробрасывает observer в ``self._primary`` и во все
    future fallback leaves (см.
    ``nanobot/providers/fallback_provider.py``). Поэтому
    достаточно вызвать ``set_llm_call_observer`` ОДИН раз
    на ``FallbackProvider`` — fan-out произойдёт
    автоматически.
    """
    provider.set_llm_call_observer(store.record)

def attach_fallback_model_observer(provider, bus_or_handler) -> None:
    """Подключает observer для fallback-model переключений.

    ``set_fallback_model_observer`` — отдельный observer
    для семантических событий смены модели (НЕ для LLM
    calls). Подключается только если провайдер —
    FallbackProvider (по образцу
    ``nanobot.cli.gateway_runtime._observe_provider``).
    """
    if isinstance(provider, FallbackProvider):
        provider.set_fallback_model_observer(build_fallback_observer(bus_or_handler))

def wrap_provider_snapshot_loader(base_loader, store: LLMUsageStore, bus=None):
    """Обёртка вокруг provider_snapshot_loader, вызывающая
    attach_llm_observer на каждом загруженном ProviderSnapshot."""
    def wrapped(*, preset_name=None, **kwargs):
        snapshot = base_loader(preset_name=preset_name, **kwargs)
        attach_llm_observer(snapshot.provider, store)
        if bus is not None:
            attach_fallback_model_observer(snapshot.provider, bus)
        return snapshot
    return wrapped
```

В `ApplicationContext.start()` (после создания
`LLMUsageStore` и `AgentLoop`):

```python
agent = AgentLoop.from_config(
    config,
    bus,
    provider_snapshot_loader=wrap_provider_snapshot_loader(
        base_loader=build_provider_snapshot,
        store=self.usage_store,
    ),
    # ... остальные kwargs
)
```

**Обоснование.**

- Подтверждено через интроспекцию upstream 0.3.5:
  - `nanobot.providers.base.LLMProvider.set_llm_call_observer(observer)`
    — публичный API, принимает `Callable[[LLMCallRecord], None]`.
  - `LLMCallObserver = Callable[["LLMCallRecord"], None]` —
    type alias из `nanobot/providers/base.py`.
  - `record_llm_call` (дефолтный callback upstream) оборачивает
    `get_llm_usage_store().record(call)` в try/except — fail-open
    семантика гарантирована upstream: ошибка записи в SQLite
    НЕ прерывает LLM-вызов.
- `nanobot.cli.gateway_runtime._observe_provider` делает
  ровно то же, но мы его не вызываем (наш gateway.py —
  не upstream CLI).
- `provider_snapshot_loader` — это callback, который
  `AgentLoop` вызывает для получения `ProviderSnapshot`
  (lazy/перезагрузка); оборачивая его, мы гарантируем,
  что любой снапшот (включая пересозданный при reload
  или смене model preset) уже содержит подключённый
  observer.

**Поведение при ошибке attach.** `set_llm_call_observer`
на практике не бросает исключений (это простое присваивание
`self._llm_call_observer = observer`), но если upstream изменит
это поведение в будущем — `wrap_provider_snapshot_loader`
SHALL логировать WARNING через `loguru` и продолжить работу
без observer (а не падать). Метрика `llm_observer_attached=0`
экспортируется в health-check (см. `lib/services/runtime_health.py`).
Агент SHALL NOT быть остановлен из-за потери observer — usage
tracking — observability concern, не business-critical.

**Альтернативы.**

- (а) Вызвать `provider.set_llm_call_observer` напрямую
  в `ApplicationContext.start()` после создания `AgentLoop`.
  Отклонено: `AgentLoop` создаёт `provider` внутри
  `from_config(...)` и **не возвращает** его; мы не имеем
  прямого доступа к объекту.
- (б) Использовать upstream `record_llm_call` callback
  напрямую (без своего `LLMUsageStore`). Отклонено:
  путь к файлу SQLite жёстко зашит в
  `nanobot.llm_usage.llm_usage_store_path()` →
  `get_data_dir() / "llm_usage.sqlite3"`; нет override
  через конфиг.
- (в) Подписка на `bus.subscribe(handler, LLMCallCompleted)`
  как fallback. Отклонено: в upstream 0.3.5 такого
  `LLMCallCompleted`-типа нет — `LLMCallObserver` —
  это `Callable`, а не `AgentEvent`-подкласс.

**Подтверждение через контрактный тест
`tests/contract/test_llm_observer_api.py`** (см. task 1.4):

- `test_llm_provider_has_set_llm_call_observer` —
  проверяет наличие публичного метода.
- `test_record_llm_call_is_fail_open` — вызывает
  `record_llm_call` на замоканном `LLMUsageStore`,
  бросающем исключение; проверяет, что callback не
  пробрасывает исключение наружу (no-op).
- `test_provider_snapshot_loader_callback_signature` —
  проверяет, что обёрнутый callback возвращает
  `ProviderSnapshot` с подключённым observer.

### D4: Конфигурируемость UsageStore

**Решение.** Путь к SQLite-файлу `LLMUsageStore` —
`gateway.usage_store.sqlite_path` в `project.json`
(опционально, дефолт — `get_runtime_subdir("usage") / "usage.db"`).
Никаких других ключей конфигурации: retention и
WAL-параметры hardcoded в upstream `LLMUsageStore`
(`MAX_CALLS_RETAINED`, `MAX_DAYS_RETAINED`).

**Обоснование.**

- Upstream `LLMUsageStore` не предоставляет конфигурации
  retention / WAL в 0.3.5 (поля hardcoded в
  `nanobot/llm_usage/store.py`).
- Дефолт через `get_runtime_subdir("usage")` следует
  upstream-конвенции для runtime-данных.
- Возможность override через `project.json` —
  для тестов и multi-instance deploy.

**Альтернативы.**

- (а) Полная конфигурируемость (включая retention,
  WAL-mode). Отклонено: upstream эти поля не
  параметризует; введение собственных параметров
  создаст второй источник истины.
- (б) Хардкод пути. Отклонено: тестам нужен override.

### D5: Контрактные тесты на upstream API

**Решение.** Контрактные тесты на `SessionManager` /
`LLMUsageStore` / observer-pipeline фиксируют публичный
API nanobot 0.3.5. Тесты MUST запускаться первыми при
апгрейде upstream и MUST падать при изменении совместимого
API. Это страховка от регрессий при следующих минорных
апдейдах nanobot.

**Обоснование.**

- Прямой `inspect.signature()` для каждого используемого
  upstream-символа.
- Прямой тест инициализации с дефолтным стором
  (`SessionManager(workspace=tmp_path)`, `LLMUsageStore(tmp_path / "usage.db")`).
- Список tests фиксируется в `tasks.md`.

**Альтернативы.**

- (а) Никаких контрактных тестов. Отклонено: при апгрейде
  upstream (1-2 месяца) тесты упадут в production без
  предупреждения.
- (б) Mocks без реального upstream. Отклонено: цель
  контрактных тестов — зафиксировать upstream API, а не
  наш код.

### D-Pool: Правила использования пула PG-соединений

`SessionColdSyncService` использует общий пул ``utils.db``
(см. `workspace/utils/db.py`). Никаких собственных
psycopg2-пулов, никаких ``connect()`` в модуле — все правила
ниже обязательны для исполнения.

#### Правило 1. Получение пула — только через DI

Все новые компоненты (``SessionColdSyncService``, любые будущие
sync-сервисы) получают пул через ``utils.db.transaction()`` /
``utils.db.run()``. ``utils.db.configure(dsn)`` вызывается
**один раз** из ``ApplicationContext`` и неявно инициализирует
синглтон-менеджер пула.

В модуле **запрещено**:

- ``psycopg2.pool.SimpleConnectionPool(...)`` / ``ThreadedConnectionPool(...)`` /
  ``AbstractConnectionPool(...)``;
- ``psycopg2.connect(...)`` / ``asyncpg.create_pool(...)``;
- ``DBManager(...)`` напрямую (только через ``utils.db``-helpers).

**Гард:** ``tests/test_storage_hybridization.py`` —
``test_no_new_pool_created``: AST-проход по
``lib/services/session_cold_sync_service.py`` ищет
``SimpleConnectionPool``, ``psycopg2.pool``, ``connect(``,
``create_pool``; любое совпадение → ``pytest.fail``.

#### Правило 2. Advisory lock — через `pg_try_advisory_xact_lock`

`pg_try_advisory_lock` живёт на конкретном соединении, что плохо
совместимо с пулом ``utils.db`` (lease выдаётся на один job).
Решение — **per-transaction lock**:

- на каждом sync-цикле внутри одной транзакции вызывается
  ``SELECT pg_try_advisory_xact_lock(hashtext('storage_hybridization_session_cold_sync')::bigint)``;
- если результат ``False`` — цикл пропускается
  (``cycles_skipped_lock_busy += 1``);
- lock автоматически освобождается на COMMIT/ROLLBACK (xact-scoped);
- никакого долгоживущего соединения и никакого ручного unlock.

Никогда не использовать ``pg_try_advisory_lock`` (session-level) в
комбинации с пулом — это приведёт к «зависшему» lock'у.

#### Правило 3. Threading model: acquire/release в одном worker-потоке

Пул ``utils.db`` устроен так, что каждый job отправляется в
отдельный worker-поток, и lease на соединение выдаётся в том же
worker'е (см. ``utils.db._acquire_lease`` / ``_release_lease``).
Это означает:

- acquire/release соединения **происходит в одном и том же
  worker-потоке** пула;
- ``SessionColdSyncService._worker`` (daemon-thread) только
  отправляет job'ы (``utils.db.run(...)``), но **не держит
  соединение сам**;
- **запрещено** брать соединение в одном worker-потоке и
  возвращать в другом (для ``psycopg2.pool`` это не всегда
  безопасно; для ``utils.db`` это нарушает инвариант lease).

#### Правило 4. Батчи и порядок

Sync пишет в ``agent_session_meta`` / ``agent_session_messages``.
Чтобы не ловить deadlock с ``DbLoggingService`` (тот тоже пишет в
``agent_gateway_logs`` в этом же пуле):

- сессии обрабатываются **батчами** (default ``batch_size=50``,
  настраивается через ``gateway.session_cold_sync.batch_size``);
- перед батчингом список upstream-сортируется по ``session_key``
  (детерминированный порядок блокировок → исключает ABBA-deadlock
  с DbLoggingService);
- транзакция per session_key (не один гигантский batch на весь
  workspace), чтобы не держать долгий lock.

#### Правило 5. Обработка pool busy / TimeoutError

Если пул временно исчерпан (``utils.db.run(...)`` бросает
``RuntimeError`` / ``TimeoutError`` / ``PoolError``):

- **не падать** с критической ошибкой;
- логировать ``event_type="session_cold_sync_failed"`` через
  ``try_log_event``;
- инкрементить ``cycles_skipped_pool_busy`` (отдельная метрика
  от lock-busy);
- ждать следующий цикл через backoff из D13.

#### Правило 6. Метрики пула в `RuntimeHealth.get_stats()`

Метрики обязательны (без них нельзя отличить «sync не работает,
потому что реплика не лидер» от «sync не работает, потому что
пул занят»):

- ``pool_size`` (текущий размер пула, ``int | None`` если недоступен);
- ``pool_available`` (свободные соединения, ``int | None``);
- ``pool_wait_seconds`` (время последнего цикла, включая ожидание
  lease'а);
- ``cycles_skipped_lock_busy`` (advisory-lock занят);
- ``cycles_skipped_pool_busy`` (пул исчерпан).

Гард: ``tests/test_session_cold_sync_service.py::test_get_stats_shape``
проверяет наличие всех 5 ключей.

#### Правило 7. Shutdown order

``SessionColdSyncService.stop()`` → освободить advisory lock
(автоматически на ROLLBACK последней транзакции) → дождаться
завершения текущего worker-цикла → вернуть lease (автоматически).
Никаких дополнительных действий с пулом — ``utils.db`` не
закрывается на shutdown сервиса (он живёт до завершения всего
приложения).

### D6: `PGSessionManager` остаётся как compatibility layer

**Решение.** Класс `PGSessionManager` в
`lib/session/pg_session_manager.py` сохраняется **исключительно
как тонкий compatibility layer** для 56 call-sites в коде:

- docstring обновляется: «hot-path methods delegate to
  upstream `SessionManager`; legacy PG tables are
  read-only via `PGSessionLegacyReader`; no PG writes in
  hot path»;
- hot-path методы `get_or_create` / `save` /
  `list_sessions` / `read_session_metadata` / etc.
  делегируются в `super()` (`upstream SessionManager`) —
  никаких SQL-операций в hot path;
- mirror-операции выносятся в `SessionColdSyncService`;
  legacy read — в `PGSessionLegacyReader`;
- класс остаётся `SessionManager`-subclass для type-checker'а
  и обратной совместимости импортов.

**Обоснование.**

- 56 call-sites `PGSessionManager` продолжают работать
  без переписывания (`from lib.session.pg_session_manager
  import PGSessionManager` остаётся валидным).
- Класс становится **тонкой обёрткой**, а не подменой
  upstream. Никаких side-effect'ов в hot path.
- Альтернативы (полное удаление, переименование) — breaking
  changes с минимальной функциональной выгодой.

**Альтернативы.**

- (а) Полное удаление `PGSessionManager` и замена на
  тонкую обёртку. Отклонено: 56 call-sites; преимущества
  минимальны.
- (б) Переименование в `PGSessionMirror`. Отклонено:
  breaking change без функциональной выгоды.

**Ограничение (явное):** `PGSessionManager` остаётся в коде,
но НЕ используется как hot-path writer. Если разработчик
импортирует `PGSessionManager` и вызывает его методы, он
получает upstream-поведение (mirror в JSONL); никакого
прямого PG-write не происходит. Architecture-guard
(`tests/test_storage_hybridization.py`) проверяет это
инвариант на уровне репозитория.

### D10: Legacy PG-сессии остаются read-only (PGSessionLegacyReader)

**Проблема.** До этого change `PGSessionManager` писал сессии
напрямую в PG `agent_session_meta` / `agent_session_messages`.
Эти строки **остаются** в PG после deploy и должны быть
доступны для чтения, но **не** должны зеркалироваться в
upstream JSONL (это нарушит «single-writer per layer»).

**Решение.** Legacy PG-сессии остаются в PG как **read-only
fallback**. Доступ — через отдельный компонент
`PGSessionLegacyReader` (НЕ через `PGSessionManager`).

```python
# lib/session/pg_session_legacy_reader.py (новый файл)
class PGSessionLegacyReader:
    """Read-only доступ к legacy PG-сессиям (созданным до
    storage-hybridization). Используется ТОЛЬКО в admin-утилитах,
    НЕ в hot path.
    """

    def __init__(self, pg_dsn: str, schema: str = "public",
                 meta_table: str = "agent_session_meta",
                 messages_table: str = "agent_session_messages"):
        self._pg_dsn = pg_dsn
        self._schema = schema
        self._meta_table = meta_table
        self._messages_table = messages_table

    def read(self, key: str) -> dict | None:
        """Прочитать legacy сессию по ключу.
        Возвращает dict с полями key, created_at, updated_at,
        last_consolidated, metadata, messages, или None."""
        from utils.db import run
        # SELECT из agent_session_meta + JOIN agent_session_messages
        # по session_key, ORDER BY seq
        ...

    def list_all(self) -> list[dict]:
        """Список всех legacy сессий (для admin/migration tools)."""
        ...

    def count(self) -> int:
        """Количество legacy сессий в PG."""
        ...
```

**Граница изоляции (явная):**

- `PGSessionLegacyReader` импортируется только в
  `tools/dump_legacy_session.py` (admin-утилита).
- Никакой runtime-код (`AgentLoop`, `Channel`,
  `ApplicationContext`, runtime-hooks) НЕ импортирует
  `PGSessionLegacyReader`.
- Architecture-guard (`tests/test_storage_hybridization.py`)
  проверяет это правило.

**Что НЕ делается:**

- ❌ НЕ мигрируем legacy PG-сессии в JSONL (изменение
  схемы `agent_session_meta` через миграцию; hot-path
  lookup-fallback не нужен — `PGSessionLegacyReader`
  достаточно для admin-доступа).
- ❌ НЕ удаляем `agent_session_meta` /
  `agent_session_messages` после deploy — таблицы
  остаются для reference и future-migration.
- ❌ НЕ включаем legacy сессии в `SessionColdSyncService`
  (sync работает только с upstream `list_sessions()`).

**Rollback.** Если legacy данные нужны приложению (например,
admin-команда восстанавливает историю пользователя),
запускается `tools/dump_legacy_session.py` и выдаёт
JSON-дамп сессии. Миграция в JSONL — отдельный будущий
change, если потребуется.

**Документация.** `docs/MIGRATION.md` § «Legacy PG sessions»:

```markdown
## v3.x.0: Storage hybridization

### Legacy session data
Sessions created before `storage-hybridization` (in
`agent_session_meta` / `agent_session_messages`) remain
in PostgreSQL as read-only data. They are NOT mirrored to
upstream JSONL.

To inspect a legacy session:
```bash
python tools/dump_legacy_session.py --key "telegram:12345"
```

To count legacy sessions:
```bash
python tools/dump_legacy_session.py --count
```

Future migration of legacy data to JSONL is a separate
OpenSpec change (NOT in this one). Until then, legacy
data is accessible only via `tools/dump_legacy_session.py`.
```

**Решение.** Класс `PGSessionManager` в
`lib/session/pg_session_manager.py` сохраняется **исключительно
как тонкий compatibility layer** для 56 call-sites в коде:

- docstring обновляется: «hot-path methods delegate to
  upstream `SessionManager`; legacy PG tables are
  read-only via `PGSessionLegacyReader`; no PG writes in
  hot path»;
- hot-path методы `get_or_create` / `save` /
  `list_sessions` / `read_session_metadata` / etc.
  делегируются в `super()` (`upstream SessionManager`) —
  никаких SQL-операций в hot path;
- mirror-операции выносятся в `SessionColdSyncService`;
  legacy read — в `PGSessionLegacyReader`;
- класс остаётся `SessionManager`-subclass для type-checker'а
  и обратной совместимости импортов.

**Обоснование.**

- 56 call-sites `PGSessionManager` продолжают работать
  без переписывания (`from lib.session.pg_session_manager
  import PGSessionManager` остаётся валидным).
- Класс становится **тонкой обёрткой**, а не подменой
  upstream. Никаких side-effect'ов в hot path.
- Альтернативы (полное удаление, переименование) — breaking
  changes с минимальной функциональной выгодой.

**Альтернативы.**

- (а) Полное удаление `PGSessionManager` и замена на
  тонкую обёртку. Отклонено: 56 call-sites; преимущества
  минимальны.
- (б) Переименование в `PGSessionMirror`. Отклонено:
  breaking change без функциональной выгоды.

**Ограничение (явное):** `PGSessionManager` остаётся в коде,
но НЕ используется как hot-path writer. Если разработчик
импортирует `PGSessionManager` и вызывает его методы, он
получает upstream-поведение (mirror в JSONL); никакого
прямого PG-write не происходит. Architecture-guard
(`tests/test_storage_hybridization.py`) проверяет это
инвариант на уровне репозитория.

## Risks / Trade-offs

- **R1: При апгрейде upstream nanobot могут сломаться
  контракты `SessionManager` / `LLMUsageStore`.**
  → Митигация: контрактные тесты (D5) запускаются первыми;
  при падении — freeze merge до обновления нашего кода.
- **R2: `SessionColdSyncService` может перегрузить PG
  при большом объёме сессий.** → Митигация:
  батчинг (batch_size=100), `ON CONFLICT DO UPDATE WHERE
  EXCLUDED.updated_at > existing.updated_at` для
  `last-write-wins`, метрики `sync_cycles_total` /
  `sync_rows_total` через `DbLoggingService.log_sync_event`
  (по образцу `PgDuckDbSyncService`).
- **R3: Mirror-write может перезаписать более новые
  данные при split-brain сценарии
  (два gateway-инстанса).** → Митигация: `updated_at`-based
  `last-write-wins` (`ON CONFLICT DO UPDATE WHERE
  EXCLUDED.updated_at > existing.updated_at`) —
  единственная защита (без `version`). При clock skew между
  инстансами возможен теоретический overwrite; митигация —
  NTP-синхронизация инстансов.
- **R4: Тесты вне upgrade-скоупа могут замаскировать
  регрессии.** → Митигация: при apply этого change
  `tests/test_pg_session_manager.py` обновляется под
  новую роль (mirror-only); старые hot-path тесты
  удаляются, новые mirror-тесты добавляются.
- **R5: Observer-pipeline API может быть недоступен в
  некоторых провайдерах.** → Митигация: fallback на
  `bus.subscribe(handler, LLMCallCompleted)` через
  контрактный тест `test_llm_observer_api.py`; если ни
  один путь не доступен — `LLMUsageStore` остаётся
  сконфигурированным, но без активной записи (WARNING
  при старте).
- **R6: `PGSessionManager` остаётся как класс — новые
  разработчики могут по ошибке использовать его как
  hot-path writer.** → Митигация: docstring явно говорит
  «cold-storage mirror»; architecture-guard в
  `tests/test_storage_hybridization.py` проверяет, что
  в hot-path runtime-коде (вне `SessionColdSyncService`)
  нет прямого `INSERT INTO ... agent_session_meta`.
- **R7: Multi-instance deploy (несколько gateway-реплик)
  запускают `SessionColdSyncService` параллельно.**
  → Решение: **`pg_try_advisory_lock` для leader-election**
  (см. D14). Каждая реплика пытается захватить
  advisory lock `hashtext('session_cold_sync')` в
  начале каждого sync-цикла; только одна реплика
  получает lock и выполняет sync. Остальные пропускают
  цикл и логируют `cycles_skipped_lock_busy`.
  → Lock освобождается через `pg_advisory_unlock` после
  завершения цикла (в `finally` блоке). При краше
  реплики PG автоматически освобождает lock при
  разрыве соединения.
  → Дополнительный escape hatch: `gateway.session_cold_sync.enabled=false`
  (опционально, default `true`) — sync-сервис не
  запускается вообще (для реплик, которые по политике
  не должны синхронизировать). Lifecycle всё равно
  вызывает `stop()` без ошибок.
- **R8: Legacy PG-сессии от старого `PGSessionManager`
  остаются в `agent_session_meta` / `agent_session_messages`.**
  → Решение: они НЕ трогаются `SessionColdSyncService`
  (только сессии из upstream `list_sessions()`).
  → Доступ к ним — через `PGSessionLegacyReader`
  (admin-утилита `tools/dump_legacy_session.py`),
  изолированную от hot path.
  → Миграция legacy → JSONL — отдельный будущий
  change, если потребуется (см. `docs/MIGRATION.md`).

### D21: Порядок shutdown (явный инвариант)

`ApplicationContext.stop()` выполняет teardown в обратном
порядке относительно `start()`:

1. Health-check endpoint (перестаёт принимать запросы)
2. Channels (PostgresChannel, RedisChannel, ...) —
   перестают принимать новые сообщения
3. AgentLoop — завершает текущие turn'ы (через
   `_lifecycle.shutdown` upstream)
4. **`SessionColdSyncService`** — выполняет **финальный
   flush** (sync dirty sessions в PG), освобождает
   `pg_advisory_lock`, останавливает `_sync_loop`
5. `LLMUsageStore` — `store.close()`
6. `DbLoggingService` — закрывает PG-соединение
7. `PGSessionLegacyReader` — закрывает свой pool (если
   был создан; обычно не создаётся, lazy)
8. `SessionManager` (upstream JSONL) — закрывает
   файловые дескрипторы
9. `CacheProvider` (DuckDB) — закрывает DuckDB-соединение
10. `ConfigService` — освобождает ресурсы

**Ключевой инвариант.** `SessionColdSyncService` (шаг 4)
останавливается **до** `SessionManager` (шаг 8), чтобы
успеть синхронизировать все изменения, пока JSONL ещё
читается. Нарушение этого порядка может привести к
потере финального батча mirror'а.

Реализуется через `_lifecycle.shutdown(steps=[...])` в
`ApplicationContext.stop()` (по образцу существующего
shutdown для `PgDuckDbSyncService`).

## Migration Plan

### Порядок выполнения

1. **Baseline.** `pytest tests/test_pg_session_manager.py tests/test_application_context.py -q` → зафиксировать baseline.
2. **Этап 1 (HIGH, контрактные тесты).**
   - D5: `tests/contract/test_session_manager_api.py`.
   - D5: `tests/contract/test_usage_store_api.py`.
   - D5: `tests/contract/test_llm_observer_api.py`.
3. **Этап 2 (MEDIUM, переключение hot path).**
   - D1: `PGSessionManager.hot-path` → `super()`.
   - D2: `SessionColdSyncService` в
     `lib/core/application_context.py` + lifecycle.
4. **Этап 3 (MEDIUM, LLMUsageStore).**
   - D3: observer-pipeline в `ApplicationContext.start()`.
   - D4: конфиг `gateway.usage_store.sqlite_path`.
5. **Этап 4 (LOW, документация и валидация).**
   - D6: docstring `PGSessionManager` обновляется.
   - `docs/architecture/storage-layers.md` секция
     «Session storage: hot JSONL + cold PG mirror».
   - `docs/architecture/usage-tracking.md` секция
     «LLM usage: upstream LLMUsageStore + DbLoggingService».
   - `docs/MIGRATION.md` раздел про переход.
   - `CHANGELOG.md` блок `## [Unreleased]`.
6. **Validate.** `openspec.cmd validate storage-hybridization
   --strict` → зелёный; `pytest tests/` → зелёный.

### Rollback

`git revert` коммита `storage-hybridization` откатывает
все изменения кода; PG-таблицы `agent_session_meta` /
`agent_session_messages` остаются нетронутыми
(мы только убрали hot-path writer, не удалили данные).
SQLite-файлы `get_runtime_subdir("usage")/usage.db`
также остаются (LLMUsageStore — additive, не destructive).

Если что-то пошло совсем не так (например, observer-pipeline
ломает LLM-вызовы в production):

1. `git revert <commit-hash>`;
2. перезапустить gateway;
3. LLM-вызовы восстановятся, `LLMUsageStore` отключится
   (без observer нет записей);
4. сессии останутся в JSONL (upstream hot path не трогали).

## Open Questions

- **Q1: Использовать ли `bus.subscribe(handler,
  LLMCallCompleted)` как primary или fallback для
  observer-pipeline?** Резолвится в D3 через
  контрактный тест `test_llm_observer_api.py`;
  выбор фиксируется в `tasks.md`.
- **Q2: Нужно ли в `project.json` опцию
  `gateway.usage_store.enabled=false`?** Отложено:
  retention и так hardcoded в upstream; если
  `gateway.usage_store` секция отсутствует — `LLMUsageStore`
  не создаётся (graceful degradation).
