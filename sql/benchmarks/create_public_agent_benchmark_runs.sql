-- ============================================================================
-- public.agent_benchmark_runs — мета-информация о прогонах бенчмарков
-- Один прогон = один набор тестов (suite).
-- Управляется: benchmarks/db.py.
-- Совместимость: Greenplum 6.5.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS public.agent_benchmark_runs (
    id              UUID NOT NULL DEFAULT gen_random_uuid(),
    suite_name      TEXT NOT NULL,
    suite_tags      JSONB DEFAULT '[]'::jsonb,
    config          JSONB DEFAULT '{}'::jsonb,
    total_items     INT NOT NULL DEFAULT 0,
    passed_items    INT NOT NULL DEFAULT 0,
    total_score     REAL NOT NULL DEFAULT 0.0,
    avg_score       REAL NOT NULL DEFAULT 0.0,
    duration_sec    REAL NOT NULL DEFAULT 0.0,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at     TIMESTAMPTZ,
    artifacts_dir   TEXT,
    PRIMARY KEY (id)
)
DISTRIBUTED BY (id);

COMMENT ON TABLE  public.agent_benchmark_runs IS 'Мета-информация о прогонах бенчмарков (один прогон = один набор тестов).';
COMMENT ON COLUMN public.agent_benchmark_runs.id            IS 'PK прогона (UUID).';
COMMENT ON COLUMN public.agent_benchmark_runs.suite_name    IS 'Имя тестового набора.';
COMMENT ON COLUMN public.agent_benchmark_runs.suite_tags    IS 'JSONB: теги набора (smoke/full/regression).';
COMMENT ON COLUMN public.agent_benchmark_runs.config        IS 'JSONB: конфигурация прогона.';
COMMENT ON COLUMN public.agent_benchmark_runs.total_items   IS 'Всего вопросов в прогоне.';
COMMENT ON COLUMN public.agent_benchmark_runs.passed_items  IS 'Сколько вопросов прошло.';
COMMENT ON COLUMN public.agent_benchmark_runs.total_score   IS 'Сумма score по всем вопросам.';
COMMENT ON COLUMN public.agent_benchmark_runs.avg_score     IS 'Средний score по вопросам.';
COMMENT ON COLUMN public.agent_benchmark_runs.duration_sec  IS 'Длительность прогона, сек.';
COMMENT ON COLUMN public.agent_benchmark_runs.started_at    IS 'Время начала.';
COMMENT ON COLUMN public.agent_benchmark_runs.finished_at   IS 'Время завершения (NULL пока идёт).';
COMMENT ON COLUMN public.agent_benchmark_runs.artifacts_dir IS 'Каталог файловых отчётов прогона.';
