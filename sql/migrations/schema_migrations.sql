-- Трекинг-таблица миграций схемы.
-- Создаётся первой (идемпотентно) перед применением любых версий.

CREATE TABLE IF NOT EXISTS public.schema_migrations (
    version     text PRIMARY KEY,
    name        text NOT NULL,
    checksum    text NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now(),
    applied_by  text NOT NULL DEFAULT current_user,
    duration_ms integer
);

COMMENT ON TABLE public.schema_migrations IS
    'История применения миграций схемы (tools/migrate.py)';
