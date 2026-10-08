-- ============================================================================
-- public.agent_gateway_logs_test — test-клон public.agent_gateway_logs
-- Структурированный журнал событий агента под профилем test.
-- Колонка user_id включена сразу (см. tools-history-search: это security
-- boundary для history_search(session_scope="all")); индекс
-- agent_gateway_logs_test_user_id_timestamp_idx создан сразу при создании
-- таблицы — отдельной миграции не требуется (аналог V004 для prod).
-- Распределён по request_id. PK на id не объявлен (см. ниже).
-- Управляется: lib/services/db_logging_service.py.
-- Совместимость: Greenplum 6.5.
--
-- Этот файл живёт ТОЛЬКО для psql-ручного применения; штатное применение
-- всех шести test-таблиц — `python tools/apply_test_profile_tables.py`
-- (миграции схемы test-профиля в репозитории нет).
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS public.agent_gateway_logs_test (
    id           UUID NOT NULL DEFAULT gen_random_uuid(),
    "timestamp"  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    level        VARCHAR(16) NOT NULL,
    event_type   VARCHAR(64) NOT NULL,

    request_id   VARCHAR(256),
    session_id   VARCHAR(256),
    channel      VARCHAR(64),
    actor        VARCHAR(32),
    user_id      VARCHAR(256),
    name         VARCHAR(256),

    summary      TEXT,
    payload      JSONB,
    metadata     JSONB,

    CONSTRAINT valid_level CHECK (level IN ('DEBUG', 'INFO', 'WARN', 'ERROR'))
);

CREATE INDEX IF NOT EXISTS agent_gateway_logs_test_user_id_timestamp_idx
    ON public.agent_gateway_logs_test (user_id, "timestamp" DESC);

COMMENT ON TABLE  public.agent_gateway_logs_test IS 'Test-профиль: структурированный журнал событий агента. Структурный клон public.agent_gateway_logs; используется под профилем test. Связан с agent_question_runs_test по request_id.';
COMMENT ON COLUMN public.agent_gateway_logs_test.id          IS 'PK события (UUID, генерируется в приложении).';
COMMENT ON COLUMN public.agent_gateway_logs_test."timestamp" IS 'Время события.';
COMMENT ON COLUMN public.agent_gateway_logs_test.level       IS 'Уровень логирования: DEBUG/INFO/WARN/ERROR.';
COMMENT ON COLUMN public.agent_gateway_logs_test.event_type  IS 'Тип события (tool_call, agent_run, ...).';
COMMENT ON COLUMN public.agent_gateway_logs_test.request_id  IS 'FK-логически на agent_question_runs_test.request_id.';
COMMENT ON COLUMN public.agent_gateway_logs_test.session_id  IS 'Денормализованный channel:chat_id для удобства.';
COMMENT ON COLUMN public.agent_gateway_logs_test.channel     IS 'Канал (telegram/cli/etc).';
COMMENT ON COLUMN public.agent_gateway_logs_test.actor       IS 'Кто инициировал событие (user/agent/system).';
COMMENT ON COLUMN public.agent_gateway_logs_test.user_id     IS 'Test-профиль: идентификатор пользователя (security boundary для history_search(session_scope="all")). Включён в create-DDL (не отдельной миграцией), чтобы tool_history_search работал сразу после первого запуска под профилем test.';
COMMENT ON COLUMN public.agent_gateway_logs_test.name        IS 'Сущность события (категориальный ключ для фильтрации, никогда не NULL).';
COMMENT ON COLUMN public.agent_gateway_logs_test.summary     IS 'Человекочитаемый сниппет события.';
COMMENT ON COLUMN public.agent_gateway_logs_test.payload     IS 'JSONB: детальные данные события.';
COMMENT ON COLUMN public.agent_gateway_logs_test.metadata    IS 'JSONB: дополнительные метаданные.';
COMMENT ON INDEX  public.agent_gateway_logs_test_user_id_timestamp_idx IS 'Обслуживает access-pattern history_search(session_scope="all"): WHERE user_id = ? ORDER BY "timestamp" DESC.';

-- Идемпотентная синхронизация дефолта id с prod (prod имеет DEFAULT, CREATE
-- TABLE IF NOT EXISTS на уже существующей таблице default не выставит).
ALTER TABLE public.agent_gateway_logs_test
    ALTER COLUMN id SET DEFAULT gen_random_uuid();
