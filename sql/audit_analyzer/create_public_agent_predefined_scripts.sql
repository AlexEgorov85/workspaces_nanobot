-- ============================================================================
-- public.agent_predefined_scripts — реестр предопределённых SQL-скриптов
-- Источник истины для --mode predefined навыка audit_analyzer.
-- JSONB-колонка parameters повторяет структуру dataclass ParamDefinition:
-- {param_name: {type, required, default, description, validation}}.
-- Копируется в DuckDB-кэш через db_additional_tables и читается через
-- db_loader.load_registry().
-- Совместимость: Greenplum 6.5.
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.agent_predefined_scripts (
    name             TEXT NOT NULL,
    description      TEXT NOT NULL,
    sql_template     TEXT NOT NULL,
    parameters       JSONB NOT NULL DEFAULT '{}'::jsonb,
    max_rows_default INTEGER NOT NULL,
    returns          TEXT NOT NULL DEFAULT '',
    long_description TEXT NOT NULL DEFAULT '',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (name)
)
DISTRIBUTED RANDOMLY;

COMMENT ON TABLE  public.agent_predefined_scripts IS 'Реестр предопределённых SQL-скриптов навыка audit_analyzer. Источник истины для режима --mode predefined.';
COMMENT ON COLUMN public.agent_predefined_scripts.name             IS 'PK — уникальное имя скрипта. Используется в CLI: --script <name>. Должно быть валидным идентификатором (^[a-z][a-z0-9_]*$).';
COMMENT ON COLUMN public.agent_predefined_scripts.description      IS 'Краткое описание для меню/подсказок (1-2 строки).';
COMMENT ON COLUMN public.agent_predefined_scripts.sql_template     IS 'SQL-шаблон с позиционными ?-плейсхолдерами (DuckDB-стиль). Каждый ? соответствует параметру из JSONB parameters в порядке объявления. Реализация: PredefinedScriptRequestBuilder в lib/services/predefined_script_request.py.';
COMMENT ON COLUMN public.agent_predefined_scripts.parameters       IS 'JSONB: {param_name: ParamDefinition} — type/required/default/description/validation.';
COMMENT ON COLUMN public.agent_predefined_scripts.max_rows_default IS 'Лимит строк по умолчанию (добавляется в LIMIT).';
COMMENT ON COLUMN public.agent_predefined_scripts.returns          IS 'Что возвращает скрипт (для документации и LLM-промпта).';
COMMENT ON COLUMN public.agent_predefined_scripts.long_description IS 'Подробное описание для LLM-промпта: что делает, когда использовать, edge cases.';
COMMENT ON COLUMN public.agent_predefined_scripts.created_at       IS 'Время создания записи.';
COMMENT ON COLUMN public.agent_predefined_scripts.updated_at       IS 'Время последнего изменения.';
