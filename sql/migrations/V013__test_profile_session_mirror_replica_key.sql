-- ============================================================================
-- V013 — test-профиль: зеркало сессий в боевой форме (change 2026-10-04-db-schema-audit)
-- ============================================================================
--
-- agent_session_meta_test и agent_session_messages_test в существующих базах
-- остались в дорепличной форме: без replica_id, source_digest, missing_cycles,
-- message_count, synced_at и с ключом либо по session_key, либо по суррогатному
-- id. Их собственный DDL (sql/session/create_public_agent_session_*_test.sql)
-- объявляет боевую форму, поэтому расхождение — не «другая схема», а недовездевший
-- DDL: свежая база из репозитория и эта база разошлись.
--
-- Почему нельзя было просто удалить таблицы. profiles/test.jsonc указывает агенту
-- именно на них (channels.postgres.messages_table/meta_table), а стартовая
-- проверка схемы (ApplicationContext._validate_runtime_schema) требует наличия
-- всех пяти имён. Удаление сделало бы профиль test нестартующим, то есть
-- «мёртвой» оказалась бы не таблица, а проверка.
--
-- Шаги идут в порядке боевой V010 и по той же причине (обратный порядок ломает
-- существующие строки базой): добавить колонки, пометить прежние строки явной
-- меткой, сменить ключ. Отличие одно: replica_id объявляется непустой сразу в
-- шаге добавления, а не ужесточением отдельной командой — на Greenplum 6.5
-- (ядро 9.4) такое ужесточение проверяет таблицу целиком под ACCESS EXCLUSIVE,
-- и страж tests/test_runtime_environment_contract.py такие файлы не пропускает.
-- Подробности — в шаге 3.
--
-- replica_id НЕ выдумывается для существующих строк: какая реплика их писала —
-- неизвестно, и придуманное значение неотличимо от настоящего. Всем прежним
-- строкам проставляется явный маркер 'legacy', который новая реплика не пишет.
-- Поэтому новый писатель таких строк не увидит и не перепишет: ровно то же
-- поведение, что задано боевой V010, и по той же причине — строки без
-- происхождения нельзя ни приписывать реплике, ни переписывать её именем.
--
-- Ничего не удаляется. Строки 'legacy' остаются лежать, как и в боевой базе;
-- их разбор — отдельное решение оператора (в V010 это шаг 5 за флагом
-- nanobot.cleanup_legacy_session_mirror, в тестовом контуре разбор не нужен:
-- значения всё равно восстанавливаются из JSONL).
--
-- Совместимость: Greenplum 6.5 (ядро PostgreSQL 9.4) и PostgreSQL 13+.
-- ADD COLUMN IF NOT EXISTS (9.6) и ADD CONSTRAINT IF NOT EXISTS на 9.4
-- недоступны, поэтому каждая операция идёт через проверку каталога.
-- Идемпотентна: повторное применение — no-op.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- Шаг 1 — колонки.
--
-- replica_id объявляется НЕПУСТОЙ сразу, вместе с меткой прежнего писателя в
-- DEFAULT: пустой колонки в боевой форме нет, а ужесточение отдельной командой
-- на Greenplum 6.5 проверяет таблицу целиком под ACCESS EXCLUSIVE (см. шаг 3).
-- Остальные колонки — обычные nullable: они не входят в ключ и писатель может
-- их не заполнять.
-- ---------------------------------------------------------------------------
DO $columns$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'agent_session_meta_test'
          AND column_name = 'replica_id'
    ) THEN
        ALTER TABLE public.agent_session_meta_test
            ADD COLUMN replica_id TEXT NOT NULL DEFAULT 'legacy';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'agent_session_meta_test'
          AND column_name = 'source_digest'
    ) THEN
        ALTER TABLE public.agent_session_meta_test ADD COLUMN source_digest TEXT;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'agent_session_meta_test'
          AND column_name = 'missing_cycles'
    ) THEN
        ALTER TABLE public.agent_session_meta_test
            ADD COLUMN missing_cycles INT NOT NULL DEFAULT 0;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'agent_session_meta_test'
          AND column_name = 'message_count'
    ) THEN
        ALTER TABLE public.agent_session_meta_test ADD COLUMN message_count INT;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'agent_session_meta_test'
          AND column_name = 'synced_at'
    ) THEN
        ALTER TABLE public.agent_session_meta_test ADD COLUMN synced_at TIMESTAMPTZ;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'agent_session_messages_test'
          AND column_name = 'replica_id'
    ) THEN
        ALTER TABLE public.agent_session_messages_test
            ADD COLUMN replica_id TEXT NOT NULL DEFAULT 'legacy';
    END IF;
