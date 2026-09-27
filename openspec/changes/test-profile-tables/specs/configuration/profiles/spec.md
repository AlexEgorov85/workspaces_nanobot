## MODIFIED Requirements

### Requirement: Test профиль разрешает test таблицы

Система ДОЛЖНА при активном профиле `test` выбирать test-specific table suffixes (`*_test`) в шести runtime-ключах (`channels.postgres.{table_name, messages_table, meta_table, claims_table}`, `logging.db.{table_name, question_runs_table}`), НЕ ДОЛЖНА молча fallback на non-test имена, и для каждого из шести разрешённых runtime-имён ДОЛЖНА существовать таблица `public.agent_*_test` в БД. Отсутствие таблицы SHALL приводить к ошибке выполнения первого же обращения канала/сервиса/инструмента, а не к тихой подмене на prod-таблицу. Test-таблицы создаются DDL из `sql/<domain>/create_public_agent_*_test.sql` и применяются через миграцию `sql/migrations/V005__test_profile_tables.sql` (или эквивалентный ручной psql-запуск тех же скриптов).

#### Scenario: Тестовая среда запускается под профилем test
- **КОГДА** активный профиль равен `test` и в БД применена миграция V005
- **ТОГДА** первые обращения `PostgresChannel` (`agent_conversation_messages_test`), `PGSessionManager` (`agent_session_meta_test`, `agent_session_messages_test`), `DbLoggingService` (`agent_gateway_logs_test`, `agent_question_runs_test`) и пул воркеров (`agent_worker_claims_test`) SHALL завершаться без `relation does not exist`
