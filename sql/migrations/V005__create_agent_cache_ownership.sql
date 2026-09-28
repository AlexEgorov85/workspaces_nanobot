-- ============================================================================
-- V005 — agent_cache_ownership (change unify-cli-gateway-architecture)
-- ============================================================================
-- Единый PG-level claim для логического cache resource (resource_key =
-- 'local_cache'). Один владелец (READ_WRITE), остальные процессы — READ_ONLY.
-- Каждый takeover инкрементирует generation (fencing token), чтобы старый
-- producer мог обнаружить потерю ownership и прекратить мутации.
--
-- Owner-worker_id: монотонно идентифицирует владельца одного поколения.
-- Generation: инкрементируется на 1 при каждом takeover, начиная с 1.
--   НЕ является самостоятельным write barrier (см. advisory lock в
--   CacheOwnershipCoordinator.acquire_write_fence); generation check
--   обязательно выполняется внутри транзакции с pg_advisory_xact_lock
--   для mutual exclusion.
--
-- expires_at: следующий момент, до которого claim считается живым без
--   heartbeat. TTL = 60 сек (DEFAULT, может быть переопределён через
--   CacheOwnershipCoordinator(ttl_seconds=...)). Heartbeat
--   UPDATE SET expires_at = NOW() + INTERVAL '60 seconds'.
--
-- Миграция идемпотентна: CREATE TABLE IF NOT EXISTS + COMMENT ON.
-- Совместимость: Greenplum 6.5 / PostgreSQL 12+.
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.agent_cache_ownership (
    resource_key        VARCHAR(64) PRIMARY KEY,
    owner_id            VARCHAR(256) NOT NULL,
    generation          BIGINT NOT NULL DEFAULT 1,
    acquired_at         TIMESTAMP NOT NULL DEFAULT NOW(),
    last_heartbeat_at   TIMESTAMP NOT NULL DEFAULT NOW(),
    expires_at          TIMESTAMP NOT NULL
);

COMMENT ON TABLE public.agent_cache_ownership IS
    'PG-level ownership claim для логического cache resource '
    '(resource_key=''local_cache''). Atomic claim через INSERT ... ON CONFLICT '
    'в CacheOwnershipCoordinator.try_claim(); fencing через generation check '
    'внутри pg_advisory_xact_lock(hashtext(resource_key)).';

COMMENT ON COLUMN public.agent_cache_ownership.resource_key IS
    'Идентификатор логического cache resource. Фиксированное значение '
    '''local_cache''. НЕ per-process, НЕ per-role, НЕ per-storage.';

COMMENT ON COLUMN public.agent_cache_ownership.owner_id IS
    'worker_id текущего владельца ("{role}_{pid}" по соглашению).';

COMMENT ON COLUMN public.agent_cache_ownership.generation IS
    'Fencing token. Инкрементируется на 1 при каждом takeover. '
    'Стартует с 1 при первой вставке. Strictly monotonic.';

COMMENT ON COLUMN public.agent_cache_ownership.expires_at IS
    'Момент, до которого claim валиден без heartbeat. '
    'При expires_at < NOW() следующий try_claim() может перехватить ownership.';

-- Регистрация версии выполняется runner'ом tools/migrate.py
-- (INSERT в public.schema_migrations) — НЕ этим файлом.
-- При ручном выполнении SQL нужно отдельно вставить запись
-- в schema_migrations (или воспользоваться ``--baseline``).
