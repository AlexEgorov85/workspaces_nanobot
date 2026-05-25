-- Миграция: добавить колонки в существующую таблицу
ALTER TABLE public.conversation_messages
    ADD COLUMN IF NOT EXISTS chat_id TEXT,
    ADD COLUMN IF NOT EXISTS user_id TEXT,
    ADD COLUMN IF NOT EXISTS media JSONB DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS reply_to UUID,
    ADD COLUMN IF NOT EXISTS buttons JSONB DEFAULT '[]'::jsonb;

-- Индекс для поиска по chat_id
CREATE INDEX IF NOT EXISTS idx_conversation_messages_chat
    ON public.conversation_messages (chat_id, created_at ASC);
