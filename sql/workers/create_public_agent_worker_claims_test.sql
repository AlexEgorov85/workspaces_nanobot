-- ============================================================================
-- public.agent_worker_claims_test — test-клон public.agent_worker_claims
-- Аренда задач воркерами (мульти-машинный пул) под профилем test.
-- Управляется: PostgresChannel (клейм, heartbeat, reclaim).
-- Совместимость: Greenplum 6.5.
--
-- Инвариант: задача обрабатывается воркером (status='processing')  ⇔
--            существует ровно одна claim-запись для task_id (с живым lease).
-- PK (task_id) — жёсткая гарантия эксклюзивности.
--
-- Этот файл живёт ТОЛЬКО для psql-ручного применения; штатное применение
-- всех шести test-таблиц — `python tools/apply_test_profile_tables.py`
-- (миграции схемы test-профиля в репозитории нет).
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.agent_worker_claims_test (
    task_id     UUID NOT NULL PRIMARY KEY,
    worker_id   TEXT NOT NULL,
    claimed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_until TIMESTAMPTZ NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE  public.agent_worker_claims_test IS 'Test-профиль: аренда задач воркерами PostgresChannel. PK(task_id) гарантирует, что одна задача обрабатывается ровно одним воркером.';
COMMENT ON COLUMN public.agent_worker_claims_test.task_id     IS 'PK — ID задачи (= agent_conversation_messages_test.id).';
COMMENT ON COLUMN public.agent_worker_claims_test.worker_id   IS 'Идентификатор воркера, держащего аренду.';
COMMENT ON COLUMN public.agent_worker_claims_test.claimed_at  IS 'Момент захвата аренды.';
COMMENT ON COLUMN public.agent_worker_claims_test.lease_until IS 'Срок жизни аренды; продлевается heartbeat воркера; после истечения задача возвращается в пул (reclaim).';
COMMENT ON COLUMN public.agent_worker_claims_test.created_at  IS 'Время создания записи аренды.';
