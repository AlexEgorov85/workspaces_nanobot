## Context

См. `proposal.md` для мотивации. Краткое состояние:

- `nanobot-ai==0.3.5` зафиксирован в `requirements.txt`. Upstream
  `nanobot.session.manager.SessionManager` использует
  `JsonlSessionStore`; upstream `nanobot.llm_usage.store.LLMUsageStore`
  — SQLite WAL.
- Текущий `PGSessionManager` (`lib/session/pg_session_manager.py`,
  ~410 строк) был subclass `SessionManager`, который подменял
  upstream JSONL-стор на прямой SQL `INSERT/UPDATE/DELETE` в
  `agent_session_meta` / `agent_session_messages`.
- Существует единый пул PG-соединений `workspace/utils/db.py`
  (worker-thread pool с lease'ами); все PG-операции в проекте
  идут через него. Это **единственный** пул, никаких
  параллельных `psycopg2.pool.SimpleConnectionPool` или
  прямых `psycopg2.connect(...)` в runtime-модулях.
- Существующая спека `storage/session-hybridization` (см.
  `openspec/changes/storage-hybridization/specs/`) фиксирует
  «пул — единый» как нормативный контракт (D-Pool).

## Goals / Non-Goals

**Goals:**

1. Реализовать `SessionColdSyncService` в рамках единого пула
   (`utils.db.transaction()` / `utils.db.run()`); никаких
   `psycopg2.connect()` / `SimpleConnectionPool` / `create_pool()`.
2. Поддержать **stale-detection** (D23): если PG свежее JSONL +
   tolerance — пропустить sync для этой сессии и залогировать
   `event_type="session_stale_detected"`.
3. Поддержать **reverse-lag detection**: если JSONL свежее PG +
   threshold — залогировать `event_type="sync_lag_exceeded"`.
4. Сохранить `pg_try_advisory_xact_lock` как per-transaction
   leader-election (auto-release на COMMIT).
5. Сохранить last-write-wins по `updated_at` (без DDL).
6. Сохранить backoff + per-iteration `self._running` проверку.

**Non-Goals:**

- Изменение схемы PG (DDL не меняется).
- Изменение `DbLoggingService` (см. спеку `logging-db`).
- Перенос `agent_vector_index_store` / `agent_worker_claims`
  (PG остаётся обязательным).
- Изменение domain skills.
- Изменение observer-pipeline API nanobot 0.3.5.
- **Не вводить** `pg_pool` / `_lock_conn` / двухкомпонентный
  `pg_try_advisory_lock` (это было в ранней версии дизайна
  и **отменено** — см. § «Отменённое решение»).

## Decisions

### D13: Sync-модель и фоновый поток

Sync-код в `daemon=True` потоке, чтобы не блокировать event loop.
Sync-код с `threading.Lock` совместим с пулом `utils.db`
(worker-потоки пула принимают job'ы от вызывающего daemon'а;
acquire/release соединения происходит в одном worker-потоке).

```python
class SessionColdSyncService:
    def __init__(
        self,
        session_manager: SessionManager,
        pg_dsn: str,
        *,
        db_logging_service: DbLoggingService | None = None,
        stale_tolerance_seconds: int = 120,
        sync_lag_threshold_seconds: int = 3600,
        sync_interval_sec: float = 30.0,
        batch_size: int = 50,
        enabled: bool = True,
    ) -> None:
        self._session_manager = session_manager
        self._stale_tolerance = timedelta(seconds=stale_tolerance_seconds)
        self._sync_lag_threshold = timedelta(seconds=sync_lag_threshold_seconds)
        self._running = True
        self._state_lock = threading.Lock()
        self._sync_lock = threading.Lock()
        self._stale_logged_at: dict[str, datetime] = {}

        # Метрики
        self._cycles_total = 0
        self._cycles_failed_total = 0
        self._cycles_skipped_lock_busy = 0
        self._stale_detected_counter = 0
        self._sync_lag_exceeded_counter = 0
        self._sync_skipped_stale_counter = 0
        self._rows_synced_total = 0
        self._messages_synced_total = 0
        ...

    def start(self) -> None:
        if not self._enabled:
            return
        self._thread = threading.Thread(
            target=self._sync_loop, name="session-cold-sync", daemon=True,
        )
        self._thread.start()

    def _sync_loop(self) -> None:
        while self._running:
            try:
                with self._sync_lock:
                    self._do_sync_batch()
            except Exception as exc:
                self._consecutive_failures += 1
                self._log_failure(exc)
            if self._stop_event.wait(timeout=self._compute_delay()):
                break

    def stop(self, timeout_sec: float = 30.0) -> None:
        self._running = False
        self._stop_event.set()
        self._thread.join(timeout_sec)
        self._thread = None
```

### D23: Stale-detection и reverse-lag detection

Внутри `_do_sync_batch` для каждого upstream-ключа:

```python
def _do_sync_batch(self) -> None:
    if not self._try_advisory_xact_lock():
        self._cycles_skipped_lock_busy += 1
        return

    try:
        upstream_sessions = self._read_upstream_sorted()
        for entry in upstream_sessions:
            # КРИТИЧНО: проверка флага остановки на каждой итерации
            if not self._running:
                return

            jsonl_meta = self._read_upstream_meta(entry["key"])
            pg_meta = self._read_pg_meta(entry["key"])

            # 1. Stale: PG свежее JSONL + tolerance → пропустить sync
            if pg_meta is not None and pg_meta["updated_at"] > jsonl_meta["updated_at"] + self._stale_tolerance:
                if not self._is_stale_logged_recently(entry["key"]):
                    self._log_stale(entry["key"], jsonl_meta["updated_at"], pg_meta["updated_at"])
                    self._stale_logged_at[entry["key"]] = datetime.now()
                    self._stale_detected_counter += 1
                self._sync_skipped_stale_counter += 1
                continue

            # 2. Reverse lag: JSONL свежее PG + threshold → sync сломан
            if pg_meta is not None and jsonl_meta["updated_at"] > pg_meta["updated_at"] + self._sync_lag_threshold:
                self._log_lag_exceeded(entry["key"], jsonl_meta["updated_at"], pg_meta["updated_at"])
                self._sync_lag_exceeded_counter += 1

            # 3. Нормальный sync (LWW)
            self._sync_session(entry["key"], jsonl_meta)
    finally:
        self._consecutive_failures = 0
        # xact-lock освобождается автоматически на COMMIT/ROLLBACK
```

`_is_stale_logged_recently(key)` — дедупликация: возвращает True,
если для этого ключа уже логировали `session_stale_detected`
за последние 60 секунд (anti-flood).

### D14 (отменённое): Выделенное `_lock_conn` через `psycopg2.connect`

**Отменено.** Решение в первоначальной версии дизайна
(выделенное `psycopg2.connect(_lock_conn)`) **противоречит**
спеке `storage/session-hybridization` requirement
«Правила использования пула PG-соединений» (D-Pool): пул —
единый, `pg_pool` / `_lock_conn` запрещены. См. § «Отменённое
решение» для записи альтернативы.

### D-Pool (актуально): правила пула

См. раздел «Connection pool» в `openspec/changes/storage-hybridization/design.md`
(общий для всех модулей проекта):

- Пул — единый `utils.db`. Все PG-операции через
  `utils.db.transaction()` / `utils.db.run()`.
- `pg_try_advisory_xact_lock` — per-transaction; auto-release
  на COMMIT/ROLLBACK. Никакого долгоживущего соединения.
- Батчи с сортировкой по `session_key` (исключает ABBA-deadlock
  с DbLoggingService, которая пишет в тот же пул).

## Risks / Trade-offs

- **R23-A: Stale-detection требует clock skew между инстансами < tolerance.**
  → Использовать NTP; tolerance=120s по умолчанию достаточно для
  типичных NTP-дрейфов.
- **R23-B: Per-key stale-log dedup в memory → при рестарте теряется.**
  → Допустимо: stale-detection — observability concern, не
  критичный; первый sync после рестарта просто запишет события
  заново.
- **R23-C: `stale_logged_at` без TTL → memory growth при большом
  workspace.** → TTL=60s, периодический cleanup в `_sync_loop`.
- **R-Pool: `_try_advisory_xact_lock` не работает при конкурентных
  sync-циклах в одном процессе (один job не может захватить lock
  в уже-выполняющейся транзакции).** → Митигация: внутренний
  `threading.Lock` (D13) + per-iteration `self._running` проверка
  дают single-flight; advisory xact-lock нужен только для
  multi-instance leader-election.
- **R-Compat: `pg_try_advisory_xact_lock` имеет перегрузку
  `(bigint)` и `(int, int)`.** → Используем
  `pg_try_advisory_xact_lock(hashtext(...)::bigint)` (явный
  `::bigint` cast устраняет implicit cast и делает выбор
  перегрузки детерминированным).

## Migration Plan

1. Деплой без ручных миграций: upstream JSONL — source of truth
   с момента deploy; исторические PG-сессии (если были)
   удаляются первым же sync-циклом (если оператор хочет их
   сохранить — отдельный скрипт миграции, вне scope этого change).
2. Rollback: `git revert <commit>`; PG-таблицы остаются
   нетронутыми (sync-сервис только зеркалирует upstream, не
   удаляет таблицу). SQLite-файлы `<runtime>/usage/usage.db` —
   additive.

## Open Questions

Нет.

## Отменённое решение (для истории)

Первоначальная версия дизайна (D14 в коммите `9922bf8`)
предлагала выделенное соединение через `psycopg2.connect()`:

```python
def _get_lock_conn(self):
    if self._lock_conn is None or getattr(self._lock_conn, 'closed', True):
        self._lock_conn = psycopg2.connect(
            self.pg_dsn,
            application_name="nanobot_cold_sync_lock",
            **self._conn_kwargs
        )
    return self._lock_conn
```

с двухкомпонентным `pg_try_advisory_lock(hashtext(%s), 0)`
(где `0` — второй компонент).

**Почему отменено:**

1. Спека `storage/session-hybridization` (нормативный контракт)
   явно запрещает собственные psycopg2-пулы и `connect()`:
   «Пул — единый, через DI. Никаких `SimpleConnectionPool` /
   `psycopg2.pool` / `connect()` / `create_pool`».
2. Проект уже имеет общий `utils.db` worker-thread pool; новое
   выделенное соединение — это race-condition с пулом (lock
   «повиснет» на отдельном соединении, если процесс упадёт).
3. Двухкомпонентный lock — workaround для перегрузки
   `pg_try_advisory_lock(int, int)` (используется для различения
   пространств имён lock'ов); `pg_try_advisory_xact_lock(bigint)`
   делает то же самое через `hashtext(... )::bigint`.

**Альтернатива (применена):** per-transaction
`pg_try_advisory_xact_lock(hashtext('storage_hybridization_session_cold_sync')::bigint)`
через `utils.db.transaction()` — auto-release на COMMIT/ROLLBACK,
никаких долгоживущих соединений, единый пул, threading-safe.