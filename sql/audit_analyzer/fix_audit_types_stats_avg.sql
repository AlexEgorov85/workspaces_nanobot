-- ============================================================================
--  Migration: fix audit_types_stats AVG(severity) bug + JSON-encoded corruption
-- ============================================================================
--  Bug 1: audit_types_stats использовал AVG(v.severity) где severity = VARCHAR
--  ('Низкая', 'Средняя', 'Высокая'). DuckDB AVG() требует числовых аргументов,
--  скрипт падал с Binder Error.
--
--  Fix 1: CASE-based numeric mapping (Низкая=1, Средняя=2, Высокая=3), NULL для
--  неизвестных значений. Осмысленная шкала — сохраняет порядок severity.
--
--  Bug 2: В PG sql_template хранился JSON-кодированный ('{"..."}') — обёртка
--  от psycopg2.extras.Json (зарегистрирован как адаптер dict). Из-за этого DuckDB
--  cache получал мусор вместо SQL. Обновлено через raw psycopg2 connection
--  (без utils.db.execute, который триггерит JSON-адаптер).
--
--  Идемпотентно: повторный запуск перезаписывает шаблон.
--  Применяется ПОСЛЕ create_public_agent_predefined_scripts.sql и
--  seed_default_indexes.sql.
--
--  Применение:
--    psql "$DATABASE_URL" -f sql/audit_analyzer/fix_audit_types_stats_avg.sql
--
--  Альтернатива: tools/generate_predefined_scripts_sql.py --from-db
--  (выгрузит текущее состояние реестра; затем правки вносятся в этот файл).
-- ============================================================================

UPDATE public.agent_predefined_scripts
SET sql_template = '
            SELECT
                a.audit_type,
                COUNT(*) AS audit_count,
                COUNT(DISTINCT a.auditee_entity) AS unique_objects,
                COUNT(v.id) AS total_violations,
                AVG(
                    CASE v.severity
                        WHEN ''Низкая'' THEN 1
                        WHEN ''Средняя'' THEN 2
                        WHEN ''Высокая'' THEN 3
                        ELSE NULL
                    END
                ) AS avg_severity,
                MAX(a.actual_date) AS last_audit_date
            FROM oarb.audits a
            LEFT JOIN oarb.violations v ON a.id = v.audit_id
            WHERE a.actual_date IS NOT NULL
              AND a.audit_type IS NOT NULL
            {% if audit_type %} AND a.audit_type ILIKE :audit_type {% endif %}
            {% if date_from %} AND a.actual_date >= :date_from {% endif %}
            GROUP BY a.audit_type
            ORDER BY audit_count DESC
         '
WHERE name = 'audit_types_stats';
