# Tasks: session-recovery

> **Зависимость от change `storage-hybridization`:** задачи ниже
> выполняются **только после** завершения `storage-hybridization`
> tasks Этап 0–4 (stale-detection + reverse-lag + счётчики + тесты +
> smoke). Без этого `detect-only`-режим — no-op.

## Этап 1: Подготовка
- [ ] 1.1 Убедиться, что change `storage-hybridization` полностью
      применён и работает (tasks 0.1–4.5 закрыты).
- [ ] 1.2 Написать admin tool `tools/recover_stale_sessions.py` с
      флагом `--dry-run` для безопасного анализа. Использует тот же
      staleness-детектор, что и `SessionRecoveryService`.

## Этап 2: Конфигурация (новые ключи)
- [ ] 2.1 Добавить pydantic-модель `SessionRecoverySettings` в
      `lib/core/project_settings.py` (`gateway.session_recovery.*`):
      - `mode: Literal["detect-only", "read-only-fallback",
        "backup-and-restore"] | None = "detect-only"` (default);
      - `backup_dir: Path | None` (default:
        `~/.cache/nanobot/sessions/.backups`);
      - `warmup_on_startup: bool | None = False`;
      - `stale_tolerance_seconds: int | None` (default = `None`,
        fallback на `gateway.session_cold_sync.stale_tolerance_seconds`).
- [ ] 2.2 Добавить `session_recovery: SessionRecoverySettings | None`
      в `GatewaySettings`.
- [ ] 2.3 Обновить `project.json` (только при инициализации —
      секция с дефолтами не обязательна, pydantic имеет дефолты).

## Этап 3: Реализация SessionRecoveryService
- [ ] 3.1 Все PG-операции через `utils.db.transaction()` — НЕ через
      `pg_pool.getconn()` / `putconn()` / `psycopg2.connect()`
      (см. design D-Pool в `storage-hybridization`).
- [ ] 3.2 `_read_session_from_pg` через `utils.db.transaction()` с
      проверкой размера (100 MB) и обязательным `rollback()` на
      исключении (контракт `utils.db.transaction()`).
- [ ] 3.3 `atomic_backup` и `_rollback_backup` с обработкой
      `errno.EXDEV` (cross-device fallback через `shutil.copy2` +
      `unlink`).
- [ ] 3.4 Хэширование имён файлов бэкапов (`hashlib.sha256`,
      первые 16 hex-символов) для ограничения длины имени файла.
- [ ] 3.5 Строгая проверка `_read_only` в `PGSessionManager.save()`:
      если `metadata['_read_only'] is True` — `RuntimeError` с
      понятным сообщением (включая требуемый режим escape'а).
- [ ] 3.6 Получение списка stale-сессий — **через тот же детектор**,
      что использует `SessionColdSyncService` для эмиссии
      `session_stale_detected` (single source of truth для условия
      stale), а не через собственный re-implementation. Это можно
      сделать через `SessionColdSyncService.get_stats()` + повторный
      `read_session_metadata` + `read_pg_meta`, либо через отдельный
      публичный метод `SessionColdSyncService.list_stale_keys(...)`.

## Этап 4: Интеграция и Rotation
- [ ] 4.1 Интегрировать `SessionRecoveryService` в
      `ApplicationContext.start()` (через `_make_session_recovery_service`)
      **ДО** инициализации `SessionColdSyncService` (шаг 4b
      в `application_context.py`).
- [ ] 4.2 Реализовать `BackupRotationService` с обработкой
      `FileNotFoundError` для multi-instance safety.
- [ ] 4.3 Метрики `stale_restored_total` / `stale_backup_failed_total`
      публикуются через `gateway.py`-композицию (см.
      `lib/services/runtime_health.py` L130–145 — расширения
      добавляются в gateway). **НЕ трогать `RuntimeHealth`** —
      расширение `RuntimeHealth` не входит в scope этого change
      (Non-Goal).
- [ ] 4.4 Lifecycle: `SessionRecoveryService.stop()` вызывается в
      `ShutdownCoordinator` ДО `SessionColdSyncService.stop()` —
      иначе sync может перезаписать только что восстановленную
      сессию.

## Этап 5: Валидация
- [ ] 5.1 Smoke (detect-only): запустить, убедиться, что stale-сессии
      логируются, но не мутируются.
- [ ] 5.2 Smoke (read-only): попытаться записать в stale-сессию,
      убедиться в получении `RuntimeError`.
- [ ] 5.3 Smoke (backup-and-restore): искусственно состарить JSONL,
      запустить recovery, убедиться, что JSONL обновлён, а бэкап
      создан.
- [ ] 5.4 `pytest tests/ -q` → 0 failed.
- [ ] 5.5 Обновить `docs/architecture/storage-layers.md`,
      `docs/TARGET_ARCHITECTURE.md` (если затрагивает архитектурные
      правила), `CHANGELOG.md`.