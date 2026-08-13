-- Greenplum 6.25: create_benchmark_tables.sql + DISTRIBUTED BY, pgcrypto, без FK
-- Префикс agent_ — таблицы агента (не навыка).

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- миграция со старых имён
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema = 'public' AND table_name = 'benchmark_runs') THEN
    ALTER TABLE public.benchmark_runs RENAME TO agent_benchmark_runs;
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema = 'public' AND table_name = 'benchmark_results') THEN
    ALTER TABLE public.benchmark_results RENAME TO agent_benchmark_results;
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS public.agent_benchmark_runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    suite_name      TEXT NOT NULL,
    suite_tags      JSONB DEFAULT '[]'::jsonb,
    config          JSONB DEFAULT '{}'::jsonb,
    total_items     INT NOT NULL DEFAULT 0,
    passed_items    INT NOT NULL DEFAULT 0,
    total_score     REAL NOT NULL DEFAULT 0.0,
    avg_score       REAL NOT NULL DEFAULT 0.0,
    duration_sec    REAL NOT NULL DEFAULT 0.0,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at     TIMESTAMPTZ
)
DISTRIBUTED BY (id);

CREATE TABLE IF NOT EXISTS public.agent_benchmark_results (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id          UUID NOT NULL,
    item_id         TEXT NOT NULL,
    item_name       TEXT NOT NULL,
    difficulty      INT NOT NULL,
    category        TEXT,
    item_type       TEXT NOT NULL,
    passed          BOOLEAN NOT NULL DEFAULT FALSE,
    score           REAL NOT NULL DEFAULT 0.0,
    response        TEXT,
    tools_used      JSONB DEFAULT '[]'::jsonb,
    skills_activated JSONB DEFAULT '[]'::jsonb,
    total_iterations INT NOT NULL DEFAULT 0,
    duration_sec    REAL NOT NULL DEFAULT 0.0,
    error           TEXT,
    llm_judge_score REAL,
    details         JSONB DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
DISTRIBUTED BY (run_id);

-- Индексы (GP: CREATE INDEX без IF NOT EXISTS)
DO $gp$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_indexes
                   WHERE schemaname = 'public' AND indexname = 'idx_agent_benchmark_results_run') THEN
        CREATE INDEX idx_agent_benchmark_results_run
            ON public.agent_benchmark_results (run_id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_indexes
                   WHERE schemaname = 'public' AND indexname = 'idx_agent_benchmark_results_item') THEN
        CREATE INDEX idx_agent_benchmark_results_item
            ON public.agent_benchmark_results (item_id, created_at DESC);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_indexes
                   WHERE schemaname = 'public' AND indexname = 'idx_agent_benchmark_runs_suite') THEN
        CREATE INDEX idx_agent_benchmark_runs_suite
            ON public.agent_benchmark_runs (suite_name, started_at DESC);
    END IF;
END
$gp$;
