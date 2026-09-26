# Proposal: storage-hybridization

## Why
Upstream nanobot-ai 0.3.5 использует JSONL-файлы для хранения сессий (`SessionManager`) и SQLite для метрик LLM (`LLMUsageStore`). Текущая реализация проекта дублирует эту логику через кастомный `PGSessionManager`, что создаёт технический долг и ломается при обновлениях upstream.

## What
Переход к гибридной модели:
1. **Hot path**: upstream `SessionManager` (JSONL) и `LLMUsageStore` (SQLite).
2. **Cold mirror**: фоновая синхронизация метаданных и сообщений из JSONL в PostgreSQL (`SessionColdSyncService`).
3. **Защита от потери данных**: детект stale-сессий (когда PG свежее JSONL) с **блокировкой sync** для таких сессий, чтобы не перезаписать актуальные данные в PG устаревшими.

## In scope
- Интеграция upstream `SessionManager` и `LLMUsageStore`.
- `SessionColdSyncService` (sync-модель, фоновый поток, advisory locks).
- Детект stale-сессий (D23) и детект отставания sync (reverse lag).
- Контрактные тесты на upstream API.

## Out of scope
- Автоматическое восстановление stale-сессий из PG (отдельный change `session-recovery`).
- Восстановление отсутствующих (missing) JSONL-файлов (отдельный change `session-restore-from-pg`).
- Изменение схемы PostgreSQL (DDL).
- Изменение `DbLoggingService` (зафиксирован в спеке `logging-db`).

## Success criteria
1. `SessionManager` и `LLMUsageStore` работают в hot path.
2. `SessionColdSyncService` синхронизирует данные, не блокируя event loop.
3. Stale-сессии детектируются, и sync для них **пропускается** (защита PG).
4. `pytest tests/contract/ -q` → 0 failed.
5. `pytest tests/ -q --ignore=tests/integration` → 0 failed.