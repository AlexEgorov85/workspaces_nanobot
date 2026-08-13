-- ============================================================================
-- Таблицы для хранения результатов бенчмарков.
-- Префикс agent_ — таблицы, которыми владеет сам агент (не навык).
-- Схема: public (или переопределяется в коде)
-- Управляется: benchmarks/db.py
-- ============================================================================

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
  IF EXISTS (SELECT 1 FROM pg_indexes
             WHERE schemaname = 'public' AND indexname = 'idx_benchmark_runs_suite') THEN
    ALTER INDEX public.idx_benchmark_runs_suite RENAME TO idx_agent_benchmark_runs_suite;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_indexes
             WHERE schemaname = 'public' AND indexname = 'idx_benchmark_results_run') THEN
    ALTER INDEX public.idx_benchmark_results_run RENAME TO idx_agent_benchmark_results_run;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_indexes
             WHERE schemaname = 'public' AND indexname = 'idx_benchmark_results_item') THEN
    ALTER INDEX public.idx_benchmark_results_item RENAME TO idx_agent_benchmark_results_item;
  END IF;
END $$;

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ---- agent_benchmark_runs: мета-информация о прогонах ----

CREATE TABLE IF NOT EXISTS public.agent_benchmark_runs (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
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
);

COMMENT ON TABLE public.agent_benchmark_runs IS
    'Мета-информация о прогонах бенчмарков (один прогон = один набор тестов). '
    'Управляется benchmarks/db.py. Таблица агента (префикс agent_).';
COMMENT ON COLUMN public.agent_benchmark_runs.id IS 'PK прогона (UUID).';
COMMENT ON COLUMN public.agent_benchmark_runs.suite_name IS 'Имя тестового набора.';
COMMENT ON COLUMN public.agent_benchmark_runs.suite_tags IS 'JSONB: теги набора (smoke/full/regression).';
COMMENT ON COLUMN public.agent_benchmark_runs.config IS 'JSONB: конфигурация прогона.';
COMMENT ON COLUMN public.agent_benchmark_runs.total_items IS 'Всего вопросов в прогоне.';
COMMENT ON COLUMN public.agent_benchmark_runs.passed_items IS 'Сколько вопросов прошло.';
COMMENT ON COLUMN public.agent_benchmark_runs.total_score IS 'Сумма score по всем вопросам.';
COMMENT ON COLUMN public.agent_benchmark_runs.avg_score IS 'Средний score по вопросам.';
COMMENT ON COLUMN public.agent_benchmark_runs.duration_sec IS 'Длительность прогона, сек.';
COMMENT ON COLUMN public.agent_benchmark_runs.started_at IS 'Время начала.';
COMMENT ON COLUMN public.agent_benchmark_runs.finished_at IS 'Время завершения (NULL пока идёт).';

-- ---- agent_benchmark_results: результаты по каждому вопросу ----

CREATE TABLE IF NOT EXISTS public.agent_benchmark_results (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
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
);

COMMENT ON TABLE public.agent_benchmark_results IS
    'Результаты по каждому вопросу бенчмарка. '
    'Связаны с agent_benchmark_runs по run_id. '
    'Таблица агента (префикс agent_).';
COMMENT ON COLUMN public.agent_benchmark_results.id IS 'PK результата (UUID).';
COMMENT ON COLUMN public.agent_benchmark_results.run_id IS 'FK на agent_benchmark_runs.id.';
COMMENT ON COLUMN public.agent_benchmark_results.item_id IS 'ID тестового вопроса.';
COMMENT ON COLUMN public.agent_benchmark_results.item_name IS 'Человекочитаемое имя вопроса.';
COMMENT ON COLUMN public.agent_benchmark_results.difficulty IS 'Сложность (1-5 или шкала suite).';
COMMENT ON COLUMN public.agent_benchmark_results.category IS 'Категория (sql/reasoning/...).';
COMMENT ON COLUMN public.agent_benchmark_results.item_type IS 'single (один шаг) | multi_step.';
COMMENT ON COLUMN public.agent_benchmark_results.passed IS 'True, если ответ прошёл проверку.';
COMMENT ON COLUMN public.agent_benchmark_results.score IS 'Оценка 0.0–1.0 (от автотеста).';
COMMENT ON COLUMN public.agent_benchmark_results.response IS 'Ответ агента (text).';
COMMENT ON COLUMN public.agent_benchmark_results.tools_used IS 'JSONB: список вызванных инструментов.';
COMMENT ON COLUMN public.agent_benchmark_results.skills_activated IS 'JSONB: список активированных навыков.';
COMMENT ON COLUMN public.agent_benchmark_results.total_iterations IS 'Количество итераций агента.';
COMMENT ON COLUMN public.agent_benchmark_results.duration_sec IS 'Длительность ответа, сек.';
COMMENT ON COLUMN public.agent_benchmark_results.error IS 'Текст ошибки (если была).';
COMMENT ON COLUMN public.agent_benchmark_results.llm_judge_score IS 'Оценка LLM-judge (если использовался).';
COMMENT ON COLUMN public.agent_benchmark_results.details IS 'JSONB: произвольные детали прогона.';
COMMENT ON COLUMN public.agent_benchmark_results.created_at IS 'Время создания записи.';

-- Индексы
CREATE INDEX IF NOT EXISTS idx_agent_benchmark_results_run
    ON public.agent_benchmark_results (run_id);

CREATE INDEX IF NOT EXISTS idx_agent_benchmark_results_item
    ON public.agent_benchmark_results (item_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_agent_benchmark_runs_suite
    ON public.agent_benchmark_runs (suite_name, started_at DESC);
