-- ============================================================================
-- public.agent_gateway_logs — структурированный журнал событий агента
-- PK на id ОБЪЯВЛЕН и объявлен всегда: сверка с живой базой показала, что
-- расхождение было именно в этом файле, а не в базе. Прежний отказ от PK
-- опирался на неверное основание — «объявлять PK без уникального индекса
-- нельзя», тогда как PRIMARY KEY создаёт такой индекс сам. Вдобавок
-- объявленный ниже DISTRIBUTED BY (id) только при наличии PK на id и
-- удовлетворяет правилу Greenplum «ключ распределения — подмножество ключа»:
-- без PK объявление перестаёт быть проверяемым, а уникальность держится
-- на слове (UUID генерируется в Python). Здесь уникальность не выдумывается,
-- а проверяется базой.
-- Управляется: lib/services/db_logging_service.py.
-- Совместимость: Greenplum 6.5 (ядро PostgreSQL 9.4) — боевая среда;
-- PostgreSQL 13.22 — тестовый контур. Файлы из sql/ применяются к обоим
-- движкам, поэтому клаузы, которых нет на одном из них, в теле CREATE TABLE
-- не пишутся: распределение объявляется ограждённым шагом ниже, а
-- идемпотентные конструкции 9.5/9.6 заменены на DO-блоки с проверкой
-- pg_indexes и information_schema.columns. Повторный запуск файла обязан
-- оставаться no-op, иначе psql -f перестал бы быть идемпотентным.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS public.agent_gateway_logs (
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

    -- Момент СОБЫТИЯ и ключ ПОРЯДКА. Nullable в этом файле: ограничение
    -- NOT NULL накладывает V008__agent_gateway_logs_event_time_columns.sql
    -- и только после backfill и очистки строк без ключа. Значения не
    -- приходят извне, их разбирает писатель из metadata
    -- (db_logging_service.event_time_columns / data.service.main).
    seq          BIGINT,
    occurred_at  TIMESTAMPTZ,

    PRIMARY KEY (id),

    CONSTRAINT valid_level CHECK (level IN ('DEBUG', 'INFO', 'WARN', 'ERROR'))
);

-- Ключ распределения объявлен отдельным шагом, а не в теле CREATE TABLE:
-- файлы из sql/ применяются к обоим движкам (тестовый контур — PostgreSQL
-- 13.22, боевая среда — Greenplum 6.5), а в теле клауза DISTRIBUTED упала бы
-- на PostgreSQL как синтаксическая ошибка.
--
-- Выбран id: он же и есть первичный ключ, поэтому ограничение «ключ
-- распределения — подмножество ключа» выполнено, а равномерная запись по
-- сегментам следует из равномерности UUID. Хеш по времени или по seq дал бы
-- обратный эффект: строки с текущим моментом времени ушли бы на один
-- сегмент, то есть запись журнала упёрлась бы в один сегмент. Безявная
-- клауза тоже не годится: Greenplum по умолчанию взял бы первый столбец
-- таблицы — здесь это id, но это его выбор, а не наш.
DO $distribution$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relname = 'pg_dist_partition'
          AND n.nspname = 'pg_catalog'
    ) THEN
        EXECUTE 'ALTER TABLE public.agent_gateway_logs
                 SET DISTRIBUTED BY (id)';
    END IF;
END
$distribution$;

DO $indexes$
BEGIN
    -- Индексы под момент события и ключ порядка создаются здесь для свежей
    -- установки; на существующей базе их добавляет
    -- V009__agent_gateway_logs_event_time_indexes.sql — строго после backfill и
    -- очистки V008, потому что до очистки NULL держит часть строк и индексы
    -- не окупаются.
    --
    -- Обслуживает access-pattern history_search(session_scope="all"):
    -- WHERE user_id = ? ORDER BY "timestamp" DESC.
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public'
          AND tablename  = 'agent_gateway_logs'
          AND indexname  = 'agent_gateway_logs_user_id_timestamp_idx'
    ) THEN
        CREATE INDEX agent_gateway_logs_user_id_timestamp_idx
            ON public.agent_gateway_logs (user_id, "timestamp" DESC);
    END IF;

    -- Канонический порядок чтения оборота: ORDER BY seq, id.
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public'
          AND tablename  = 'agent_gateway_logs'
          AND indexname  = 'idx_agent_logs_seq'
    ) THEN
        CREATE INDEX idx_agent_logs_seq
            ON public.agent_gateway_logs (seq);
    END IF;

    -- Окно времени СОБЫТИЯ; "timestamp" для этого не годится — он ставится
    -- базой при сбросе батча.
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public'
          AND tablename  = 'agent_gateway_logs'
          AND indexname  = 'idx_agent_logs_occurred_at'
    ) THEN
        CREATE INDEX idx_agent_logs_occurred_at
            ON public.agent_gateway_logs (occurred_at DESC);
    END IF;
