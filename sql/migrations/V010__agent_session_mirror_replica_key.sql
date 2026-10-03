-- ============================================================================
-- V010 — replica_id в ключ зеркала сессий (change session-mirror-mcp)
-- ============================================================================
-- Зеркало сессий (`agent_session_meta` / `agent_session_messages`) переводится
-- с «строка на сессию» на «строка на (реплика, сессия)». Причина не в красоте
-- ключа, а в том, что текущая схема при нескольких репликах УНИЧТОЖАЕТ данные:
--
--   1. `session_key` был первичным ключом, поэтому строки сессии, которую видят
--      две реплики, делят одну строку и бьются за неё last-write-wins.
--   2. Цикл очистки удалял из зеркала всё, чего нет в СВОЁМ списке сессий
--      (`SessionColdSyncService._cleanup_missing`). У реплики B есть сессии,
--      которых нет в списке реплики A, — и реплика A удаляла их каждые 30
--      секунд. Multi-instance (заявленная цель зеркала) не работал вовсе.
--   3. Две реплики, одновременно вытягивающие одну новую сессию, получали
--      нарушение уникальности: писатель делал UPDATE, а при отсутствии строки —
--      INSERT без ON CONFLICT.
--
-- Ключ (replica_id, session_key) закрывает все три разом: разграничение
-- принадлежности становится частью ключа, а не соглашением в коде, поэтому
-- забыть его нельзя — забытый replica_id даст нарушение первичного ключа, а не
-- тихую порчу чужих строк.
--
-- replica_id НЕ выдумывается для существующих строк: какая реплика их писала,
-- неизвестно, и придуманное значение неотличимо от настоящего. Всем прежним
-- строкам проставляется явный маркер 'legacy' (шаг 2), который ни одна
-- реплика не пишет. Следствие: новый писатель таких строк не увидит (они не
-- его) и не перепишет — очистка шага 5 разбирает их отдельно и по явному
-- решению оператора.
--
-- ПОРЯДОК ШАГОВ ОБЯЗАТЕЛЕН и повторяет уроки V008: nullable-колонка → backfill →
-- проверка нулевой остачи → SET NOT NULL → смена первичного ключа. Обратный
-- порядок ломает существующие строки базой: NOT NULL до backfill падает на
-- строках без значения, а backfill после него невозможен.
--
-- Совместимость: PostgreSQL 13.22 (фактическая база). Синтаксиса распределённой
-- СУБД здесь нет и быть не должно: `DISTRIBUTED BY` из исходного DDL убран
-- ещё раньше, потому что pg_dist_partition на сервере отсутствует. Из этого
-- следует, что локальности по session_key больше нет — её заменяет индекс
-- V011, и до его создания выборки по сессии сканируют таблицу.
--
-- Идемпотентна. Регистрация версии выполняется runner'ом tools/migrate.py
-- (INSERT в public.schema_migrations) — НЕ этим файлом.
--
-- ВНИМАНИЕ: этот файл НЕ применяется к боевой базе автоматически. Шаг 5
-- удаляет данные и выполняется только по явному решению оператора (см. ниже).
--
-- ПОРЯДОК ПРИМЕНЕНИЯ ЖЁСТКИЙ: V010 применяется ТОЛЬКО вместе с писателем,
-- который заполняет replica_id. Старый писатель вставлял строки без этой
-- колонки, поэтому после шага 5 его INSERT отвергся бы NOT NULL на первой же
-- синхронизации — и не запись, а падение всего цикла. Иными словами: либо
-- сначала переводится писатель, либо миграция ждёт; «применим и посмотрим»
-- здесь не работает.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- Шаг 1 — колонки. replica_id сначала nullable: NOT NULL на существующих
-- строках упал бы на backfill, которого ещё не было.
-- ---------------------------------------------------------------------------
ALTER TABLE public.agent_session_meta
    ADD COLUMN IF NOT EXISTS replica_id      TEXT,
    ADD COLUMN IF NOT EXISTS source_digest   TEXT,
    ADD COLUMN IF NOT EXISTS missing_cycles  INT  NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS message_count   INT,
    ADD COLUMN IF NOT EXISTS synced_at       TIMESTAMPTZ;

ALTER TABLE public.agent_session_messages
    ADD COLUMN IF NOT EXISTS replica_id TEXT;

-- ---------------------------------------------------------------------------
-- Шаг 2 — backfill. Явный маркер 'legacy', а не значение по умолчанию из
-- шага 1: DEFAULT применяется к НОВЫМ строкам, а этим нужна честная метка
-- «происхождение неизвестно». Новый писатель никогда 'legacy' не пишет, и
-- поэтому подмены настоящей реплики маркером произойти не может.
-- ---------------------------------------------------------------------------
UPDATE public.agent_session_meta
   SET replica_id = 'legacy'
 WHERE replica_id IS NULL;

UPDATE public.agent_session_messages
   SET replica_id = 'legacy'
 WHERE replica_id IS NULL;

-- ---------------------------------------------------------------------------
-- Шаг 3 — проверка ДО ограничения, чтобы падение называло причину, а не
-- «violates not-null constraint».
-- ---------------------------------------------------------------------------
DO $verify$
DECLARE
    meta_leftover     bigint;
    message_leftover  bigint;
