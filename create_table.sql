-- ================================================================
-- Таблица для связки web-сервер ↔ nanobot agent
-- Настраивается в config.json: channels.postgres.{table, schema}
-- По умолчанию: public.conversation_messages
-- ================================================================

CREATE TABLE IF NOT EXISTS public.conversation_messages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chat_id         TEXT,                          -- идентификатор чата/диалога
    user_id         TEXT,                          -- идентификатор пользователя (для allow_from)
    conversation_id UUID NOT NULL,                 -- внутренний ID сессии nanobot
    role            TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content         TEXT NOT NULL,                  -- тело сообщения
    media           JSONB DEFAULT '[]'::jsonb,      -- массив URL/data-URI файлов
    metadata        JSONB DEFAULT '{}',             -- служебные метаданные
    reply_to        UUID,                          -- ID сообщения, на которое ответ (из OutboundMessage.reply_to)
    buttons         JSONB DEFAULT '[]'::jsonb,      -- кнопки из OutboundMessage.buttons
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'processing', 'completed', 'failed')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Индекс для поллинга: ищем по status + role, сортируем по created_at
CREATE INDEX IF NOT EXISTS idx_conversation_messages_poll
    ON public.conversation_messages (status, role, created_at ASC);

-- Индекс для поиска по conversation_id (нужен агенту)
CREATE INDEX IF NOT EXISTS idx_conversation_messages_conv
    ON public.conversation_messages (conversation_id, created_at ASC);

-- Индекс для поиска по chat_id
CREATE INDEX IF NOT EXISTS idx_conversation_messages_chat
    ON public.conversation_messages (chat_id, created_at ASC);
