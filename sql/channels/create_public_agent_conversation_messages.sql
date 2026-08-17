-- ============================================================================
-- public.agent_conversation_messages — обмен сообщениями канала PostgresChannel / Web-чата
-- Единотабличная схема: role в role, рассуждения в metadata.reasoning.
-- Управляется: PostgresChannel / Streamlit UI.
-- Совместимость: Greenplum 6.5.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS public.agent_conversation_messages (
    id          UUID NOT NULL DEFAULT gen_random_uuid(),
    chat_id     TEXT,
    user_id     TEXT,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    media       JSONB DEFAULT '[]'::jsonb,
    metadata    JSONB DEFAULT '{}'::jsonb,
    reply_to    UUID,
    buttons     JSONB DEFAULT '[]'::jsonb,
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id)
)
DISTRIBUTED BY (id);

COMMENT ON TABLE  public.agent_conversation_messages IS 'Таблица обмена сообщениями канала PostgresChannel / Web-чата. Агент опрашивает входящие (status=pending), отвечает и пишет ответ обратно в эту же таблицу.';
COMMENT ON COLUMN public.agent_conversation_messages.id         IS 'PK — уникальный ID сообщения (UUID).';
COMMENT ON COLUMN public.agent_conversation_messages.chat_id    IS 'ID чата / диалога.';
COMMENT ON COLUMN public.agent_conversation_messages.user_id    IS 'ID отправителя (пользователь или агент).';
COMMENT ON COLUMN public.agent_conversation_messages.role       IS 'Роль: user / assistant / system / tool.';
COMMENT ON COLUMN public.agent_conversation_messages.content    IS 'Текст сообщения.';
COMMENT ON COLUMN public.agent_conversation_messages.media      IS 'JSONB: вложения (картинки, файлы, ...).';
COMMENT ON COLUMN public.agent_conversation_messages.metadata   IS 'JSONB: дополнительные метаданные (reasoning, session, ...).';
COMMENT ON COLUMN public.agent_conversation_messages.reply_to   IS 'ID родительского сообщения (для связки ответ—вопрос).';
COMMENT ON COLUMN public.agent_conversation_messages.buttons    IS 'JSONB: интерактивные кнопки / инлайн-клавиатура.';
COMMENT ON COLUMN public.agent_conversation_messages.status     IS 'Статус: pending / processing / completed.';
COMMENT ON COLUMN public.agent_conversation_messages.created_at IS 'Время создания сообщения.';
COMMENT ON COLUMN public.agent_conversation_messages.updated_at IS 'Время последнего изменения (статус/reasoning).';
