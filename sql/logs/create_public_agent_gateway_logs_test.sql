-- ============================================================================
-- public.agent_gateway_logs_test — test-клон public.agent_gateway_logs
-- Структурированный журнал событий агента под профилем test.
-- Колонка user_id включена сразу (см. tools-history-search: это security
-- boundary для history_search(session_scope="all")); индекс
-- agent_gateway_logs_test_user_id_timestamp_idx создан сразу при создании
-- таблицы — отдельной миграции не требуется (аналог V004 для prod).
-- Момент события и ключ порядка (seq, occurred_at) — тоже сразу, см. ALTER
-- ниже: тестовый профиль повторяет форму prod-таблицы, иначе замеры и
-- проверки чтения оборота проверяли бы не тот состав колонок.
-- Управляется: lib/services/db_logging_service.py.
-- Совместимость: Greenplum 6.5 (ядро PostgreSQL 9.4) — боевая среда;
-- PostgreSQL 13.22 — тестовый контур. Файлы из sql/ применяются к обоим
-- движкам, поэтому клаузы, которых нет на одном из них, в теле CREATE TABLE
-- не пишутся (см. ограждённый шаг распределения ниже), а идемпотентные
-- конструкции 9.5/9.6 заменены на DO-блоки с проверкой pg_indexes и
-- information_schema.columns. Повторный запуск файла обязан оставаться
-- no-op.
--
-- Этот файл живёт ТОЛЬКО для psql-ручного применения; для версионированного
-- применения через runner — V005__test_profile_tables.sql.
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

    PRIMARY KEY (id),

    CONSTRAINT valid_level CHECK (level IN ('DEBUG', 'INFO', 'WARN', 'ERROR'))
);

-- Ключ распределения — id, как в боевом файле: он же и есть первичный
-- ключ, поэтому ограничение «ключ распределения — подмножество ключа»
-- выполнено, а хеш по времени упирал бы запись журнала в один сегмент.
-- Клауза объявлена
-- ограждённым шагом, а не в теле CREATE TABLE, потому что файлы из sql/
-- применяются и к PostgreSQL 13.22, где её нет в синтаксисе.
DO $distribution$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relname = 'pg_dist_partition'
          AND n.nspname = 'pg_catalog'
    ) THEN
        EXECUTE 'ALTER TABLE public.agent_gateway_logs_test
                 SET DISTRIBUTED BY (id)';
    END IF;
END
$distribution$;

-- ---------------------------------------------------------------------------
-- Момент события и ключ порядка: тестовый клон повторяет prod-миграцию
-- V008__agent_gateway_logs_event_time_columns.sql.
--
-- Колонки ЗДЕСЬ ОСТАЮТСЯ NULLABLE намеренно. Ограничение NOT NULL в prod
-- убирает представимость дефекта, но не отменяет читательскую обязанность:
-- контракт двухчастного чтения оборота и счётчик unattributed обязаны
-- проверяться негативными тестами там, где NULL представим, — то есть здесь.
-- Наложить NOT NULL в тестовом профиле значит delete-тестами, которыми
-- держится требование «Отсутствие ключа порядка определено и не молчит».
--
-- Порядок шагов в prod (nullable → backfill → очистка → ограничение
-- непустоты) здесь не воспроизводится целиком: очистка УДАЛЯЕТ строки, а
-- тестовый профиль для этого не предназначен, и объём удаления задаёт
-- замер, а не файл DDL. Порядок проверяется отдельным тестом на самом файле
-- миграции.
--
-- ADD COLUMN IF NOT EXISTS (9.6) на 9.4 недоступен, поэтому каждая колонка
-- добавляется через проверку information_schema.columns. Проверка нужна и
-- ради идемпотентности: файл применяют повторно, и без неё второй прогос
-- упал бы на «column already exists».
-- ---------------------------------------------------------------------------
DO $columns$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name   = 'agent_gateway_logs_test'
          AND column_name  = 'seq'
    ) THEN
        ALTER TABLE public.agent_gateway_logs_test ADD COLUMN seq BIGINT;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name   = 'agent_gateway_logs_test'
          AND column_name  = 'occurred_at'
    ) THEN
        ALTER TABLE public.agent_gateway_logs_test ADD COLUMN occurred_at TIMESTAMPTZ;
    END IF;