BEGIN
    SELECT count(*) INTO meta_leftover
      FROM public.agent_session_meta WHERE replica_id IS NULL;
    IF meta_leftover > 0 THEN
        RAISE EXCEPTION
            'в agent_session_meta осталось % строк без replica_id: '
            'backfill не выполнен. SET NOT NULL не применяется', meta_leftover;
    END IF;

    SELECT count(*) INTO message_leftover
      FROM public.agent_session_messages WHERE replica_id IS NULL;
    IF message_leftover > 0 THEN
        RAISE EXCEPTION
            'в agent_session_messages осталось % строк без replica_id: '
            'backfill не выполнен. SET NOT NULL не применяется', message_leftover;
    END IF;
END
$verify$;

ALTER TABLE public.agent_session_meta
    ALTER COLUMN replica_id SET NOT NULL;

ALTER TABLE public.agent_session_messages
    ALTER COLUMN replica_id SET NOT NULL;

-- ---------------------------------------------------------------------------
-- Шаг 4 — первичный ключ. Старый снимается до нового: два ключа на одну
-- таблицу невозможны, а сначала добавить новый нельзя — он бы продублировал
-- существующие строки.
-- ---------------------------------------------------------------------------
ALTER TABLE public.agent_session_meta
    DROP CONSTRAINT IF EXISTS agent_session_meta_pkey;

ALTER TABLE public.agent_session_meta
    ADD CONSTRAINT agent_session_meta_pkey PRIMARY KEY (replica_id, session_key);

COMMENT ON COLUMN public.agent_session_meta.replica_id IS
    'Реплика-владелец строки. Часть первичного ключа: одна и та же сессия на '
    'двух репликах — это ДВЕ строки, а не одна, за которую спорят '
    'last-write-wins. ''legacy'' — строки, записанные до появления реплик: '
    'какой инстанс их писал, неизвестно, и подставлять догадку нельзя. Пишет '
    'операция mirror_session платформы, не вызывающая сторона.';

COMMENT ON COLUMN public.agent_session_meta.source_digest IS
    'SHA-256 файла сессии (JSONL) на момент зеркалирования. ПРИЗНАК ИЗМЕНЕНИЯ, '
    'а не updated_at: upstream не поднимает updated_at при изменении metadata '
    'сессии (JsonlSessionStore.update_metadata переписывает только поле '
    'metadata первой строки), поэтому по updated_at такие правки не видны '
    'никогда. NULL — строка записана до появления дайджеста; она требует '
    'однократной принудительной синхронизации.';

COMMENT ON COLUMN public.agent_session_meta.missing_cycles IS
    'Сколько циклов подряд сессия была в зеркале, но отсутствовала в списке '
    'upstream. Удаление происходит не по первому пропуску, а по достижении '
    'порога: один пустой или частичный список сессий (сетевая ошибка, каталог '
    'на NFS) иначе стёр бы всё зеркало. Сбрасывается в 0 любой синхронизацией.';

COMMENT ON COLUMN public.agent_session_meta.message_count IS
    'Сколько строк сообщений зеркала принадлежит этой сессии. Сверяется с '
    'фактическим числом строк: расхождение означает разорванную запись '
    '(metadata обновилась, сообщения нет) и требует принудительной '
    'синхронизации — без неё зеркало осталось бы разорванным навсегда, '
    'потому что updated_at уже совпал бы с upstream.';

COMMENT ON COLUMN public.agent_session_meta.synced_at IS
    'Момент последней записи строки зеркала. Отделяет «файл давно не менялся» '
    'от «зеркалом перестали писать»: по одному updated_at эти случаи '
    'неразличимы.';

COMMENT ON COLUMN public.agent_session_messages.replica_id IS
    'Реплика-владелец строки; часть ключа наравне с session_key. Сессия, '
    'общая для двух реплик, хранит у каждой СВОИ сообщения.';

-- ---------------------------------------------------------------------------
-- Шаг 5 — разбор строк 'legacy'. УДАЛЯЕТ ДАННЫЕ и по умолчанию пропускается.
--
-- Обоснование флага: зеркало живых сессий восстанавливается из JSONL, но у
-- сессий, удалённых из upstream, только зеркало и останется. Спецификация
-- удаление исторического зеркала не авторизует, объём определяется замером ДО
-- операции, а решение принимает оператор.
--
-- Пропуск шага НЕ ломает следующие шаги: строки 'legacy' просто невидимы для
-- нового писателя (не его реплика) и не мешают работе. В отличие от V008
-- здесь ничего не падает — потому что падать не на что.
-- ---------------------------------------------------------------------------
DO $cleanup$
DECLARE
    legacy_meta    bigint;
    legacy_message bigint;
BEGIN
    IF current_setting('nanobot.cleanup_legacy_session_mirror', true)
       IS DISTINCT FROM 'on' THEN
        SELECT count(*) INTO legacy_meta
          FROM public.agent_session_meta WHERE replica_id = 'legacy';
        SELECT count(*) INTO legacy_message
          FROM public.agent_session_messages WHERE replica_id = 'legacy';
        RAISE NOTICE
            'шаг 5 (разбор строк ''legacy'') ПРОПУЩЕН: % строк мета и % строк '
            'сообщений остаются в зеркале и не обслуживаются новым писателем. '
            'Очистить: SET nanobot.cleanup_legacy_session_mirror = ''on'' и '
            'повторить миграцию', legacy_meta, legacy_message;
        RETURN;
    END IF;

    DELETE FROM public.agent_session_messages WHERE replica_id = 'legacy';
    GET DIAGNOSTICS legacy_message = ROW_COUNT;

    DELETE FROM public.agent_session_meta WHERE replica_id = 'legacy';
    GET DIAGNOSTICS legacy_meta = ROW_COUNT;

    RAISE NOTICE 'удалено строк ''legacy'': мета %, сообщений %',
        legacy_meta, legacy_message;
END
$cleanup$;
