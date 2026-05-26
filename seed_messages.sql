-- Скрипты INSERT в public.conversation_questions / conversation_answers
-- для тестирования PostgresChannel с двухтабличной схемой + reasoning.
-- Запуск: psql -d <db> -U <user> -f seed_messages.sql

-- ================================================================
-- ВОПРОСЫ (INSERT в conversation_questions)
-- ================================================================

-- 1. Простой вопрос про аудит (alice, начало диалога)
INSERT INTO public.conversation_questions
    (chat_id, user_id, conversation_id, content, status)
VALUES
    ('chat_alice_1', 'alice', gen_random_uuid(),
     'Сколько аудиторских проверок проведено за прошлый год?',
     'pending');

-- 2. Запрос с анализом нарушений (bob)
INSERT INTO public.conversation_questions
    (chat_id, user_id, conversation_id, content, status)
VALUES
    ('chat_bob_1', 'bob', gen_random_uuid(),
     'Покажи топ-10 нарушений по типам за 2024 год',
     'pending');

-- 3. Векторный поиск по документам (alice, второй чат)
INSERT INTO public.conversation_questions
    (chat_id, user_id, conversation_id, content, status)
VALUES
    ('chat_alice_2', 'alice', gen_random_uuid(),
     'Найди документы, связанные с налоговыми проверками',
     'pending');

-- 4. SQL-запрос через LLM (bob)
INSERT INTO public.conversation_questions
    (chat_id, user_id, conversation_id, content, status)
VALUES
    ('chat_bob_1', 'bob', gen_random_uuid(),
     'Напиши SQL: сколько объектов проверено в каждом квартале 2024',
     'pending');

-- 5. Диалог из нескольких сообщений (alice, chat_alice_1 — один чат)
--    Каждому user-сообщению → вопрос, каждому assistant-сообщению → ответ
WITH conv AS (
    SELECT gen_random_uuid() AS cid
),
q1 AS (
    INSERT INTO public.conversation_questions
        (chat_id, user_id, conversation_id, content, status)
    VALUES
        ('chat_alice_1', 'alice', (SELECT cid FROM conv),
         'Какие типы нарушений чаще всего встречаются?', 'pending')
    RETURNING id, conversation_id
),
a1 AS (
    INSERT INTO public.conversation_answers
        (question_id, chat_id, conversation_id, content, status)
    SELECT q1.id, 'chat_alice_1', q1.conversation_id,
           'Чаще всего встречаются: налоговые, трудовые и экологические нарушения.',
           'completed'
    FROM q1
)
INSERT INTO public.conversation_questions
    (chat_id, user_id, conversation_id, content, status)
SELECT 'chat_alice_1', 'alice', (SELECT cid FROM conv),
       'А какие из них самые дорогие по штрафам?',
       'pending';

-- 6. Запрос с metadata (charlie, web-клиент)
INSERT INTO public.conversation_questions
    (chat_id, user_id, conversation_id, content, metadata, status)
VALUES
    ('chat_charlie_1', 'charlie', gen_random_uuid(),
     'Сделай анализ эффективности проверок за 2023-2024: группировка по кварталам, сумма штрафов, количество нарушений',
     '{"client": "web", "priority": "high"}'::json,
     'pending');

-- 7. Уже обработанный вопрос (должен пропускаться поллером — status = completed)
INSERT INTO public.conversation_questions
    (chat_id, user_id, conversation_id, content, status)
VALUES
    ('chat_alice_1', 'alice', gen_random_uuid(),
     'Это сообщение уже обработано',
     'completed');

-- 8. Сообщение с медиа-файлом (data URI)
INSERT INTO public.conversation_questions
    (chat_id, user_id, conversation_id, content, media, status)
VALUES
    ('chat_alice_1', 'alice', gen_random_uuid(),
     'Прочитай этот файл',
     '["data:text/plain;base64,0J/RgNC40LLQtdGCLCDQsdC+0YIhINCt0YLQviDRgtC10YHRgtC+0LLRi9C5INGE0LDQudC7INC40Lcg0LHQsNC30Ysg0LTQsNC90L3Ri9GFLg=="]'::json,
     'pending');

