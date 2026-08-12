-- РЕФЕРЕНСНЫЙ DDL исходных таблиц домена аудита (PostgreSQL 13+ / Greenplum 6+).
--
-- ВНИМАНИЕ: это REFERENCE-схема, восстановленная по колонкам, которые
-- реально использует код навыка audit_analyzer (predefined-скрипты,
-- SQL-генерация через LLM, векторная индексация). Точную структуру
-- таблиц определяет владелец данных: здесь перечислен минимальный
-- набор обязательных колонок, совместимых с запросами навыка.
--
-- Порядок развёртывания:
--   1. sql/create_audit_source_tables_gp.sql  — эти таблицы
--   2. sql/create_audit_vectors_table_gp.sql  — векторные таблицы
--   3. sql/create_vector_index_config_gp.sql  — конфигурация индексов
--   4. python tools/build_vectors.py --full-rebuild

CREATE SCHEMA IF NOT EXISTS oarb;

-- ─────────────────────────────────────────────────────────────────────
-- oarb.audits — аудиторские проверки
-- Колонки, используемые кодом: id, title, audit_type, actual_date,
-- auditee_entity, status
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS oarb.audits (
    id             BIGSERIAL PRIMARY KEY,
    title          TEXT,
    audit_type     TEXT,
    actual_date    DATE,
    auditee_entity TEXT,
    status         TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON COLUMN oarb.audits.audit_type IS 'Тип проверки (например, "финансовый")';
COMMENT ON COLUMN oarb.audits.actual_date IS 'Дата фактического проведения проверки';
COMMENT ON COLUMN oarb.audits.auditee_entity IS 'Проверяемый объект (название организации)';
COMMENT ON COLUMN oarb.audits.status IS 'Статус проверки (например, "закрыто"/"completed")';
COMMENT ON COLUMN oarb.audits.updated_at IS 'Отметка изменений — используется для проверки свежести DuckDB-кеша';

-- ─────────────────────────────────────────────────────────────────────
-- oarb.violations — выявленные нарушения
-- Колонки, используемые кодом: id, audit_id, violation_code, severity
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS oarb.violations (
    id             BIGSERIAL PRIMARY KEY,
    audit_id       BIGINT NOT NULL REFERENCES oarb.audits (id),
    violation_code TEXT,
    severity       NUMERIC,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_violations_audit_id ON oarb.violations (audit_id);

COMMENT ON COLUMN oarb.violations.audit_id IS 'Связь с проверкой (oarb.audits.id)';
COMMENT ON COLUMN oarb.violations.violation_code IS 'Код/тип нарушения (например, "финансовые")';
COMMENT ON COLUMN oarb.violations.severity IS 'Серёзность нарушения (число)';

-- ─────────────────────────────────────────────────────────────────────
-- oarb.audit_reports — отчёты по проверкам (опциональная таблица)
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS oarb.audit_reports (
    id         BIGSERIAL PRIMARY KEY,
    audit_id   BIGINT REFERENCES oarb.audits (id),
    content    TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─────────────────────────────────────────────────────────────────────
-- oarb.report_items — строки/пункты отчётов (опциональная таблица)
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS oarb.report_items (
    id            BIGSERIAL PRIMARY KEY,
    report_id     BIGINT REFERENCES oarb.audit_reports (id),
    violation_id  BIGINT REFERENCES oarb.violations (id),
    content       TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
