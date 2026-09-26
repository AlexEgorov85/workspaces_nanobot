# Tasks: session-recovery

## Этап 1: Подготовка
- [ ] 1.1 Убедиться, что change `storage-hybridization` полностью применён и работает.
- [ ] 1.2 Написать admin tool `tools/recover_stale_sessions.py` с флагом `--dry-run` для безопасного анализа.

## Этап 2: Реализация SessionRecoveryService
- [ ] 2.1 Реализовать `_get_lock_conn` с реконнектом (переиспользуя логику из `storage-hybridization`).
- [ ] 2.2 Реализовать `_read_session_from_pg` с проверкой размера (100 MB) и обязательным `conn.rollback()`.
- [ ] 2.3 Реализовать `atomic_backup` и `_rollback_backup` с обработкой `errno.EXDEV`.
- [ ] 2.4 Реализовать хэширование имён файлов бэкапов (`hashlib.sha256`).
- [ ] 2.5 Добавить строгую проверку `_read_only` в `PGSessionManager.save()`.

## Этап 3: Интеграция и Rotation
- [ ] 3.1 Интегрировать `SessionRecoveryService` в `ApplicationContext.start()` **ДО** инициализации `SessionColdSyncService`.
- [ ] 3.2 Реализовать `BackupRotationService` с обработкой `FileNotFoundError` для multi-instance safety.
- [ ] 3.3 Добавить метрики `stale_restored_total`, `stale_backup_failed_total` в `RuntimeHealth`.

## Этап 4: Валидация
- [ ] 4.1 Smoke (detect-only): запустить, убедиться, что stale-сессии логируются, но не мутируются.
- [ ] 4.2 Smoke (read-only): попытаться записать в stale-сессию, убедиться в получении `RuntimeError`.
- [ ] 4.3 Smoke (backup-and-restore): искусственно состарить JSONL, запустить recovery, убедиться, что JSONL обновлён, а бэкап создан.
- [ ] 4.4 `pytest tests/ -q` → 0 failed.