END
$indexes$;

COMMENT ON TABLE  public.agent_gateway_logs IS 'Структурированный журнал событий агента. Связан с agent_question_runs по request_id.';
COMMENT ON COLUMN public.agent_gateway_logs.id          IS 'PK события (UUID, генерируется в приложении).';
COMMENT ON COLUMN public.agent_gateway_logs."timestamp" IS 'Момент ЗАПИСИ строки (ставит база при сбросе батча). Событийным временем является occurred_at.';
COMMENT ON COLUMN public.agent_gateway_logs.level       IS 'Уровень логирования: DEBUG/INFO/WARN/ERROR.';
COMMENT ON COLUMN public.agent_gateway_logs.event_type  IS 'Каноническое имя события из словаря platform.json → data.log_unknown_event_type_policy; при policy=strict имя вне словаря отказывает батчем.';
COMMENT ON COLUMN public.agent_gateway_logs.request_id  IS 'FK-логически на agent_question_runs.request_id.';
COMMENT ON COLUMN public.agent_gateway_logs.session_id  IS 'Денормализованный channel:chat_id для удобства.';
COMMENT ON COLUMN public.agent_gateway_logs.channel     IS 'Канал (telegram/cli/etc).';
COMMENT ON COLUMN public.agent_gateway_logs.actor       IS 'Кто инициировал событие (user/agent/system).';
COMMENT ON COLUMN public.agent_gateway_logs.user_id     IS 'Идентификатор пользователя (sender_id из RequestContext). Денормализован из agent_question_runs.user_id как security boundary для history_search(session_scope="all"). Заполняется DbLoggingService явно (от producer''а или через request_id matching в _enqueue) либо backfill-миграцией V004.';
COMMENT ON COLUMN public.agent_gateway_logs.name        IS 'Сущность события (категориальный ключ для фильтрации, никогда не NULL). Значения задаются писателем и следуют за именем события: agent.received — sender/user; agent.delivered — "assistant"; tool.started/tool.completed — имя tool; llm.exchanged — модель; agent.responded — "run"; agent.degraded — канал/сессия. Список дореформенных имён (tool_call, llm_call, outbound_final, inbound, run_finished, error) в боевой таблице отсутствует: переименование сведено со словарём, а строки без ключа порядка удалены миграцией V008.';
COMMENT ON COLUMN public.agent_gateway_logs.summary     IS 'Человекочитаемый сниппет события: обрезанный content (<=200), текст ошибки (для tool_result с status=error) или статус (finish_reason).';
COMMENT ON COLUMN public.agent_gateway_logs.payload     IS 'JSONB: детальные данные события.';
COMMENT ON COLUMN public.agent_gateway_logs.metadata    IS 'JSONB: дополнительные метаданные. Транспорт батча: seq/occurred_at едут здесь и разбираются в одноимённые колонки единственным табличным писателем; читать порядок и окно времени по тексту нельзя (текст не индексируется и не сортируется как хронология).';
COMMENT ON COLUMN public.agent_gateway_logs.seq         IS 'Ключ порядка строки журнала: момент события в наносекундах (time.time_ns()). Канонический порядок чтения оборота — ORDER BY seq, id. NULL означает «момент события неизвестно», а не «собылось позже всего»: такие строки читаются отдельным счётчиком unattributed. Ставит писатель, а не база. NOT NULL — после V008 (backfill + очистка).';
COMMENT ON COLUMN public.agent_gateway_logs.occurred_at IS 'Момент СОБЫТИЯ; timestamp остаётся моментом ЗАПИСИ строки (её ставит база при сбросе батча). Тот же мгновенный снимок, что и seq. NOT NULL — после V008 (backfill + очистка).';
COMMENT ON INDEX  public.agent_gateway_logs_user_id_timestamp_idx IS 'Обслуживает access-pattern history_search(session_scope="all"): WHERE user_id = ? ORDER BY "timestamp" DESC.';
COMMENT ON INDEX  public.idx_agent_logs_seq IS 'Канонический порядок чтения оборота: ORDER BY seq, id.';
COMMENT ON INDEX  public.idx_agent_logs_occurred_at IS 'Окно времени СОБЫТИЯ; "timestamp" для этого не годится — он ставится базой при сбросе батча.';
