# Tasks: storage-hybridization

## Этап 0: Contract-тесты на текущее поведение (ОБЯЗАТЕЛЬНО перед A2)

Эти тесты фиксируют поведение `_sync_session` ДО рефакторинга условия
(этап A2). Без них есть риск сломать silent-skip для нормального
случая при добавлении stale-detection.

- [ ] 0.1 `test_sync_skips_when_pg_equal_to_jsonl`: при
      `existing_updated_at == upstream_updated_at` sync не выполняется,
      никаких событий не публикуется (текущее поведение).
- [ ] 0.2 `test_sync_skips_when_pg_newer_within_tolerance`: при
      `pg > jsonl`, но `pg - jsonl < stale_tolerance` — sync
      пропускается silently (текущее поведение; после A2 это станет
      explicit "no-op" веткой).
- [ ] 0.3 `test_sync_performs_lww_when_jsonl_newer`: при
      `upstream_updated_at > existing_updated_at` sync выполняется
      (last-write-wins, текущее поведение).

## Этап 1: Контрактные тесты на upstream API

- [ ] 1.1 `test_read_session_metadata_returns_logical_updated_at`:
      проверить, что `updated_at` обновляется при `save()`
      (без `time.sleep`, поддержка ISO-строк).
- [ ] 1.2 `test_session_constructor_signature`: проверить, что
      upstream `Session` принимает `key`/`id` и `messages`.
- [ ] 1.3 `test_db_logging_try_log_event_is_sync`: убедиться,
      что метод sync для использования в фоновом потоке.
- [ ] 1.4 `test_pg_read_does_not_leave_open_transaction`: проверить
      вызов `conn.rollback()` перед `putconn`.

## Этап 2: Интеграция Upstream Components

- [ ] 2.1 Заменить кастомный `PGSessionManager` на upstream
      `SessionManager` (JSONL). Оставить `PGSessionManager` как
      тонкую обёртку для обратной совместимости импортов.
- [ ] 2.2 Инициализировать `LLMUsageStore` и обернуть
      `provider_snapshot_loader` для подключения observer.
- [ ] 2.3 Обновить конфигурацию `project.json` (добавить
      `gateway.session_cold_sync.stale_tolerance_seconds` и
      `sync_lag_threshold_seconds`) — defaults 120 и 3600.
- [ ] 2.4 Пробросить `stale_tolerance_seconds` и
      `sync_lag_threshold_seconds` в конструктор
      `SessionColdSyncService` через `application_context._make_*`:
      - `application_context.py:_make_session_cold_sync_service`
        читает оба ключа из `gateway.session_cold_sync.*`
        (`SessionColdSyncSettings` уже определяет поля);
      - передаёт их в `SessionColdSyncService(...)`;
      - валидация: `sync_lag_threshold_seconds >= stale_tolerance_seconds`,
        иначе `ConfigurationError` на старте.
      - логирует `logger.info(f"stale_tolerance={...}s,
        sync_lag_threshold={...}s")` при инициализации.

## Этап 3: Реализация SessionColdSyncService (Sync-модель)

- [ ] 3.1 Per-transaction advisory lock
      (`pg_try_advisory_xact_lock(hashtext(... )::bigint)` через
      `utils.db.transaction`); xact-scoped — без `_lock_conn`
      (единый пул, D-Pool.1).
- [ ] 3.2 `_read_pg_meta` через `utils.db.transaction()` с
      обязательным `rollback()` на исключении
      (контракт `utils.db.transaction()` сам делает ROLLBACK
      на исключении).
- [ ] 3.3 Worker loop: `_stop_event` (`threading.Event`) +
      `_stop_event.wait(timeout=self._compute_delay())` вместо
      `time.sleep(interval)`; `_running: bool` остаётся как
      per-iteration флаг для graceful shutdown в середине батча.
- [ ] 3.4 **Рефакторинг** условия в `_sync_session` (НЕ просто
      добавление ветки):
      ```python
      if existing_updated_at is None:
          # нормальный sync (новая сессия в PG)
      elif existing_updated_at > upstream_updated_at + tolerance:
          # STALE: лог session_stale_detected, skip
      elif existing_updated_at >= upstream_updated_at:
          # EQUAL/PG-WITHIN-TOLERANCE: silent skip (current behavior)
      else:
          # jsonl > pg → нормальный sync + проверка sync_lag_exceeded
      ```
- [ ] 3.5 Stale-detection: если `pg > jsonl + tolerance`,
      логировать `session_stale_detected` через существующий
      `db_logging_service.try_log_event(...)` и **пропускать** sync
      (`continue`); дедупликация через `_stale_logged_at` TTL=60s.
- [ ] 3.6 Reverse-lag detection: если `jsonl > pg + threshold`,
      логировать `sync_lag_exceeded` через тот же `db_logging_service`
      (sync при этом выполняется нормально — это observability,
      а не skip).
- [ ] 3.7 Счётчики в `get_stats()`:
      - `stale_detected_total`
      - `stale_sync_skipped_total` (все skip из-за stale, включая
        повторные после dedup TTL)
      - `sync_lag_exceeded_total`
      - существующие `cycles_total`/`cycles_failed_total`/
        `cycles_skipped_lock_busy` остаются без изменений.
- [ ] 3.8 Graceful shutdown (`stop()` с адекватным таймаутом и
      `thread.join`).

## Этап 4: Валидация и документация

- [ ] 4.1 `pytest tests/contract/ -q` → 0 failed.
- [ ] 4.2 `pytest tests/ -q --ignore=tests/integration` → 0 failed.
- [ ] 4.3 Тесты stale-detection (этап 0 как регрессионные +
      новые: `test_stale_detected_event_published_when_pg_ahead`,
      `test_stale_event_dedup_within_ttl`,
      `test_reverse_lag_event_published`,
      `test_normal_sync_continues_after_lag_event`).
- [ ] 4.4 Smoke: запустить gateway, убедиться, что sync-поток
      работает, не блокируя main thread, и метрики обновляются.
- [ ] 4.5 Обновить `docs/architecture/storage-layers.md` и
      `CHANGELOG.md`.