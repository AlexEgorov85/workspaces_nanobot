-- ============================================================================
-- V015 — объявление контракта якоря оборота в COMMENT (change 2026-10-04-queue-as-anchor-identity, Ф1.1)
-- ============================================================================
--
-- Что делает: обновляет COMMENT ON COLUMN ... request_id в четырёх таблицах —
-- public.agent_gateway_logs, public.agent_question_runs и их _test-клонов.
-- Структура не меняется: ни одной колонки, индекса, типа и ограничения.
--
-- Почему это вообще миграция, а не правка create-файла: объявленный DDL —
-- это ещё и комментарии, и база, созданная до этого change, несёт в COMMENT
-- ЛОЖНОЕ утверждение о поле. До V015 читатель описания видел:
--   agent_gateway_logs.request_id IS 'FK-логически на agent_question_runs.request_id'
--   agent_question_runs.request_id IS 'PK — ID сообщения, вызвавшего обработку'
-- Первое обещало несуществующий FK (его нет и быть не может: логическое
-- «повода не было» обязано отличаться от «повод был», а FK этого не различает),
-- второе не говорило ничего о том, откуда значение берётся. Новый install
-- получал бы одно, существующая база — другое; расхождение объявленного и
-- живого контракта в DDL, который сам является каноном, — ровно тот класс
-- расхождений, ради которого существует V012 (те-же колонки test-профиля).
--
-- Что НЕ делает и почему:
--   * Не добавляет колонку-якорь. Решение владельца зафиксировано в proposal:
--     якорь живёт в самом request_id, а признак отдельного события — в
--     существующих actor/event_type/channel/metadata. Причина отклонения
--     отдельной колонки: у журнала ДВА писателя с ФИКСИРОВАННЫМИ списками
--     колонок (lib/services/db_logging_service.py и
--     mcp-platform/.../capabilities/data/service/main.py), боевой — платформа,
--     а список колонок запинен стражем tests/test_journal_writer_columns_contract.py.
--   * Не удаляет и не переписывает строки. Ни одного DELETE/TRUNCATE/UPDATE:
--     миграция только объявляет. Уже накопленные строки журнала, лежащие под
--     выдуманными UUID и sentinel'ами пробы, остаются как есть — их уборка
--     описана отдельным пунктом задач и требует согласования владельца.
--   * Не объявляет непустоту. ALTER COLUMN ... SET NOT NULL запрещён в новых
--     миграциях стражем tests/test_runtime_environment_contract.py: на ядре
--     Greenplum 9.4 это полное сканирование таблицы под ACCESS EXCLUSIVE.
--
-- Совместимость: Greenplum 6.5 (ядро PostgreSQL 9.4) и PostgreSQL 13+.
-- Идемпотентна: COMMENT перезаписывает сам себя, таблицы проверяются на
-- существование (на боевой базе _test-клоны отсутствуют — и это не ошибка).
-- ============================================================================

DO $anchor_contract$
BEGIN
    IF to_regclass('public.agent_gateway_logs') IS NOT NULL THEN
        COMMENT ON COLUMN public.agent_gateway_logs.request_id IS
            'ИДЕНТИФИКАТОР ВОПРОСА (якорь оборота) либо пусто — см. change 2026-10-04-queue-as-anchor-identity Ф1. Значение: id строки role=''user'' очереди, взятой оборотом (она же agent_question_runs.request_id), либо объявленное второе пространство subagent:<task_id>. Пусто означает «повода не было»: подписной участок есть, а якоря нет, и это разные вещи. Выдуманный идентификатор, sentinel стартовой пробы (startup-*) и служебные probe-* в это поле не пишутся (UUID4-схема и sentinel отменены коммитом f7e4a8d). Отдельной колонки-якоря нет намеренно: пришлось бы расширять фиксированный список колонок у двух писателей журнала, из которых боевой писатель — платформа. Признак-маркер отдельного события хранится в существующих actor/event_type/channel/metadata.';
    END IF;

    IF to_regclass('public.agent_question_runs') IS NOT NULL THEN
        COMMENT ON COLUMN public.agent_question_runs.request_id IS
            'PK и ЯКОРЬ ОБОРОТА: id строки role=''user'' очереди, взятой этим оборотом, а не идентификатор, придуманный на лету. Значение порождается очередью (её id) и держится до конца оборота; UUID4-схема выдумывала несуществующий вопрос и отменена коммитом f7e4a8d (change 2026-10-04-queue-as-anchor-identity Ф1). Второе объявленное пространство — subagent:<task_id> при is_subagent: true.';
    END IF;

    IF to_regclass('public.agent_gateway_logs_test') IS NOT NULL THEN
        COMMENT ON COLUMN public.agent_gateway_logs_test.request_id IS
            'ИДЕНТИФИКАТОР ВОПРОСА (якорь оборота) либо пусто — см. change 2026-10-04-queue-as-anchor-identity Ф1. Контракт идентичен боевой таблице: id строки role=''user'' очереди взятого оборота либо объявленное второе пространство subagent:<task_id>; пусто означает «повода не было», выдуманный идентификатор и sentinel пробы сюда не пишутся. Отдельной колонки-якоря нет намеренно, признак-маркер лежит в actor/event_type/channel/metadata.';
    END IF;

    IF to_regclass('public.agent_question_runs_test') IS NOT NULL THEN
        COMMENT ON COLUMN public.agent_question_runs_test.request_id IS
            'PK и ЯКОРЬ ОБОРОТА: id строки role=''user'' очереди, взятой этим оборотом, а не идентификатор, придуманный на лету (change 2026-10-04-queue-as-anchor-identity Ф1). Контракт идентичен боевой таблице; UUID4-схема отменена коммитом f7e4a8d. Второе объявленное пространство — subagent:<task_id> при is_subagent: true.';
    END IF;
END
$anchor_contract$;