END
$columns$;

-- ---------------------------------------------------------------------------
-- Шаг 2 — backfill для случая, когда колонка уже существовала.
--
-- На базе, где колонки не было, метку проставляет DEFAULT из шага 1, и этот
-- UPDATE не находит ни строки. Он нужен для повторного применения на базе с
-- колонкой, добавленной вручную: там DEFAULT не участвовал, и строки без
-- реплики остались бы NULL — а шаг 3 такой случай отвергает с требованием
-- решить это оператору. Backfill меткой ничего не выдумывает (см. шапку файла).
-- ---------------------------------------------------------------------------
UPDATE public.agent_session_meta_test
   SET replica_id = 'legacy'
 WHERE replica_id IS NULL;

UPDATE public.agent_session_messages_test
   SET replica_id = 'legacy'
 WHERE replica_id IS NULL;

-- ---------------------------------------------------------------------------
-- Шаг 3 — проверка непустоты колонки.
--
-- Шаг 1 объявляет replica_id сразу непустой, поэтому ужесточать её отдельной
-- командой не нужно. И это не только короче: ужесточение непустоты отдельной
-- командой на существующей колонке проверяет таблицу целиком под блокировкой
-- ACCESS EXCLUSIVE — на PostgreSQL 12 этот обход убрали, а на Greenplum 6.5
-- (ядро 9.4) он остаётся, и страж tests/test_runtime_environment_contract.py
-- такой файл не пропускает. Здесь объявление непустоты встроено в добавление
-- колонки, и стоимость сравнима с перестроением, которого всё равно требует
-- смена ключа ниже.
--
-- Если колонка уже существовала и осталась пустой, файл НЕ пытается её
-- ужесточить сам: молча смириться с nullable replica_id нельзя (забытый
-- писателем реплика тогда не отвергся бы, а записался бы в 'legacy'), и
-- нельзя делать это в обход правила движка. Сообщение называет таблицу и
-- требуемое действие.
-- ---------------------------------------------------------------------------
DO $verify_notnull$
DECLARE
    nullable_meta    text;
    nullable_message text;
BEGIN
    SELECT is_nullable INTO nullable_meta
      FROM information_schema.columns
     WHERE table_schema = 'public' AND table_name = 'agent_session_meta_test'
       AND column_name = 'replica_id';
    IF nullable_meta = 'YES' THEN
        RAISE EXCEPTION
            'agent_session_meta_test.replica_id остаётся пустой: колонка существовала '
            'до применения миграции, и ужесточить её без перестроения таблицы файл не '
            'умеет (движок Greenplum 6.5). Действие оператора: объявить колонку '
            'непустой вручную и повторить миграцию';
    END IF;

    SELECT is_nullable INTO nullable_message
      FROM information_schema.columns
     WHERE table_schema = 'public' AND table_name = 'agent_session_messages_test'
       AND column_name = 'replica_id';
    IF nullable_message = 'YES' THEN
        RAISE EXCEPTION
            'agent_session_messages_test.replica_id остаётся пустой: колонка существовала '
            'до применения миграции, и ужесточить её без перестроения таблицы файл не '
            'умеет (движок Greenplum 6.5). Действие оператора: объявить колонку '
            'непустой вручную и повторить миграцию';
    END IF;
END
$verify_notnull$;

-- Дефолт 'legacy' снимается сразу после добавления колонки: он нужен был
-- только чтобы объявить её непустой в одном выражении. Оставленный дефолт
-- означал бы, что забытая вставка без replica_id тихо получила бы метку
-- 'legacy' вместо отказа, а смысл всей миграции — в том, чтобы забытый
-- реплика отвергался, а не подставлялся.
ALTER TABLE public.agent_session_meta_test
    ALTER COLUMN replica_id DROP DEFAULT;

ALTER TABLE public.agent_session_messages_test
    ALTER COLUMN replica_id DROP DEFAULT;

-- ---------------------------------------------------------------------------
-- Шаг 4а — ключ метаданных: (session_key) → (replica_id, session_key).
-- Имя снимаемого ключа берём из каталога, а не по шаблону имени: шаблонное
-- DROP CONSTRAINT IF EXISTS был бы no-op при другом имени, и следующий же
-- ADD CONSTRAINT упал бы с «multiple primary keys» — ошибка, не называющая
-- настоящую причину.
-- ---------------------------------------------------------------------------
DO $meta_key$
DECLARE
    pk_name          text;
    duplicate_sessions bigint;
