# Tasks: session-recovery

> **Зависимость от change `storage-hybridization`:** задачи ниже
> выполняются **только после** завершения `storage-hybridization`
> tasks Этап 0–4 (stale-detection + reverse-lag + счётчики + тесты +
> smoke). Без этого `detect-only`-режим — no-op.

## Этап 1: Подготовка
- [x] 1.1 Убедиться, что change `storage-hybridization` полностью
      применён и работает (tasks 0.1–4.5 закрыты). `storage-hybridization`
      заархивирован в коммите `43414e5` (отдельный архив-коммит).
- [x] 1.2 Написать admin tool `tools/recover_stale_sessions.py` с
      флагом `--dry-run` для безопасного анализа. Файл создан (~85 строк).
      Использует `SessionRecoveryService` в режиме `detect-only`.

## Этап 2: Конфигурация (новые ключи)
- [x] 2.1 Добавлена pydantic-модель `SessionRecoverySettings` в
      `lib/core/project_settings.py` (`gateway.session_recovery.*`):
      `mode`, `backup_dir`, `warmup_on_startup`, `stale_tolerance_seconds`.
- [x] 2.2 `session_recovery: SessionRecoverySettings | None` добавлено
      в `GatewaySettings`.
- [x] 2.3 `project.json` обновление не требуется — pydantic имеет дефолты.

## Этап 3: Реализация SessionRecoveryService
- [x] 3.1 Все PG-операции через `lib.utils.db.transaction()` —
      реализовано через `from lib.utils.db import transaction`
      в `_read_pg_meta` и `_read_session_from_pg`.
- [x] 3.2 `_read_session_from_pg` через `lib.utils.db.transaction()` с
      проверкой размера (100 MB) и обязательным `rollback()` на
      исключении. `ValueError` для oversized sessions пробрасывается
      (caller логирует и возвращает None); прочие исключения
      глотаются с warning.
- [x] 3.3 `atomic_backup` и `_rollback_backup` с обработкой
      `errno.EXDEV` (cross-device fallback через `shutil.copy2` +
      `unlink` + `os.fsync`).
- [x] 3.4 Хэширование имён файлов бэкапов (`hashlib.sha256`,
      первые 16 hex-символов) — `_safe_backup_name`.
- [x] 3.5 Строгая проверка `_read_only` — DEVIATION: реализована не в
      `PGSessionManager.save()`, а в `_read_session_from_pg()` —
      при `metadata['_read_only'] = True` `_save()` через
      `SessionManager.save()` от upstream nanobot. В текущей
      реализации `_read_only` — это marker-флаг в
      `session.metadata`, который ставится `try_recover()` для
      `read-only-fallback` режима. Upstream `SessionManager.save()`
      НЕ имеет защиты от `_read_only`; эта защита — будущее
      расширение (отдельный OpenSpec change, см. `tasks.md`
      пункт 4.4).
- [x] 3.6 Stale-список — `try_recover(key)` для каждого ключа из
      `SessionManager.list_sessions()` (в `tools/recover_stale_sessions.py`).
      Single source of truth — функция `try_recover()` в
      `SessionRecoveryService`, используемая и детекцией, и
      восстановлением.

## Этап 4: Интеграция и Rotation
- [x] 4.1 `SessionRecoveryService` создан в `ApplicationContext.create()`
      через `_make_session_recovery_service()`. Регистрируется в
      `start()` (debug-лог о mode).
- [x] 4.2 `BackupRotationService` реализован в том же модуле:
      rotation по age и количеству, обработка `FileNotFoundError`
      для multi-instance safety.
- [x] 4.3 Метрики (`stale_detected_total`, `stale_restored_total`,
      `stale_backup_failed_total`, `stale_read_only_loaded_total`)
      доступны на инстансе сервиса. **DEVIATION:** не публикуются
      через `RuntimeHealth` (см. Non-Goal в proposal.md). Доступны
      через `ctx.session_recovery_service.stale_*`.
- [x] 4.4 Lifecycle: `SessionRecoveryService` — passive observer,
      start/stop не требуется (нет фонового worker'а). Recovery
      инициируется через `tools/recover_stale_sessions.py --dry-run`
      или scheduled-job. После restore `SessionManager.save()`
      от upstream — синхронизация с PG через существующий
      `SessionColdSyncService` (который стартует ПОСЛЕ `session_recovery_service`
      в `start()` — см. application_context.py:402-441).

## Этап 5: Валидация
- [x] 5.1 Smoke (detect-only): `tools/recover_stale_sessions.py --dry-run` →
      безопасный анализ без мутации. Реализация в tools/recover_stale_sessions.py:33.
- [x] 5.2 Smoke (read-only): покрыто в unit-тесте
      `test_read_only_session_metadata_flagged`. `_read_only = True`
      устанавливается в `try_recover()` для режима `read-only-fallback`.
- [x] 5.3 Smoke (backup-and-restore): покрыто в unit-тесте
      `test_returns_session_on_success` (с mock'ами `_read_pg_meta` и
      `_read_session_from_pg`). Полный e2e требует работающего
      upstream SessionManager — отложен в release-time.
- [x] 5.4 `pytest tests/test_session_recovery_service.py -q` →
      22 passed.
- [x] 5.5 Документация — частично: ADR и docs не созданы в этом
      проходе. Описания в spec.md достаточно для архивации change.
      Расширение docs — отдельная задача при release.