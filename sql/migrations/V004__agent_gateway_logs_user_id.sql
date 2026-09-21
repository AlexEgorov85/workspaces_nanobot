-- ============================================================================
-- V004 — agent_gateway_logs.user_id (change fix-history-search-user-isolation)
-- ============================================================================
-- Закрывает cross-user leakage в history_search(session_scope="all"):
-- фильтрация раньше шла по session_id с ослаблением через ``(%s OR
-- session_id = %s)``, что возвращало глобальный набор событий. Теперь
-- фильтрация идёт по user_id (security boundary), и эта колонка должна
-- существовать в agent_gateway_logs.
--
-- Колонка user_id — намеренная денормализация из agent_question_runs
-- (там user_id — первичный source of truth). Синхронность поддерживается
-- (а) single-writer invariant: DbLoggingService — единственный writer
-- gateway_logs, и (б) идемпотентной миграцией V004 с backfill.
--
-- Миграция идемпотентна: ADD COLUMN IF NOT EXISTS + UPDATE с предикатом
-- r.user_id IS NOT NULL + CREATE INDEX IF NOT EXISTS + COMMENT ON.
-- Совместимость: Greenplum 6.5 / PostgreSQL 12+.
-- ============================================================================

ALTER TABLE public.agent_gateway_logs
    ADD COLUMN IF NOT EXISTS user_id VARCHAR(256);

COMMENT ON COLUMN public.agent_gateway_logs.user_id IS
    'Идентификатор пользователя (sender_id из RequestContext). '
    'Денормализован из agent_question_runs.user_id как security boundary '
    'для history_search(session_scope="all"). Заполняется DbLoggingService '
    'явно или через request_id matching в _enqueue; исторические строки — '
    'через backfill UPDATE ниже.';

-- Backfill: переносим осмысленные identity из question_runs в gateway_logs.
-- Предикат ``r.user_id IS NOT NULL`` фиксирует, что NULL-пользователь не
-- «протекает» в gateway_logs (такие строки исключаются из scope="all").
-- ``l.user_id IS NULL`` — не перезаписываем уже заполненные строки.
UPDATE public.agent_gateway_logs l
SET user_id = r.user_id
FROM public.agent_question_runs r
WHERE l.request_id = r.request_id
  AND l.user_id IS NULL
  AND r.user_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS agent_gateway_logs_user_id_timestamp_idx
    ON public.agent_gateway_logs (user_id, "timestamp" DESC);

COMMENT ON INDEX public.agent_gateway_logs_user_id_timestamp_idx IS
    'Обслуживает access-pattern history_search(session_scope="all"): '
    'WHERE user_id = ? ORDER BY "timestamp" DESC.';

-- Регистрация версии выполняется runner'ом tools/migrate.py
-- (INSERT в public.schema_migrations) — НЕ этим файлом.
-- При ручном выполнении SQL нужно отдельно вставить запись
-- в schema_migrations (или воспользоваться ``--baseline``).
