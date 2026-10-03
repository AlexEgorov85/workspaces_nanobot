-- ============================================================================
-- oarb.audits — аудиторские проверки (REFERENCE DDL)
-- Доменная таблица навыка audit_analyzer.
-- Колонки, используемые кодом: id, title, audit_type, actual_date,
-- auditee_entity, status, updated_at.
-- Совместимость: Greenplum 6.5.
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS oarb;

CREATE TABLE IF NOT EXISTS oarb.audits (
    id             BIGSERIAL NOT NULL,
    title          TEXT,
    audit_type     TEXT,
    planned_date   DATE,
    actual_date    DATE,
    status         TEXT,
    auditee_entity TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id)
);

-- Распределение объявлено ограждённым шагом, а не в теле CREATE TABLE:
-- файлы из sql/ применяются и к PostgreSQL 13.22 (тестовый контур), где
-- клаузы DISTRIBUTED в синтаксисе нет. Проверка служебного каталога
-- pg_dist_partition отличает Greenplum, поэтому на PostgreSQL шаг — no-op.
DO $distribution$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relname = 'pg_dist_partition'
          AND n.nspname = 'pg_catalog'
    ) THEN
        EXECUTE 'ALTER TABLE oarb.audits
                 SET DISTRIBUTED BY (id)';
    END IF;
END
$distribution$;

COMMENT ON TABLE  oarb.audits IS 'Проверки (плановые и внеплановые аудиторские мероприятия). REFERENCE — уточняется владельцем данных.';
COMMENT ON COLUMN oarb.audits.id             IS 'PK аудита.';
COMMENT ON COLUMN oarb.audits.title          IS 'Наименование / тема проверки.';
COMMENT ON COLUMN oarb.audits.audit_type     IS 'Тип проверки (плановая, внеплановая, ...).';
COMMENT ON COLUMN oarb.audits.planned_date   IS 'Плановая дата проведения.';
COMMENT ON COLUMN oarb.audits.actual_date    IS 'Фактическая дата завершения.';
COMMENT ON COLUMN oarb.audits.status         IS 'Текущий статус проверки.';
COMMENT ON COLUMN oarb.audits.auditee_entity IS 'Проверяемый объект (юр. лицо / организация).';
COMMENT ON COLUMN oarb.audits.created_at     IS 'Время создания записи.';
COMMENT ON COLUMN oarb.audits.updated_at     IS 'Время последнего изменения.';