BEGIN
    -- Ключ (replica_id, session_key) при единственной метке 'legacy' сводится к
    -- session_key, поэтому уникальность проверяется именно по нему. Проверка
    -- до DROP: иначе таблица осталась бы без ключа при откате транзакции.
    SELECT count(*) INTO duplicate_sessions FROM (
        SELECT session_key
          FROM public.agent_session_meta_test
         GROUP BY session_key
        HAVING count(*) > 1
    ) AS duplicated_sessions;
    IF duplicate_sessions > 0 THEN
        RAISE EXCEPTION
            'в agent_session_meta_test % значений session_key встречаются более одного раза — '
            'составной ключ не навешивается. Разберитесь ДО миграции: для зеркала безопасно '
            'удалить строки этих сессий и дождаться следующего цикла синхронизации, который '
            'запишет их заново из JSONL', duplicate_sessions;
    END IF;

    SELECT conname INTO pk_name
      FROM pg_constraint
     WHERE conrelid = 'public.agent_session_meta_test'::regclass
       AND contype  = 'p';

    IF pk_name IS NOT NULL THEN
        EXECUTE format(
            'ALTER TABLE public.agent_session_meta_test DROP CONSTRAINT %I', pk_name);
    END IF;

    ALTER TABLE public.agent_session_meta_test
        ADD CONSTRAINT agent_session_meta_test_pkey
        PRIMARY KEY (replica_id, session_key);
END
$meta_key$;

COMMENT ON COLUMN public.agent_session_meta_test.replica_id IS
    'Реплика-владелец строки; часть первичного ключа. ''legacy'' — строки, '
    'записанные прежним писателем, до появления реплик: какой инстанс их '
    'писал, неизвестно, и подставлять догадку нельзя. Новый писатель такие '
    'строки не увидит (это не его реплика) и не перепишет.';

COMMENT ON COLUMN public.agent_session_meta_test.source_digest IS
    'SHA-256 файла сессии (JSONL) на момент зеркалирования. ПРИЗНАК ИЗМЕНЕНИЯ, '
    'а не updated_at: upstream не поднимает updated_at при изменении metadata '
    'сессии, поэтому по updated_at такие правки не видны никогда.';

COMMENT ON COLUMN public.agent_session_meta_test.missing_cycles IS
    'Сколько циклов подряд сессия была в зеркале, но отсутствовала в списке '
    'upstream. Удаление — по достижении порога, а не по первому пропуску.';

COMMENT ON COLUMN public.agent_session_meta_test.message_count IS
    'Сколько строк сообщений зеркала принадлежит этой сессии. Расхождение с '
    'фактическим числом строк означает разорванную запись и требует принудительной '
    'синхронизации.';

COMMENT ON COLUMN public.agent_session_meta_test.synced_at IS
    'Момент последней записи строки зеркала. Отделяет «файл давно не менялся» '
    'от «зеркалом перестали писать»: по одному updated_at эти случаи неразличимы.';

-- ---------------------------------------------------------------------------
-- Шаг 4б — ключ сообщений: (id) → (replica_id, session_key, seq).
-- Суррогатный id на месте остаётся: он не часть ключа (пояснение — в COMMENT
-- ON COLUMN DDL клона), а нужен для разбора неустойчивых позиций seq.
-- ---------------------------------------------------------------------------
DO $messages_key$
DECLARE
    pk_name       text;
    duplicate_pos bigint;
BEGIN
    SELECT count(*) INTO duplicate_pos FROM (
        SELECT session_key, seq
          FROM public.agent_session_messages_test
         GROUP BY session_key, seq
        HAVING count(*) > 1
    ) AS duplicated_positions;
    IF duplicate_pos > 0 THEN
        RAISE EXCEPTION
            'в agent_session_messages_test % позиций (session_key, seq) встречаются более '
            'одного раза — составной ключ не навешивается. Дубликаты означают разорванную '
            'запись прежнего писателя. Разберитесь ДО миграции: для зеркала безопасно '
            'удалить все строки этих сессий и дождаться следующего цикла синхронизации',
            duplicate_pos;
    END IF;

    SELECT conname INTO pk_name
      FROM pg_constraint
     WHERE conrelid = 'public.agent_session_messages_test'::regclass
       AND contype  = 'p';

    IF pk_name IS NOT NULL THEN
        EXECUTE format(
            'ALTER TABLE public.agent_session_messages_test DROP CONSTRAINT %I', pk_name);
    END IF;

    ALTER TABLE public.agent_session_messages_test
        ADD CONSTRAINT agent_session_messages_test_pkey
        PRIMARY KEY (replica_id, session_key, seq);
END
$messages_key$;

COMMENT ON COLUMN public.agent_session_messages_test.replica_id IS
    'Реплика-владелец строки; часть ключа наравне с session_key. ''legacy'' — '
    'строки прежнего писателя, происхождение которых неизвестно.';
