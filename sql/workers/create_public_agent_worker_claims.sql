-- ============================================================================
-- public.agent_worker_claims — аренда задач воркерами (мульти-машинный пул)
-- Управляется: PostgresChannel (клейм, heartbeat, reclaim).
-- Совместимость: Greenplum 6.5.
--
-- Инвариант: задача обрабатывается воркером (status='processing')  ⇔
--            существует ровно одна claim-запись для task_id (с живым lease).
-- PK (task_id) — жёсткая гарантия эксклюзивности: два INSERT'а с одним
-- task_id невозможны (второй падает на unique-индексе), поэтому одна задача
-- физически не может быть захвачена двумя воркерами.
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.agent_worker_claims (
    task_id     UUID NOT NULL PRIMARY KEY,   -- = agent_conversation_messages.id
    worker_id   TEXT NOT NULL,               -- идентификатор воркера ({host}:{pid}:{rand})
    claimed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_until TIMESTAMPTZ NOT NULL,        -- продлевается heartbeat'ом воркера
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
DISTRIBUTED BY (task_id);

COMMENT ON TABLE  public.agent_worker_claims IS 'Арена задач воркерами PostgresChannel. PK(task_id) гарантирует, что одна задача обрабатывается ровно одним воркером; lease_until продлевается heartbeat воркера и по истечении возвращает задачу в пул.';
COMMENT ON COLUMN public.agent_worker_claims.task_id     IS 'PK — ID задачи (= messages.id). Два INSERT с одним task_id невозможны.';
COMMENT ON COLUMN public.agent_worker_claims.worker_id   IS 'Идентификатор воркера, держащего аренду ({hostname}:{pid}:{rand8}).';
COMMENT ON COLUMN public.agent_worker_claims.claimed_at  IS 'Момент захвата аренды.';
COMMENT ON COLUMN public.agent_worker_claims.lease_until IS 'Срок жизни аренды; продлевается heartbeat воркера; после истечения задача возвращается в пул (reclaim).';
COMMENT ON COLUMN public.agent_worker_claims.created_at  IS 'Время создания записи аренды.';
