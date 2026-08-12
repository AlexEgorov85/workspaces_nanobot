-- Скрипты INSERT в public.conversation_messages
-- для тестирования PostgresChannel с однотабличной схемой + reasoning в metadata.
-- Запуск: psql -d <db> -U <user> -f seed_messages.sql
-- PG 9.4: CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
-- GP 6.25: CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ================================================================
-- USER-СООБЩЕНИЯ (role = 'user', status = 'pending')
-- ================================================================

-- 1. Простой вопрос про аудит (alice, начало диалога)
INSERT INTO public.conversation_messages
    (chat_id, user_id, role, content, status)
VALUES
    ('chat_alice_1', 'alice', 'user',
     'Сколько аудиторских проверок проведено за прошлый год?',
     'pending');

-- 2. Запрос с анализом нарушений (bob)
INSERT INTO public.conversation_messages
    (chat_id, user_id, role, content, status)
VALUES
    ('chat_bob_1', 'bob', 'user',
     'Покажи топ-10 нарушений по типам за 2024 год',
     'pending');

-- 3. Векторный поиск по документам (alice, второй чат)
INSERT INTO public.conversation_messages
    (chat_id, user_id, role, content, status)
VALUES
    ('chat_alice_2', 'alice', 'user',
     'Найди документы, связанные с налоговыми проверками',
     'pending');

-- 4. SQL-запрос через LLM (bob)
INSERT INTO public.conversation_messages
    (chat_id, user_id, role, content, status)
VALUES
    ('chat_bob_1', 'bob', 'user',
     'Напиши SQL: сколько объектов проверено в каждом квартале 2024',
     'pending');

-- 5. Диалог из нескольких сообщений (alice, chat_alice_1 — один чат)
--    User-сообщения: role='user', Assistant-ответы: role='assistant', reply_to = user_msg.id
WITH q1 AS (
    INSERT INTO public.conversation_messages
        (chat_id, user_id, role, content, status)
    VALUES
        ('chat_alice_1', 'alice', 'user',
         'Какие типы нарушений чаще всего встречаются?', 'pending')
    RETURNING id
),
a1 AS (
    INSERT INTO public.conversation_messages
        (chat_id, role, content, reply_to, status)
    SELECT 'chat_alice_1', 'assistant',
           'Чаще всего встречаются: налоговые, трудовые и экологические нарушения.',
           q1.id, 'completed'
    FROM q1
)
INSERT INTO public.conversation_messages
    (chat_id, user_id, role, content, status)
SELECT 'chat_alice_1', 'alice', 'user',
       'А какие из них самые дорогие по штрафам?',
       'pending';

-- 6. Запрос с metadata (charlie, web-клиент)
INSERT INTO public.conversation_messages
    (chat_id, user_id, role, content, metadata, status)
VALUES
    ('chat_charlie_1', 'charlie', 'user',
     'Сделай анализ эффективности проверок за 2023-2024: группировка по кварталам, сумма штрафов, количество нарушений',
     '{"client": "web", "priority": "high"}'::jsonb,
     'pending');

-- 7. Уже обработанное user-сообщение (должно пропускаться поллером — status = completed)
INSERT INTO public.conversation_messages
    (chat_id, user_id, role, content, status)
VALUES
    ('chat_alice_1', 'alice', 'user',
     'Это сообщение уже обработано',
     'completed');

-- 8. Сообщение с медиа-файлом (data URI)
INSERT INTO public.conversation_messages
    (chat_id, user_id, role, content, media, status)
VALUES
    ('chat_alice_1', 'alice', 'user',
     'Прочитай этот файл',
     '["data:text/plain;base64,0J/RgNC40LLQtdGCLCDQsdC+0YIhINCt0YLQviDRgtC10YHRgtC+0LLRi9C5INGE0LDQudC7INC40Lcg0LHQsNC30Ysg0LTQsNC90L3Ri9GFLg=="]'::jsonb,
     'pending');

-- 9. Сообщение с файлом по URL
INSERT INTO public.conversation_messages
    (chat_id, user_id, role, content, media, status)