-- 9. Сообщение с файлом по URL
INSERT INTO public.conversation_questions
    (chat_id, user_id, conversation_id, content, media, status)
VALUES
    ('chat_bob_1', 'bob', gen_random_uuid(),
     'Проанализируй этот отчёт',
     '["https://example.com/report_2024.pdf"]'::json,
     'pending');

-- 10. Сообщение с несколькими файлами
INSERT INTO public.conversation_questions
    (chat_id, user_id, conversation_id, content, media, status)
VALUES
    ('chat_bob_1', 'bob', gen_random_uuid(),
     'Сравни данные из этих двух отчётов',
     '["https://example.com/data_2023.csv", "https://example.com/data_2024.csv"]'::json,
     'pending');

-- 11. Telegram-style сообщение
INSERT INTO public.conversation_questions
    (chat_id, user_id, conversation_id, content, metadata, status)
VALUES
    ('tg_12345', 'telegram_user_42', gen_random_uuid(),
     'Покажи статистику по проверкам за март 2025',
     '{"source": "telegram", "username": "test_user"}'::json,
     'pending');

-- 12. Длинный аналитический запрос
INSERT INTO public.conversation_questions
    (chat_id, user_id, conversation_id, content, status)
VALUES
    ('chat_alice_1', 'alice', gen_random_uuid(),
     'Проанализируй эффективность аудиторских проверок за последние 3 года. '
     'Нужно: 1) количество проверок по годам, 2) средняя сумма выявленных нарушений, '
     '3) топ-5 объектов с наибольшим количеством нарушений, '
     '4) динамика по кварталам, 5) процент проверок с выявленными нарушениями.',
     'pending');

-- 13. Векторный поиск (charlie)
INSERT INTO public.conversation_questions
    (chat_id, user_id, conversation_id, content, status)
VALUES
    ('chat_charlie_1', 'charlie', gen_random_uuid(),
     'Найди похожие случаи: штраф за несоблюдение трудового законодательства',
     'pending');

-- 14. Новый пользователь без chat_id/user_id (тест fallback на conversation_id)
INSERT INTO public.conversation_questions
    (conversation_id, content, status)
VALUES
    (gen_random_uuid(),
     'Привет! Это тест без chat_id и user_id (должен работать через fallback)',
     'pending');

-- ================================================================
-- ОТВЕТЫ (INSERT в conversation_answers) — тесты для веб-сервера
-- ================================================================

-- Ответ к вопросу 7 (показываем, что ответ может быть готов заранее)
INSERT INTO public.conversation_answers
    (question_id, chat_id, conversation_id, content, reasoning, status)
SELECT
    id, chat_id, conversation_id,
    'Вот результат: за прошлый год проведено 1247 аудиторских проверок.',
    'Пользователь спрашивает о количестве проверок. Ищем в БД по году.',
    'completed'
FROM public.conversation_questions
WHERE content LIKE 'Сколько аудиторских проверок%'
LIMIT 1;

-- Ответ с кнопками (к вопросу 2)
INSERT INTO public.conversation_answers
    (question_id, chat_id, conversation_id, content, buttons, status)
SELECT
    id, chat_id, conversation_id,
    'Топ-10 нарушений по типам за 2024 год:\n1. Налоговые — 342\n2. Трудовые — 287\n...',
    '[{"title": "Детали", "url": "https://example.com/violations/2024"}, {"title": "Экспорт CSV", "payload": "/export csv"}]'::json,
    'completed'
FROM public.conversation_questions
WHERE content LIKE 'Покажи топ-10 нарушений%'
LIMIT 1;

-- Ответ с reasoning (демонстрирует реальный поток рассуждений)
INSERT INTO public.conversation_answers
    (question_id, chat_id, conversation_id, content, reasoning, status)
SELECT
    id, chat_id, conversation_id,
    'Вот SQL-запрос для подсчёта объектов проверки по кварталам.',
    'Надо сгруппировать по EXTRACT(YEAR...) и EXTRACT(QUARTER...) из даты проверки. '
    'Использую CHECK_DATE из таблицы inspections.',
    'completed'
FROM public.conversation_questions
WHERE content LIKE 'Напиши SQL%'
LIMIT 1;
