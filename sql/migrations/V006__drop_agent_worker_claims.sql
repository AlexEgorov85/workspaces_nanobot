-- ============================================================================
-- V006 — drop agent_worker_claims (change enterprise-mcp-platform, phase 1)
-- ============================================================================
-- Протокол аренды задач (lease/heartbeat/reclaim через таблицу
-- ``public.agent_worker_claims``) удалён из ``PostgresChannel``. Активный
-- режим всегда был ``claim_strategy="single"`` — захват задачи одним
-- ``UPDATE ... RETURNING`` без обращений к таблице аренды, состояние
-- захвата хранится в самой строке задачи (``status='processing'``).
-- Физически ноль INSERT/SELECT/UPDATE/DELETE к ``agent_worker_claims``
-- в hot-path single-режима — это подтверждал ``tests/test_single_mode_audit.py``.
--
-- Вместе с таблицей удалены настройки ``channels.postgres.claims_table``,
-- ``channels.postgres.claim_strategy`` и ``channels.postgres.lease_interval``,
-- а также ключ профиля ``channels.postgres.claims_table`` в
-- ``PROFILE_OWNED_RUNTIME_KEYS`` / ``EXPECTED_RUNTIME_TABLE_NAMES``.
--
-- ВНИМАНИЕ про SKIP LOCKED. План перехода предполагал заменить опрос на
-- ``SELECT ... FOR UPDATE SKIP LOCKED``. Это не сделано и не будет: проект
-- разворачивается на Greenplum 6.5 (ядро PostgreSQL 9.4 — см. sql/README.md),
-- где SKIP LOCKED (появился в PostgreSQL 9.5) недоступен, а Greenplum
-- при ``SELECT ... FOR UPDATE`` берёт блокировку уровня ТАБЛИЦЫ. Такой
-- захват заблокировал бы все читатели и писатели ``agent_conversation_messages``.
-- Корректности он и не нужен: внешний ``AND status = 'pending'`` в
-- ``_claim_one`` делает повторный захват невозможным и без SKIP LOCKED —
-- SKIP LOCKED даёт только снижение задержки при конкурентных захватах.
--
-- Миграция идемпотентна: IF EXISTS.
-- Номер 006, а не 005: в истории уже был `V005__create_agent_cache_ownership.sql`
-- (удалён вместе с `cache_ownership.py`). Базы, где он успел примениться,
-- хранят `005` в `public.schema_migrations` — переиспользовать номер нельзя.
-- Совместимость: Greenplum 6.5 / PostgreSQL 12+.
-- ============================================================================

DROP TABLE IF EXISTS public.agent_worker_claims;

-- Комментарий к записи в schema_migrations (если скрипт выполняется вне
-- tools/migrate.py — например, руками):
COMMENT ON SCHEMA public IS 'V006 dropped agent_worker_claims table';

-- Регистрация версии выполняется runner'ом tools/migrate.py
-- (INSERT в public.schema_migrations) — НЕ этим файлом.
