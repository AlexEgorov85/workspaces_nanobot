-- Трекинг-таблица миграций схемы.
-- Создаётся первой (идемпотентно) перед применением любых версий.
--
-- Ключ распределения — version, то есть первичный ключ: в Greenplum
-- ограничение «ключ распределения должен быть подмножеством ключа» иначе не
-- выполняется, а SET DISTRIBUTED BY не существует в теле CREATE TABLE на
-- PostgreSQL 13.22, где этот файл тоже применяется. Поэтому шаг ограждён
-- проверкой служебного каталога pg_dist_partition.
--
-- Тот же шаг дублирует tools/migrate.py::ensure_tracking_table, который
-- создаёт эту же таблицу и определяет движок по version(). Расхождение
-- осознанное: статический файл обслуживает psql-ручное применение, код —
-- запуск через runner. Ключ распределения у обоих один.

CREATE TABLE IF NOT EXISTS public.schema_migrations (
    version     text PRIMARY KEY,
    name        text NOT NULL,
    checksum    text NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now(),
    applied_by  text NOT NULL DEFAULT current_user,
    duration_ms integer
);

DO $distribution$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relname = 'pg_dist_partition'
          AND n.nspname = 'pg_catalog'
    ) THEN
        EXECUTE 'ALTER TABLE public.schema_migrations
                 SET DISTRIBUTED BY (version)';
    END IF;
END
$distribution$;

COMMENT ON TABLE public.schema_migrations IS
    'История применения миграций схемы (tools/migrate.py)';
