# Proposal: session-recovery

## Why
Сценарий «JSONL старее PG» реален (восстановление из бэкапа тома, переезд на новый хост, multi-instance misconfiguration). Change `storage-hybridization` детектирует это состояние и блокирует sync, но **не восстанавливает** данные. Нужен отдельный механизм безопасного восстановления.

## What
`SessionRecoveryService` с тремя режимами:
1. `detect-only` (default) — только лог + метрика (полагается на событие `session_stale_detected` из `SessionColdSyncService`, см. `openspec/changes/storage-hybridization/design.md` D23).
2. `read-only-fallback` — загрузка из PG с жёстким запретом на запись.
3. `backup-and-restore` — атомарный бэкап JSONL → перезапись из PG → нормальная работа.

## Жёсткая зависимость от change `storage-hybridization`

**Этап A change `storage-hybridization`** (stale-detection + reverse-lag detection в `SessionColdSyncService`, метрики, события `session_stale_detected` / `sync_lag_exceeded`) — **обязательная предпосылка** для этого change.

- Без завершённой Части A режим `detect-only` — полный no-op (нет источника событий stale-detected).
- Rollout plan «включить `detect-only`, посмотреть логи, потом переключить на `backup-and-restore`» **не работает**, пока Часть A не активна и не проверена в проде.
- До развёртывания `session-recovery` в любом режиме Часть A должна быть активна и проверена в проде минимум N дней (конкретный срок — на решении оператора, не входит в эту спеку).

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