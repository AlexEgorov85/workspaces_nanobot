-- ============================================================================
-- public.agent_conversation_messages_test — test-клон public.agent_conversation_messages
-- Структура идентична prod-собрату; суффикс _test жёстко зафиксирован
-- profiles/test.jsonc (channels.postgres.table_name). Управляется так же:
-- PostgresChannel / Streamlit UI. Совместимость: Greenplum 6.5.
--
-- Этот файл живёт ТОЛЬКО для psql-ручного применения (`psql -f`); для
-- версионированного применения через runner — V005__test_profile_tables.sql.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS public.agent_conversation_messages_test (
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
);

COMMENT ON TABLE  public.agent_conversation_messages_test IS 'Test-профиль: обмен сообщениями канала PostgresChannel / Web-чата. Структурный клон public.agent_conversation_messages; используется под профилем test для изоляции тестовых прогонов от prod.';
COMMENT ON COLUMN public.agent_conversation_messages_test.id         IS 'PK — уникальный ID сообщения (UUID).';
COMMENT ON COLUMN public.agent_conversation_messages_test.chat_id    IS 'ID чата / диалога.';
COMMENT ON COLUMN public.agent_conversation_messages_test.user_id    IS 'ID отправителя (пользователь или агент).';
COMMENT ON COLUMN public.agent_conversation_messages_test.role       IS 'Роль: user / assistant / system / tool.';
COMMENT ON COLUMN public.agent_conversation_messages_test.content    IS 'Текст сообщения.';
COMMENT ON COLUMN public.agent_conversation_messages_test.media      IS 'JSONB: вложения (картинки, файлы, ...).';
COMMENT ON COLUMN public.agent_conversation_messages_test.metadata   IS 'JSONB: дополнительные метаданные (reasoning, session, ...).';
COMMENT ON COLUMN public.agent_conversation_messages_test.reply_to   IS 'ID родительского сообщения (для связки ответ—вопрос).';
COMMENT ON COLUMN public.agent_conversation_messages_test.buttons    IS 'JSONB: интерактивные кнопки / инлайн-клавиатура.';
COMMENT ON COLUMN public.agent_conversation_messages_test.status     IS 'Статус: pending / processing / completed / error (повторяемая ошибка) / failed (терминальный, не меняется).';
COMMENT ON COLUMN public.agent_conversation_messages_test.created_at IS 'Время создания сообщения.';
COMMENT ON COLUMN public.agent_conversation_messages_test.updated_at IS 'Время последнего изменения (статус/reasoning).';
