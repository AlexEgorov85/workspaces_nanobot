-- ============================================================================
-- public.agent_session_messages — сообщения сессии (холодное зеркало)
--
-- Холодная копия сообщений сессии. Источник истины — upstream JSONL-стор.
--
-- НЕ append-only. Комментарий исходного DDL утверждал append-only по
-- (session_key, seq), и это было неверно: синхронизация перезаписывает
-- сообщения сессии целиком (DELETE + INSERT в одной транзакции), потому что
-- upstream умеет менять уже сохранённое сообщение, и поштучное дописывание
-- не может сойтись с содержимым файла. Следствие для читателя: seq — это
-- ПОЗИЦИЯ в текущем списке, а не устойчивый идентификатор; после
-- консолидации нумерация сдвигается, поэтому last_consolidated в
-- agent_session_meta может указывать на seq, которого в зеркале уже нет.
-- Восстановление сессии из зеркала номера не сохраняет.
--
-- Совместимость: Greenplum 6.5 (ядро PostgreSQL 9.4) — боевая среда;
-- PostgreSQL 13.22 — тестовый контур. Ранее здесь стояло объявление
-- «PostgreSQL 13.22 (фактическая база)» и «Без FK (GP 6.5 не поддерживает
-- FK)»; первое описывало тестовый контур и выдавало его за боевую среду.
-- FK действительно не объявляется, но по существу, а не из-за Greenplum:
-- каскад выполняет писатель (cleanup_session_mirror), и объявление FK не
-- дало бы ничего, кроме второй проверки того же факта базой.
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.agent_session_messages (
    id                BIGSERIAL,
    replica_id        TEXT NOT NULL,
    session_key       TEXT NOT NULL,
    seq               INT NOT NULL,
    role              TEXT NOT NULL,
    content           TEXT,
    msg_timestamp     TEXT,
    tool_calls        JSONB,
    tool_call_id      TEXT,
    name              TEXT,
    reasoning_content TEXT,
    thinking_blocks   JSONB,
    media             JSONB,
    cli_apps          JSONB,
    mcp_presets       JSONB,
    injected_event    TEXT,
    _command          BOOLEAN,
    _channel_delivery BOOLEAN,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id),
    CONSTRAINT agent_session_messages_replica_session_seq_idx
        UNIQUE (replica_id, session_key, seq)
);

-- КЛЮЧ РАСПРЕДЕЛЕНИЯ НЕ ОБЪЯВЛЕН, и это не пропуск.
--
-- Greenplum 6 разрешает на хеш-распределённой таблице ровно один
-- UNIQUE/PRIMARY KEY, и он обязан включать все столбцы распределения
-- (Summary of Greenplum Features, Greenplum 6). Здесь их два — PRIMARY KEY
-- (id) и UNIQUE (replica_id, session_key, seq) — поэтому на Greenplum 6.5
-- таблица в таком виде не создаётся вовсе, независимо от клаузы
-- распределения.
--
-- Это следствие выбранной схемы зеркала, а не вопрос синтаксиса: закрыть
-- правкой совместимости нельзя, нужно решение, какой из двух ключей
-- остаётся. Пока его нет, файл применяется только там, где оба ключа
-- допустимы. Страж tests/test_runtime_environment_contract.py держит эту
-- позицию в карантине и не даст ей молча разойтись с кодом.

COMMENT ON TABLE  public.agent_session_messages IS 'Холодное зеркало сообщений сессии. Перезаписывается целиком при синхронизации, не append-only. Источник истины — upstream JSONL-стор SessionManager.';
COMMENT ON COLUMN public.agent_session_messages.id                IS 'PK строки. Суррогатный: настоящий ключ строки — (replica_id, session_key, seq).';
COMMENT ON COLUMN public.agent_session_messages.replica_id        IS 'Реплика-владелец строки; часть ключа наравне с session_key. У сессии, общей для двух реплик, у каждой свои сообщения.';
COMMENT ON COLUMN public.agent_session_messages.session_key       IS 'FK-логически на agent_session_meta (replica_id, session_key). FK не объявлен: каскад выполняет писатель.';
COMMENT ON COLUMN public.agent_session_messages.seq               IS 'Позиция сообщения в текущем списке сессии (0, 1, 2, ...). Не устойчивый идентификатор: после консолидации позиции сдвигаются.';
COMMENT ON COLUMN public.agent_session_messages.role              IS 'Роль: user / assistant / system / tool.';
COMMENT ON COLUMN public.agent_session_messages.content           IS 'Текст сообщения.';
COMMENT ON COLUMN public.agent_session_messages.msg_timestamp     IS 'Оригинальный timestamp из upstream (text для совместимости).';
COMMENT ON COLUMN public.agent_session_messages.tool_calls        IS 'JSONB: список вызовов инструментов ассистентом.';
COMMENT ON COLUMN public.agent_session_messages.tool_call_id      IS 'ID вызова инструмента.';
COMMENT ON COLUMN public.agent_session_messages.name              IS 'Имя tool-функции.';
COMMENT ON COLUMN public.agent_session_messages.reasoning_content IS 'Цепочка рассуждений модели.';
COMMENT ON COLUMN public.agent_session_messages.thinking_blocks   IS 'JSONB: расширенное reasoning для thinking-моделей.';
COMMENT ON COLUMN public.agent_session_messages.media             IS 'JSONB: вложения (картинки, файлы, ...).';
COMMENT ON COLUMN public.agent_session_messages.cli_apps          IS 'JSONB: список CLI-приложений, доступных в сообщении.';
COMMENT ON COLUMN public.agent_session_messages.mcp_presets       IS 'JSONB: MCP-конфигурация.';
COMMENT ON COLUMN public.agent_session_messages.injected_event    IS 'Маркер инжектированного события (webhook/timer).';
COMMENT ON COLUMN public.agent_session_messages._command          IS 'Внутренний флаг: системная команда.';
COMMENT ON COLUMN public.agent_session_messages._channel_delivery IS 'Внутренний флаг: доставлено в канал.';
COMMENT ON COLUMN public.agent_session_messages.created_at        IS 'Время записи в БД.';