VALUES
    ('chat_bob_1', 'bob', 'user',
     'Проанализируй этот отчёт',
     '["https://example.com/report_2024.pdf"]'::jsonb,
     'pending');

-- 10. Сообщение с несколькими файлами
INSERT INTO public.conversation_messages
    (chat_id, user_id, role, content, media, status)
VALUES
    ('chat_bob_1', 'bob', 'user',
     'Сравни данные из этих двух отчётов',
     '["https://example.com/data_2023.csv", "https://example.com/data_2024.csv"]'::jsonb,
     'pending');

-- 11. Telegram-style сообщение
INSERT INTO public.conversation_messages
    (chat_id, user_id, role, content, metadata, status)
VALUES
    ('tg_12345', 'telegram_user_42', 'user',
     'Покажи статистику по проверкам за март 2025',
     '{"source": "telegram", "username": "test_user"}'::jsonb,
     'pending');

-- 12. Длинный аналитический запрос
INSERT INTO public.conversation_messages
    (chat_id, user_id, role, content, status)
VALUES
    ('chat_alice_1', 'alice', 'user',
     'Проанализируй эффективность аудиторских проверок за последние 3 года. '
     'Нужно: 1) количество проверок по годам, 2) средняя сумма выявленных нарушений, '
     '3) топ-5 объектов с наибольшим количеством нарушений, '
     '4) динамика по кварталам, 5) процент проверок с выявленными нарушениями.',
     'pending');

-- 13. Векторный поиск (charlie)
INSERT INTO public.conversation_messages
    (chat_id, user_id, role, content, status)
VALUES
    ('chat_charlie_1', 'charlie', 'user',
     'Найди похожие случаи: штраф за несоблюдение трудового законодательства',
     'pending');

-- 14. Новый пользователь без chat_id/user_id (тест fallback на user_id)
INSERT INTO public.conversation_messages
    (role, content, status)
VALUES
    ('user',
     'Привет! Это тест без chat_id и user_id (должен работать через fallback)',
     'pending');

-- ================================================================
-- ASSISTANT-СООБЩЕНИЯ (role = 'assistant') — тесты для веб-сервера
-- ================================================================

-- Ответ к сообщению 1 (простой ответ с reasoning в metadata)
INSERT INTO public.conversation_messages
    (chat_id, role, content, metadata, reply_to, status)
SELECT
    chat_id, 'assistant',
    'Вот результат: за прошлый год проведено 1247 аудиторских проверок.',
    '{"reasoning": "Пользователь спрашивает о количестве проверок. Ищем в БД по году."}'::jsonb,
    id, 'completed'
FROM public.conversation_messages
WHERE role = 'user' AND content LIKE 'Сколько аудиторских проверок%'
LIMIT 1;

-- Ответ с кнопками (к сообщению 2)
INSERT INTO public.conversation_messages
    (chat_id, role, content, buttons, reply_to, status)
SELECT
    chat_id, 'assistant',
    'Топ-10 нарушений по типам за 2024 год:\n1. Налоговые — 342\n2. Трудовые — 287\n...',
    '[{"title": "Детали", "url": "https://example.com/violations/2024"}, {"title": "Экспорт CSV", "payload": "/export csv"}]'::jsonb,
    id, 'completed'
FROM public.conversation_messages
WHERE role = 'user' AND content LIKE 'Покажи топ-10 нарушений%'
LIMIT 1;

-- Ответ с reasoning (к сообщению 4 — SQL-запрос)
INSERT INTO public.conversation_messages
    (chat_id, role, content, metadata, reply_to, status)
SELECT
    chat_id, 'assistant',
    'Вот SQL-запрос для подсчёта объектов проверки по кварталам.',
    '{"reasoning": "Надо сгруппировать по EXTRACT(YEAR...) и EXTRACT(QUARTER...) из даты проверки. Использую CHECK_DATE из таблицы inspections."}'::jsonb,
    id, 'completed'
FROM public.conversation_messages
WHERE role = 'user' AND content LIKE 'Напиши SQL%'
LIMIT 1;
