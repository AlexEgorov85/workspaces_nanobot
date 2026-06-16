-- Greenplum 6.25: create_benchmark_tables.sql + DISTRIBUTED BY, pgcrypto, без FK

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- Прогоны бенчмарков (мета-информация)
CREATE TABLE IF NOT EXISTS public.benchmark_runs (
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

-- Результаты по каждому вопросу бенчмарка
CREATE TABLE IF NOT EXISTS public.benchmark_results (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id          UUID NOT NULL,
    item_id         TEXT NOT NULL,
    item_name       TEXT NOT NULL,
    difficulty      INT NOT NULL,
    category        TEXT,
    item_type       TEXT NOT NULL,                          -- single | multi_step
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

-- Индексы
CREATE INDEX IF NOT EXISTS idx_benchmark_results_run
    ON public.benchmark_results (run_id);

CREATE INDEX IF NOT EXISTS idx_benchmark_results_item
    ON public.benchmark_results (item_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_benchmark_runs_suite
    ON public.benchmark_runs (suite_name, created_at DESC);
