-- Скрипты INSERT в public.conversation_messages для тестирования PostgresChannel
-- Запуск: psql -d <db> -U <user> -f seed_messages.sql

-- 1. Простой вопрос про аудит (пользователь alice, начало диалога)
INSERT INTO public.conversation_messages
    (chat_id, user_id, conversation_id, role, content, status)
VALUES
    ('chat_alice_1', 'alice', gen_random_uuid(), 'user',
     'Сколько аудиторских проверок проведено за прошлый год?',
     'pending');

-- 2. Запрос с анализом нарушений (пользователь bob)
INSERT INTO public.conversation_messages
    (chat_id, user_id, conversation_id, role, content, status)
VALUES
    ('chat_bob_1', 'bob', gen_random_uuid(), 'user',
     'Покажи топ-10 нарушений по типам за 2024 год',
     'pending');

-- 3. Векторный поиск по документам (alice, второй чат)
INSERT INTO public.conversation_messages
    (chat_id, user_id, conversation_id, role, content, status)
VALUES
    ('chat_alice_2', 'alice', gen_random_uuid(), 'user',
     'Найди документы, связанные с налоговыми проверками',
     'pending');

-- 4. SQL-запрос через LLM (bob)
INSERT INTO public.conversation_messages
    (chat_id, user_id, conversation_id, role, content, status)
VALUES
    ('chat_bob_1', 'bob', gen_random_uuid(), 'user',
     'Напиши SQL: сколько объектов проверено в каждом квартале 2024',
     'pending');

-- 5. Диалог из нескольких сообщений (alice, chat_alice_1 — тот же чат)
WITH conv AS (
    SELECT gen_random_uuid() AS cid
)
INSERT INTO public.conversation_messages
    (chat_id, user_id, conversation_id, role, content, status)
VALUES
    ('chat_alice_1', 'alice', (SELECT cid FROM conv), 'user',
     'Какие типы нарушений чаще всего встречаются?', 'pending'),
    ('chat_alice_1', 'alice', (SELECT cid FROM conv), 'assistant',
     'Чаще всего встречаются: налоговые, трудовые и экологические нарушения.', 'completed'),
    ('chat_alice_1', 'alice', (SELECT cid FROM conv), 'user',
     'А какие из них самые дорогие по штрафам?', 'pending');

-- 6. Запрос с metadata (charlie, web-клиент)
INSERT INTO public.conversation_messages
    (chat_id, user_id, conversation_id, role, content, metadata, status)
VALUES
    ('chat_charlie_1', 'charlie', gen_random_uuid(), 'user',
     'Сделай анализ эффективности проверок за 2023-2024: группировка по кварталам, сумма штрафов, количество нарушений',
     '{"client": "web", "priority": "high"}'::jsonb,
     'pending');

-- 7. Уже обработанное сообщение (должно пропускаться поллером — status = completed)
INSERT INTO public.conversation_messages
    (chat_id, user_id, conversation_id, role, content, status)
VALUES
    ('chat_alice_1', 'alice', gen_random_uuid(), 'user',
     'Это сообщение уже обработано',
     'completed');

-- 8. Системное сообщение (должно пропускаться — role = system)
INSERT INTO public.conversation_messages
    (chat_id, user_id, conversation_id, role, content, status)
VALUES
    ('chat_bob_1', 'bob', gen_random_uuid(), 'system',
     'system_ready',
     'completed');

-- 9. Сообщение с медиа-файлом (data URI, текстовый файл)
INSERT INTO public.conversation_messages
    (chat_id, user_id, conversation_id, role, content, media, status)
VALUES
    ('chat_alice_1', 'alice', gen_random_uuid(), 'user',
     'Прочитай этот файл',
     '["data:text/plain;base64,0J/RgNC40LLQtdGCLCDQsdC+0YIhINCt0YLQviDRgtC10YHRgtC+0LLRi9C5INGE0LDQudC7INC40Lcg0LHQsNC30Ysg0LTQsNC90L3Ri9GFLg=="]'::jsonb,
     'pending');

-- 10. Сообщение с файлом по URL
INSERT INTO public.conversation_messages
    (chat_id, user_id, conversation_id, role, content, media, status)
VALUES
    ('chat_bob_1', 'bob', gen_random_uuid(), 'user',
     'Проанализируй этот отчёт',
     '["https://example.com/report_2024.pdf"]'::jsonb,
     'pending');

-- 11. Сообщение с несколькими файлами
INSERT INTO public.conversation_messages
    (chat_id, user_id, conversation_id, role, content, media, status)
VALUES
    ('chat_bob_1', 'bob', gen_random_uuid(), 'user',
     'Сравни данные из этих двух отчётов',
     '["https://example.com/data_2023.csv", "https://example.com/data_2024.csv"]'::jsonb,
     'pending');

-- 12. Telegram-style сообщение
INSERT INTO public.conversation_messages
    (chat_id, user_id, conversation_id, role, content, metadata, status)
VALUES
    ('tg_12345', 'telegram_user_42', gen_random_uuid(), 'user',
     'Покажи статистику по проверкам за март 2025',
     '{"source": "telegram", "username": "test_user"}'::jsonb,
     'pending');

-- 13. Длинный аналитический запрос
INSERT INTO public.conversation_messages
    (chat_id, user_id, conversation_id, role, content, status)
VALUES
    ('chat_alice_1', 'alice', gen_random_uuid(), 'user',
     'Проанализируй эффективность аудиторских проверок за последние 3 года. '
     'Нужно: 1) количество проверок по годам, 2) средняя сумма выявленных нарушений, '
     '3) топ-5 объектов с наибольшим количеством нарушений, '
     '4) динамика по кварталам, 5) процент проверок с выявленными нарушениями.',
     'pending');

-- 14. Векторный поиск (charlie)
INSERT INTO public.conversation_messages
    (chat_id, user_id, conversation_id, role, content, status)
VALUES
    ('chat_charlie_1', 'charlie', gen_random_uuid(), 'user',
     'Найди похожие случаи: штраф за несоблюдение трудового законодательства',
     'pending');

-- 15. Новый пользователь без chat_id/user_id (тест fallback на conversation_id)
INSERT INTO public.conversation_messages
    (conversation_id, role, content, status)
VALUES
    (gen_random_uuid(), 'user',
     'Привет! Это тест без chat_id и user_id (должен работать через fallback)',
     'pending');
