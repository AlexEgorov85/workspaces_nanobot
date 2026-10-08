-- ============================================================================
-- public.agent_session_messages_test — test-клон public.agent_session_messages
-- Append-only по (session_key, seq). Без FK (GP 6.5 не поддерживает FK).
-- Каскадное удаление выполняется в коде (pg_session_manager.py).
-- Совместимость: Greenplum 6.5.
--
-- Этот файл живёт ТОЛЬКО для psql-ручного применения; штатное применение
-- всех шести test-таблиц — `python tools/apply_test_profile_tables.py`
-- (миграции схемы test-профиля в репозитории нет).
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.agent_session_messages_test (
    id                BIGSERIAL,
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
    PRIMARY KEY (id)
);

COMMENT ON TABLE  public.agent_session_messages_test IS 'Test-профиль: сообщения чата в рамках сессии (append-only по session_key+seq). Структурный клон public.agent_session_messages.';
COMMENT ON COLUMN public.agent_session_messages_test.id                IS 'PK сообщения.';
COMMENT ON COLUMN public.agent_session_messages_test.session_key       IS 'FK-логически на agent_session_meta_test.session_key (FK не объявлено для GP).';
COMMENT ON COLUMN public.agent_session_messages_test.seq               IS 'Порядковый номер сообщения в сессии (0, 1, 2, ...).';
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
