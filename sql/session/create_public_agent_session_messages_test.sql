-- ============================================================================
-- public.agent_session_messages_test — test-клон public.agent_session_messages
--
-- Структурный клон боевой таблицы зеркала сессий под профилем test.
--
-- НЕ append-only: синхронизация перезаписывает сообщения сессии целиком.
-- seq — позиция, а не устойчивый идентификатор.
--
-- Совместимость: PostgreSQL 13.22 (фактическая база). Ранее здесь стояло
-- «Совместимость: Greenplum 6.5», «Без FK (GP 6.5 не поддерживает FK)» и
-- `DISTRIBUTED BY (session_key)` — объявления ложные.
--
-- Этот файл живёт ТОЛЬКО для psql-ручного применения.
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.agent_session_messages_test (
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
    CONSTRAINT agent_session_messages_test_replica_session_seq_idx
        UNIQUE (replica_id, session_key, seq)
);

COMMENT ON TABLE  public.agent_session_messages_test IS 'Test-профиль: холодное зеркало сообщений сессии. Структурный клон public.agent_session_messages.';
COMMENT ON COLUMN public.agent_session_messages_test.id                IS 'PK строки. Суррогатный: настоящий ключ — (replica_id, session_key, seq).';
COMMENT ON COLUMN public.agent_session_messages_test.replica_id        IS 'Реплика-владелец строки; часть ключа наравне с session_key.';
COMMENT ON COLUMN public.agent_session_messages_test.session_key       IS 'FK-логически на agent_session_meta_test (replica_id, session_key).';
COMMENT ON COLUMN public.agent_session_messages_test.seq               IS 'Позиция сообщения в текущем списке сессии. Не устойчивый идентификатор.';
COMMENT ON COLUMN public.agent_session_messages_test.role              IS 'Роль: user / assistant / system / tool.';
COMMENT ON COLUMN public.agent_session_messages_test.content           IS 'Текст сообщения.';
COMMENT ON COLUMN public.agent_session_messages_test.msg_timestamp     IS 'Оригинальный timestamp из upstream (text для совместимости).';
COMMENT ON COLUMN public.agent_session_messages_test.tool_calls        IS 'JSONB: список вызовов инструментов ассистентом.';
COMMENT ON COLUMN public.agent_session_messages_test.tool_call_id      IS 'ID вызова инструмента.';
COMMENT ON COLUMN public.agent_session_messages_test.name              IS 'Имя tool-функции.';
COMMENT ON COLUMN public.agent_session_messages_test.reasoning_content IS 'Цепочка рассуждений модели.';
COMMENT ON COLUMN public.agent_session_messages_test.thinking_blocks   IS 'JSONB: расширенное reasoning для thinking-моделей.';
COMMENT ON COLUMN public.agent_session_messages_test.media             IS 'JSONB: вложения (картинки, файлы, ...).';
COMMENT ON COLUMN public.agent_session_messages_test.cli_apps          IS 'JSONB: список CLI-приложений, доступных в сообщении.';
COMMENT ON COLUMN public.agent_session_messages_test.mcp_presets       IS 'JSONB: MCP-конфигурация.';
COMMENT ON COLUMN public.agent_session_messages_test.injected_event    IS 'Маркер инжектированного события (webhook/timer).';
COMMENT ON COLUMN public.agent_session_messages_test._command          IS 'Внутренний флаг: системная команда.';
COMMENT ON COLUMN public.agent_session_messages_test._channel_delivery IS 'Внутренний флаг: доставлено в канал.';
COMMENT ON COLUMN public.agent_session_messages_test.created_at        IS 'Время записи в БД.';
