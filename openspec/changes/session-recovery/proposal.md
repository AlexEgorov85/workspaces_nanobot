# Proposal: session-recovery

## Why
Сценарий «JSONL старее PG» реален (восстановление из бэкапа тома, переезд на новый хост, multi-instance misconfiguration). Change `storage-hybridization` детектирует это состояние и блокирует sync, но **не восстанавливает** данные. Нужен отдельный механизм безопасного восстановления.

## What
`SessionRecoveryService` с тремя режимами:
1. `detect-only` (default) — только лог + метрика (уже реализовано в `storage-hybridization`).
2. `read-only-fallback` — загрузка из PG с жёстким запретом на запись.
3. `backup-and-restore` — атомарный бэкап JSONL → перезапись из PG → нормальная работа.

## In scope
- `SessionRecoveryService` с тремя режимами.
- Атомарный backup с fallback на cross-device (`EXDEV`).
- Строгий запрет записи в режиме `read-only-fallback` (без сложных wrapper'ов).
- Ограничение размера сессии (max 100 MB) при восстановлении.
- Admin tool: `tools/recover_stale_sessions.py --dry-run`.

## Out of scope
- Восстановление **отсутствующих** (missing) JSONL-файлов (отдельный change `session-restore-from-pg`).
- Merge сообщений из JSONL и PG.
- Динамическое переключение режима recovery без перезапуска gateway.