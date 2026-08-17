-- ============================================================================
-- public.agent_benchmark_results — результаты по каждому вопросу бенчмарка
-- Связаны с agent_benchmark_runs по run_id.
-- Распределены по run_id для co-located JOIN.
-- Управляется: benchmarks/db.py.
-- Совместимость: Greenplum 6.5.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS public.agent_benchmark_results (
    id               UUID NOT NULL DEFAULT gen_random_uuid(),
    run_id           UUID NOT NULL,
    item_id          TEXT NOT NULL,
    item_name        TEXT NOT NULL,
    difficulty       INT NOT NULL,
    category         TEXT,
    item_type        TEXT NOT NULL,
    passed           BOOLEAN NOT NULL DEFAULT FALSE,
    score            REAL NOT NULL DEFAULT 0.0,
    response         TEXT,
    tools_used       JSONB DEFAULT '[]'::jsonb,
    skills_activated JSONB DEFAULT '[]'::jsonb,
    total_iterations INT NOT NULL DEFAULT 0,
    duration_sec     REAL NOT NULL DEFAULT 0.0,
    error            TEXT,
    llm_judge_score  REAL,
    details          JSONB DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id)
)
DISTRIBUTED BY (run_id);

COMMENT ON TABLE  public.agent_benchmark_results IS 'Результаты по каждому вопросу бенчмарка. Связаны с agent_benchmark_runs по run_id.';
COMMENT ON COLUMN public.agent_benchmark_results.id               IS 'PK результата (UUID).';
COMMENT ON COLUMN public.agent_benchmark_results.run_id           IS 'FK-логически на agent_benchmark_runs.id.';
COMMENT ON COLUMN public.agent_benchmark_results.item_id          IS 'ID тестового вопроса.';
COMMENT ON COLUMN public.agent_benchmark_results.item_name        IS 'Человекочитаемое имя вопроса.';
COMMENT ON COLUMN public.agent_benchmark_results.difficulty       IS 'Сложность (1-5 или шкала suite).';
COMMENT ON COLUMN public.agent_benchmark_results.category         IS 'Категория (sql/reasoning/...).';
COMMENT ON COLUMN public.agent_benchmark_results.item_type        IS 'single (один шаг) | multi_step.';
COMMENT ON COLUMN public.agent_benchmark_results.passed           IS 'True, если ответ прошёл проверку.';
COMMENT ON COLUMN public.agent_benchmark_results.score            IS 'Оценка 0.0–1.0 (от автотеста).';
COMMENT ON COLUMN public.agent_benchmark_results.response         IS 'Ответ агента (text).';
COMMENT ON COLUMN public.agent_benchmark_results.tools_used       IS 'JSONB: список вызванных инструментов.';
COMMENT ON COLUMN public.agent_benchmark_results.skills_activated IS 'JSONB: список активированных навыков.';
COMMENT ON COLUMN public.agent_benchmark_results.total_iterations IS 'Количество итераций агента.';
COMMENT ON COLUMN public.agent_benchmark_results.duration_sec     IS 'Длительность ответа, сек.';
COMMENT ON COLUMN public.agent_benchmark_results.error            IS 'Текст ошибки (если была).';
COMMENT ON COLUMN public.agent_benchmark_results.llm_judge_score  IS 'Оценка LLM-judge (если использовался).';
COMMENT ON COLUMN public.agent_benchmark_results.details          IS 'JSONB: произвольные детали прогона.';
COMMENT ON COLUMN public.agent_benchmark_results.created_at       IS 'Время создания записи.';
