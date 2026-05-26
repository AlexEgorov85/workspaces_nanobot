-- ================================================================
-- Таблицы для связки web-сервер ↔ nanobot agent
-- Настраивается в config.json: channels.postgres.{schema}
-- По умолчанию: public.conversation_questions / conversation_answers
-- ================================================================

-- Вопросы (сообщения от пользователей)
CREATE TABLE IF NOT EXISTS public.conversation_questions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chat_id         TEXT,                          -- идентификатор чата/диалога
    user_id         TEXT,                          -- идентификатор пользователя (для allow_from)
    conversation_id UUID NOT NULL,                 -- внутренний ID сессии nanobot
    content         TEXT NOT NULL,                  -- тело сообщения
    media           JSON DEFAULT '[]'::json,        -- массив URL/data-URI файлов
    metadata        JSON DEFAULT '{}'::json,        -- служебные метаданные
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'processing', 'completed', 'failed')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Ответы (сообщения от агента)
CREATE TABLE IF NOT EXISTS public.conversation_answers (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    question_id     UUID REFERENCES public.conversation_questions(id),
    chat_id         TEXT,
    conversation_id UUID NOT NULL,
    content         TEXT,                           -- финальный текст ответа
    reasoning       TEXT,                           -- рассуждения модели (пишется в реальном времени)
    metadata        JSON DEFAULT '{}'::json,        -- служебные метаданные
    buttons         JSON DEFAULT '[]'::json,        -- кнопки из OutboundMessage.buttons
    status          TEXT NOT NULL DEFAULT 'thinking'
                        CHECK (status IN ('thinking', 'streaming', 'completed', 'failed')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Индекс для поллинга вопросов: ищем по status, сортируем по created_at
CREATE INDEX IF NOT EXISTS idx_conversation_questions_poll
    ON public.conversation_questions (status, created_at ASC);

-- Индексы для поиска по conversation_id
CREATE INDEX IF NOT EXISTS idx_conversation_questions_conv
    ON public.conversation_questions (conversation_id, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_conversation_answers_conv
    ON public.conversation_answers (conversation_id, created_at ASC);

-- Индекс для поиска ответа по вопросу
CREATE INDEX IF NOT EXISTS idx_conversation_answers_question
    ON public.conversation_answers (question_id);

-- Индекс для поллинга ответов по статусу и времени обновления
CREATE INDEX IF NOT EXISTS idx_conversation_answers_poll
    ON public.conversation_answers (status, updated_at ASC);

-- Индексы для поиска по chat_id
CREATE INDEX IF NOT EXISTS idx_conversation_questions_chat
    ON public.conversation_questions (chat_id, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_conversation_answers_chat
    ON public.conversation_answers (chat_id, created_at ASC);