END
$columns$;

DO $indexes$
BEGIN
    -- Аналог V004 для prod: индекс на user_id создаётся сразу при создании
    -- таблицы, отдельной миграции не требуется.
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public'
          AND tablename  = 'agent_gateway_logs_test'
          AND indexname  = 'agent_gateway_logs_test_user_id_timestamp_idx'
    ) THEN
        CREATE INDEX agent_gateway_logs_test_user_id_timestamp_idx
            ON public.agent_gateway_logs_test (user_id, "timestamp" DESC);
    END IF;

    -- Парные боевым индексам под момент события и ключ порядка (см. V009).
    -- Колонки здесь nullable намеренно, но индексы нужны те же: иначе
    -- тестовый профиль проверял бы чтение по seq на плане сортировки, а
    -- боевой — по индексу, и расхождение всплыло бы только в бою.
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public'
          AND tablename  = 'agent_gateway_logs_test'
          AND indexname  = 'idx_agent_gateway_logs_test_seq'
    ) THEN
        CREATE INDEX idx_agent_gateway_logs_test_seq
            ON public.agent_gateway_logs_test (seq);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public'
          AND tablename  = 'agent_gateway_logs_test'
          AND indexname  = 'idx_agent_gateway_logs_test_occurred_at'
    ) THEN
        CREATE INDEX idx_agent_gateway_logs_test_occurred_at
            ON public.agent_gateway_logs_test (occurred_at DESC);
    END IF;
END
$indexes$;

COMMENT ON TABLE  public.agent_gateway_logs_test IS 'Test-профиль: структурированный журнал событий агента. Структурный клон public.agent_gateway_logs; используется под профилем test. Связан с agent_question_runs_test по request_id.';
COMMENT ON COLUMN public.agent_gateway_logs_test.id          IS 'PK события (UUID, генерируется в приложении).';
COMMENT ON COLUMN public.agent_gateway_logs_test."timestamp" IS 'Момент ЗАПИСИ строки (ставит база при сбросе батча). Событийным временем является occurred_at.';
COMMENT ON COLUMN public.agent_gateway_logs_test.level       IS 'Уровень логирования: DEBUG/INFO/WARN/ERROR.';
COMMENT ON COLUMN public.agent_gateway_logs_test.event_type  IS 'Каноническое имя события из словаря платформы; при policy=strict имя вне словаря отказывает батчем.';
COMMENT ON COLUMN public.agent_gateway_logs_test.request_id  IS 'ИДЕНТИФИКАТОР ВОПРОСА (якорь оборота) либо пусто — см. change 2026-10-04-queue-as-anchor-identity Ф1. Контракт идентичен боевой таблице: id строки role=''user'' очереди взятого оборота либо объявленное второе пространство subagent:<task_id>; пусто означает «повода не было», выдуманный идентификатор и sentinel пробы сюда не пишутся. Отдельной колонки-якоря нет намеренно, признак-маркер лежит в actor/event_type/channel/metadata.';
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

COMMENT ON COLUMN public.agent_gateway_logs_test.seq IS
    'Test-профиль: ключ порядка строки журнала (момент события в наносекундах). Nullable намеренно — см. комментарий выше: контракт чтения без ключа проверяется здесь. Канонический порядок — ORDER BY seq, id.';

COMMENT ON COLUMN public.agent_gateway_logs_test.occurred_at IS
    'Test-профиль: момент СОБЫТИЯ (timestamp остаётся моментом записи строки). Nullable намеренно, парно с seq.';
