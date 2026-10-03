-- ============================================================================
-- oarb.audit_reports — акты аудиторской проверки (REFERENCE DDL)
-- Доменная таблица навыка audit_analyzer.
-- Колонки, используемые кодом: id, audit_id, report_number, report_date,
-- title, full_text.
-- Совместимость: Greenplum 6.5.
-- ============================================================================

CREATE TABLE IF NOT EXISTS oarb.audit_reports (
    id            BIGSERIAL NOT NULL,
    audit_id      BIGINT NOT NULL,
    report_number TEXT NOT NULL,
    report_date   DATE NOT NULL,
    title         TEXT NOT NULL,
    full_text     TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
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
        EXECUTE 'ALTER TABLE oarb.audit_reports
                 SET DISTRIBUTED BY (audit_id)';
    END IF;
END
$distribution$;

COMMENT ON TABLE  oarb.audit_reports IS 'Акты аудиторской проверки (оформленные документы по результатам). REFERENCE — уточняется владельцем данных.';
COMMENT ON COLUMN oarb.audit_reports.id            IS 'PK акта.';
COMMENT ON COLUMN oarb.audit_reports.audit_id      IS 'FK-логически на oarb.audits.id.';
COMMENT ON COLUMN oarb.audit_reports.report_number IS 'Номер акта (внутренняя нумерация).';
COMMENT ON COLUMN oarb.audit_reports.report_date   IS 'Дата составления акта.';
COMMENT ON COLUMN oarb.audit_reports.title         IS 'Название акта / заголовок.';
COMMENT ON COLUMN oarb.audit_reports.full_text     IS 'Полный текст акта (если без разбивки на пункты).';
COMMENT ON COLUMN oarb.audit_reports.created_at    IS 'Время создания записи.';
COMMENT ON COLUMN oarb.audit_reports.updated_at    IS 'Время последнего обновления.';
