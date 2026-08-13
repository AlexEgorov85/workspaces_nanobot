-- =====================================================================
-- DDL схемы для Greenplum 6.5
-- Сгенерировано автоматически по снятой схеме PostgreSQL 13
-- БЕЗ индексов, ключей и ограничений (по требованию).
-- DISTRIBUTED BY выбирается по типовым GP-конвенциям:
--   * id / *_id           -> DISTRIBUTED BY (id)
--   * session_key / run_id-> DISTRIBUTED BY (<col>)
--   * иначе               -> DISTRIBUTED RANDOMLY
--
-- Имя схемы задаётся одной переменной ниже (для psql: \i файл).
-- Изменить на нужное: \set schema_name 'ваша_схема'
-- =====================================================================

\set schema_name 'public'
SET search_path = :schema_name;

-- ---------------------------------------------------------------------
-- agent_benchmark_results: Результаты по каждому вопросу бенчмарка. Связаны с agent_benchmark_runs по run_id. Таблица агента (префикс agent_).
-- ---------------------------------------------------------------------
CREATE TABLE :schema_name.agent_benchmark_results (
    id UUID NOT NULL DEFAULT uuid_generate_v4(),
    run_id UUID NOT NULL,
    item_id TEXT NOT NULL,
    item_name TEXT NOT NULL,
    difficulty INT4 NOT NULL,
    category TEXT,
    item_type TEXT NOT NULL,
    passed BOOL NOT NULL DEFAULT false,
    score FLOAT4 NOT NULL DEFAULT 0.0,
    response TEXT,
    tools_used JSONB DEFAULT '[]'::jsonb,
    skills_activated JSONB DEFAULT '[]'::jsonb,
    total_iterations INT4 NOT NULL DEFAULT 0,
    duration_sec FLOAT4 NOT NULL DEFAULT 0.0,
    error TEXT,
    llm_judge_score FLOAT4,
    details JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
DISTRIBUTED BY (id);

COMMENT ON TABLE :schema_name.agent_benchmark_results IS 'Результаты по каждому вопросу бенчмарка. Связаны с agent_benchmark_runs по run_id. Таблица агента (префикс agent_).';
COMMENT ON COLUMN :schema_name.agent_benchmark_results.id IS 'PK результата (UUID).';
COMMENT ON COLUMN :schema_name.agent_benchmark_results.run_id IS 'FK на agent_benchmark_runs.id.';
COMMENT ON COLUMN :schema_name.agent_benchmark_results.item_id IS 'ID тестового вопроса.';
COMMENT ON COLUMN :schema_name.agent_benchmark_results.item_name IS 'Человекочитаемое имя вопроса.';
COMMENT ON COLUMN :schema_name.agent_benchmark_results.difficulty IS 'Сложность (1-5 или шкала suite).';
COMMENT ON COLUMN :schema_name.agent_benchmark_results.category IS 'Категория (sql/reasoning/...).';
COMMENT ON COLUMN :schema_name.agent_benchmark_results.item_type IS 'single (один шаг) | multi_step.';
COMMENT ON COLUMN :schema_name.agent_benchmark_results.passed IS 'True, если ответ прошёл проверку.';
COMMENT ON COLUMN :schema_name.agent_benchmark_results.score IS 'Оценка 0.0–1.0 (от автотеста).';
COMMENT ON COLUMN :schema_name.agent_benchmark_results.response IS 'Ответ агента (text).';
COMMENT ON COLUMN :schema_name.agent_benchmark_results.tools_used IS 'JSONB: список вызванных инструментов.';
COMMENT ON COLUMN :schema_name.agent_benchmark_results.skills_activated IS 'JSONB: список активированных навыков.';
COMMENT ON COLUMN :schema_name.agent_benchmark_results.total_iterations IS 'Количество итераций агента.';
COMMENT ON COLUMN :schema_name.agent_benchmark_results.duration_sec IS 'Длительность ответа, сек.';
COMMENT ON COLUMN :schema_name.agent_benchmark_results.error IS 'Текст ошибки (если была).';
COMMENT ON COLUMN :schema_name.agent_benchmark_results.llm_judge_score IS 'Оценка LLM-judge (если использовался).';
COMMENT ON COLUMN :schema_name.agent_benchmark_results.details IS 'JSONB: произвольные детали прогона.';
COMMENT ON COLUMN :schema_name.agent_benchmark_results.created_at IS 'Время создания записи.';

-- ---------------------------------------------------------------------
-- agent_benchmark_runs: Мета-информация о прогонах бенчмарков (один прогон = один набор тестов). Управляется benchmarks/db.py. Таблица агента (префикс agent_).
-- ---------------------------------------------------------------------
CREATE TABLE :schema_name.agent_benchmark_runs (
    id UUID NOT NULL DEFAULT uuid_generate_v4(),
    suite_name TEXT NOT NULL,
    suite_tags JSONB DEFAULT '[]'::jsonb,
    config JSONB DEFAULT '{}'::jsonb,
    total_items INT4 NOT NULL DEFAULT 0,
    passed_items INT4 NOT NULL DEFAULT 0,
    total_score FLOAT4 NOT NULL DEFAULT 0.0,
    avg_score FLOAT4 NOT NULL DEFAULT 0.0,
    duration_sec FLOAT4 NOT NULL DEFAULT 0.0,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    artifacts_dir TEXT
)
DISTRIBUTED BY (id);

COMMENT ON TABLE :schema_name.agent_benchmark_runs IS 'Мета-информация о прогонах бенчмарков (один прогон = один набор тестов). Управляется benchmarks/db.py. Таблица агента (префикс agent_).';
COMMENT ON COLUMN :schema_name.agent_benchmark_runs.id IS 'PK прогона (UUID).';
COMMENT ON COLUMN :schema_name.agent_benchmark_runs.suite_name IS 'Имя тестового набора.';
COMMENT ON COLUMN :schema_name.agent_benchmark_runs.suite_tags IS 'JSONB: теги набора (smoke/full/regression).';
COMMENT ON COLUMN :schema_name.agent_benchmark_runs.config IS 'JSONB: конфигурация прогона.';
COMMENT ON COLUMN :schema_name.agent_benchmark_runs.total_items IS 'Всего вопросов в прогоне.';
COMMENT ON COLUMN :schema_name.agent_benchmark_runs.passed_items IS 'Сколько вопросов прошло.';
COMMENT ON COLUMN :schema_name.agent_benchmark_runs.total_score IS 'Сумма score по всем вопросам.';
COMMENT ON COLUMN :schema_name.agent_benchmark_runs.avg_score IS 'Средний score по вопросам.';
COMMENT ON COLUMN :schema_name.agent_benchmark_runs.duration_sec IS 'Длительность прогона, сек.';
COMMENT ON COLUMN :schema_name.agent_benchmark_runs.started_at IS 'Время начала.';
COMMENT ON COLUMN :schema_name.agent_benchmark_runs.finished_at IS 'Время завершения (NULL пока идёт).';

-- ---------------------------------------------------------------------
-- agent_conversation_messages: Таблица обмена сообщениями канала PostgresChannel / Web-чата (Streamlit). Агент опрашивает входящие (status=pending), отвечает и пишет ответ обратно в эту же таблицу. Единотабличная схема (роль в role, рассуждения в metadata.reasoning). Таблица агента (префикс agent_).
-- ---------------------------------------------------------------------
CREATE TABLE :schema_name.agent_conversation_messages (
    id UUID NOT NULL DEFAULT uuid_generate_v4(),
    chat_id TEXT,
    user_id TEXT,
    direction VARCHAR(16) NOT NULL,
    channel VARCHAR(64),
    agent_id VARCHAR(256),
    parent_agent_id VARCHAR(256),
    parent_request_id VARCHAR(256),
    content TEXT,
    role VARCHAR(32),
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    request_id VARCHAR(256),
    retry_count INT4 NOT NULL DEFAULT 0,
    metadata JSONB DEFAULT '{}'::jsonb,
    media JSONB DEFAULT '[]'::jsonb,
    msg_timestamp TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
DISTRIBUTED BY (id);

COMMENT ON TABLE :schema_name.agent_conversation_messages IS 'Таблица обмена сообщениями канала PostgresChannel / Web-чата (Streamlit). Агент опрашивает входящие (status=pending), отвечает и пишет ответ обратно в эту же таблицу. Единотабличная схема (роль в role, рассуждения в metadata.reasoning). Таблица агента (префикс agent_).';
COMMENT ON COLUMN :schema_name.agent_conversation_messages.id IS 'PK сообщения (UUID).';
COMMENT ON COLUMN :schema_name.agent_conversation_messages.direction IS 'in | out (направление относительно агента).';
COMMENT ON COLUMN :schema_name.agent_conversation_messages.channel IS 'Имя канала (telegram/web/postgres_channel/...).';
COMMENT ON COLUMN :schema_name.agent_conversation_messages.role IS 'user | assistant | system | tool (для assistant-сообщений).';
COMMENT ON COLUMN :schema_name.agent_conversation_messages.status IS 'pending | processing | done | failed | cancelled.';
COMMENT ON COLUMN :schema_name.agent_conversation_messages.request_id IS 'ID запроса (корреляция с agent_question_runs).';
COMMENT ON COLUMN :schema_name.agent_conversation_messages.retry_count IS 'Сколько раз ретрайили обработку.';
COMMENT ON COLUMN :schema_name.agent_conversation_messages.metadata IS 'JSONB: tool_calls, reasoning, model, usage и т.п.';
COMMENT ON COLUMN :schema_name.agent_conversation_messages.media IS 'JSONB: список вложений (файлы, изображения).';
COMMENT ON COLUMN :schema_name.agent_conversation_messages.msg_timestamp IS 'Время сообщения (для сортировки).';

-- ---------------------------------------------------------------------
-- agent_gateway_logs: Структурированный журнал событий агента. Стройный: контекст вопроса в agent_question_runs (по request_id), здесь — только то, что относится к конкретному событию. Таблица агента (префикс agent_).
-- ---------------------------------------------------------------------
CREATE TABLE :schema_name.agent_gateway_logs (
    id VARCHAR(64) NOT NULL,
    request_id VARCHAR(256),
    session_id VARCHAR(256),
    channel VARCHAR(64),
    actor VARCHAR(32),
    event_type VARCHAR(64) NOT NULL,
    name VARCHAR(256),
    level VARCHAR(16) NOT NULL,
    summary TEXT,
    payload JSONB,
    metadata JSONB,
    "timestamp" TIMESTAMPTZ NOT NULL DEFAULT now()
)
DISTRIBUTED BY (id);

COMMENT ON TABLE :schema_name.agent_gateway_logs IS 'Структурированный журнал событий агента. Стройный: контекст вопроса в agent_question_runs (по request_id), здесь — только то, что относится к конкретному событию. Таблица агента (префикс agent_).';
COMMENT ON COLUMN :schema_name.agent_gateway_logs.id IS 'PK события (ulid/short id).';
COMMENT ON COLUMN :schema_name.agent_gateway_logs.request_id IS 'FK на agent_question_runs.request_id.';
COMMENT ON COLUMN :schema_name.agent_gateway_logs.session_id IS 'ID сессии (если применимо).';
COMMENT ON COLUMN :schema_name.agent_gateway_logs.channel IS 'Имя канала.';
COMMENT ON COLUMN :schema_name.agent_gateway_logs.actor IS 'user | assistant | system | tool.';
COMMENT ON COLUMN :schema_name.agent_gateway_logs.event_type IS 'message_in / message_out / tool_call / skill_loaded / ...';
COMMENT ON COLUMN :schema_name.agent_gateway_logs.level IS 'debug | info | warn | error.';
COMMENT ON COLUMN :schema_name.agent_gateway_logs.payload IS 'JSONB: основной payload события.';
COMMENT ON COLUMN :schema_name.agent_gateway_logs.metadata IS 'JSONB: дополнительные поля.';
COMMENT ON COLUMN :schema_name.agent_gateway_logs."timestamp" IS 'Время события.';

-- ---------------------------------------------------------------------
-- agent_predefined_scripts: Реестр предопределённых SQL-скриптов навыка audit_analyzer. Источник истины для режима --mode predefined. JSONB-колонка parameters повторяет структуру dataclass ParamDefinition: {param_name: {type, required, default, description, validation}}. Таблица агента (префикс agent_).
-- ---------------------------------------------------------------------
CREATE TABLE :schema_name.agent_predefined_scripts (
    id UUID NOT NULL DEFAULT uuid_generate_v4(),
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    sql_template TEXT NOT NULL,
    parameters JSONB NOT NULL DEFAULT '{}'::jsonb,
    max_rows_default INT4 NOT NULL,
    returns TEXT NOT NULL DEFAULT ''::text,
    long_description TEXT NOT NULL DEFAULT ''::text,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
DISTRIBUTED BY (id);

COMMENT ON TABLE :schema_name.agent_predefined_scripts IS 'Реестр предопределённых SQL-скриптов навыка audit_analyzer. Источник истины для режима --mode predefined. JSONB-колонка parameters повторяет структуру dataclass ParamDefinition: {param_name: {type, required, default, description, validation}}. Таблица агента (префикс agent_).';
COMMENT ON COLUMN :schema_name.agent_predefined_scripts.id IS 'PK скрипта (UUID).';
COMMENT ON COLUMN :schema_name.agent_predefined_scripts.name IS 'Уникальное имя скрипта (используется в --mode predefined --script name).';
COMMENT ON COLUMN :schema_name.agent_predefined_scripts.description IS 'Краткое описание.';
COMMENT ON COLUMN :schema_name.agent_predefined_scripts.sql_template IS 'Шаблон SQL с {{param}} плейсхолдерами.';
COMMENT ON COLUMN :schema_name.agent_predefined_scripts.parameters IS 'JSONB: описание параметров ParamDefinition.';
COMMENT ON COLUMN :schema_name.agent_predefined_scripts.max_rows_default IS 'Лимит строк по умолчанию.';
COMMENT ON COLUMN :schema_name.agent_predefined_scripts.returns IS 'Что возвращает скрипт (описание).';
COMMENT ON COLUMN :schema_name.agent_predefined_scripts.long_description IS 'Полное описание (markdown).';

-- ---------------------------------------------------------------------
-- agent_question_runs: Контекст вопроса/прогона: пользователь, агент, статус, вопрос/ответ, summary. Одна строка на request_id. Не дублируется на каждое событие лога. Полный текст вопроса/ответа в question/response, media — вложения. Таблица агента (префикс agent_).
-- ---------------------------------------------------------------------
CREATE TABLE :schema_name.agent_question_runs (
    request_id VARCHAR(256) NOT NULL,
    session_id VARCHAR(256),
    user_id VARCHAR(256),
    chat_id VARCHAR(256),
    channel VARCHAR(64),
    agent_id VARCHAR(256),
    parent_agent_id VARCHAR(256),
    parent_request_id VARCHAR(256),
    is_subagent BOOL NOT NULL DEFAULT false,
    status VARCHAR(32),
    summary TEXT,
    question TEXT,
    response TEXT,
    media JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
DISTRIBUTED BY (request_id);

COMMENT ON TABLE :schema_name.agent_question_runs IS 'Контекст вопроса/прогона: пользователь, агент, статус, вопрос/ответ, summary. Одна строка на request_id. Не дублируется на каждое событие лога. Полный текст вопроса/ответа в question/response, media — вложения. Таблица агента (префикс agent_).';
COMMENT ON COLUMN :schema_name.agent_question_runs.request_id IS 'PK запроса (UUID).';
COMMENT ON COLUMN :schema_name.agent_question_runs.session_id IS 'ID сессии (если в рамках сессии).';
COMMENT ON COLUMN :schema_name.agent_question_runs.user_id IS 'ID пользователя.';
COMMENT ON COLUMN :schema_name.agent_question_runs.chat_id IS 'ID чата.';
COMMENT ON COLUMN :schema_name.agent_question_runs.channel IS 'Канал.';
COMMENT ON COLUMN :schema_name.agent_question_runs.agent_id IS 'ID агента, который обработал запрос.';
COMMENT ON COLUMN :schema_name.agent_question_runs.parent_agent_id IS 'ID родительского агента (для subagent).';
COMMENT ON COLUMN :schema_name.agent_question_runs.parent_request_id IS 'request_id родительского запроса.';
COMMENT ON COLUMN :schema_name.agent_question_runs.is_subagent IS 'True, если запущен как subagent.';
COMMENT ON COLUMN :schema_name.agent_question_runs.status IS 'pending | running | done | failed | cancelled.';
COMMENT ON COLUMN :schema_name.agent_question_runs.summary IS 'Краткое summary результата.';
COMMENT ON COLUMN :schema_name.agent_question_runs.question IS 'Полный текст вопроса.';
COMMENT ON COLUMN :schema_name.agent_question_runs.response IS 'Полный текст ответа.';
COMMENT ON COLUMN :schema_name.agent_question_runs.media IS 'JSONB: вложения.';

-- ---------------------------------------------------------------------
-- agent_session_messages: Сообщения чата в рамках сессии (append-only по session_key+seq). Таблица агента (префикс agent_).
-- ---------------------------------------------------------------------
CREATE TABLE :schema_name.agent_session_messages (
    id UUID NOT NULL DEFAULT uuid_generate_v4(),
    session_key VARCHAR(256) NOT NULL,
    seq INT4 NOT NULL,
    role VARCHAR(32) NOT NULL,
    content TEXT,
    tool_calls JSONB,
    tool_call_id VARCHAR(128),
    name VARCHAR(256),
    reasoning_content TEXT,
    thinking_blocks JSONB,
    media JSONB,
    cli_apps JSONB,
    mcp_presets JSONB,
    injected_event JSONB,
    _command TEXT,
    _channel_delivery JSONB,
    metadata JSONB,
    msg_timestamp TIMESTAMPTZ NOT NULL DEFAULT now()
)
DISTRIBUTED BY (id);

COMMENT ON TABLE :schema_name.agent_session_messages IS 'Сообщения чата в рамках сессии (append-only по session_key+seq). Таблица агента (префикс agent_).';
COMMENT ON COLUMN :schema_name.agent_session_messages.id IS 'PK сообщения (UUID).';
COMMENT ON COLUMN :schema_name.agent_session_messages.session_key IS 'Ключ сессии (FK на agent_session_meta.session_id).';
COMMENT ON COLUMN :schema_name.agent_session_messages.seq IS 'Порядковый номер сообщения в сессии.';
COMMENT ON COLUMN :schema_name.agent_session_messages.role IS 'user | assistant | system | tool.';
COMMENT ON COLUMN :schema_name.agent_session_messages.tool_calls IS 'JSONB: список tool_calls (для assistant).';
COMMENT ON COLUMN :schema_name.agent_session_messages.tool_call_id IS 'ID tool_call (для role=tool).';
COMMENT ON COLUMN :schema_name.agent_session_messages.reasoning_content IS 'Текст рассуждений модели.';
COMMENT ON COLUMN :schema_name.agent_session_messages.thinking_blocks IS 'JSONB: thinking_blocks (structured).';
COMMENT ON COLUMN :schema_name.agent_session_messages.media IS 'JSONB: вложения.';
COMMENT ON COLUMN :schema_name.agent_session_messages.cli_apps IS 'JSONB: зарегистрированные CLI-приложения.';
COMMENT ON COLUMN :schema_name.agent_session_messages.mcp_presets IS 'JSONB: активные MCP-пресеты.';
COMMENT ON COLUMN :schema_name.agent_session_messages.injected_event IS 'JSONB: событие, инжектированное в сообщение.';
COMMENT ON COLUMN :schema_name.agent_session_messages._command IS 'Сырая команда из канала.';
COMMENT ON COLUMN :schema_name.agent_session_messages._channel_delivery IS 'JSONB: доставка в канал.';
COMMENT ON COLUMN :schema_name.agent_session_messages.metadata IS 'JSONB: дополнительные поля.';

-- ---------------------------------------------------------------------
-- agent_session_meta: Метаданные сессий nanobot. Заменяет JSONL-файлы в workspace/sessions/. Управляется PGSessionManager (lib/session/pg_session_manager.py). Таблица агента (префикс agent_).
-- ---------------------------------------------------------------------
CREATE TABLE :schema_name.agent_session_meta (
    session_id VARCHAR(256) NOT NULL,
    user_id VARCHAR(256),
    chat_id VARCHAR(256),
    channel VARCHAR(64),
    agent_id VARCHAR(256),
    parent_session_id VARCHAR(256),
    metadata JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
DISTRIBUTED BY (session_id);

COMMENT ON TABLE :schema_name.agent_session_meta IS 'Метаданные сессий nanobot. Заменяет JSONL-файлы в workspace/sessions/. Управляется PGSessionManager (lib/session/pg_session_manager.py). Таблица агента (префикс agent_).';
COMMENT ON COLUMN :schema_name.agent_session_meta.session_id IS 'PK сессии.';
COMMENT ON COLUMN :schema_name.agent_session_meta.user_id IS 'ID пользователя.';
COMMENT ON COLUMN :schema_name.agent_session_meta.chat_id IS 'ID чата.';
COMMENT ON COLUMN :schema_name.agent_session_meta.channel IS 'Канал.';
COMMENT ON COLUMN :schema_name.agent_session_meta.agent_id IS 'ID агента.';
COMMENT ON COLUMN :schema_name.agent_session_meta.parent_session_id IS 'ID родительской сессии.';
COMMENT ON COLUMN :schema_name.agent_session_meta.metadata IS 'JSONB: произвольные метаданные.';

-- ---------------------------------------------------------------------
-- agent_vector_index_config: Конфигурация сборки векторных индексов (audit_analyzer). Таблица агента (префикс agent_).
-- ---------------------------------------------------------------------
CREATE TABLE :schema_name.agent_vector_index_config (
    index_name VARCHAR(128) NOT NULL,
    source_table VARCHAR(128),
    src_table VARCHAR(128),
    pk_column VARCHAR(128),
    content_cols TEXT,
    embedding_cols TEXT,
    track_column VARCHAR(128),
    enabled BOOL NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
DISTRIBUTED BY (index_name);

COMMENT ON TABLE :schema_name.agent_vector_index_config IS 'Конфигурация сборки векторных индексов (audit_analyzer). Таблица агента (префикс agent_).';
COMMENT ON COLUMN :schema_name.agent_vector_index_config.index_name IS 'PK индекса (имя FAISS-индекса).';
COMMENT ON COLUMN :schema_name.agent_vector_index_config.source_table IS 'Логическое имя источника (например, "audits").';
COMMENT ON COLUMN :schema_name.agent_vector_index_config.src_table IS 'Физическая таблица-источник (например, "oarb.audits").';
COMMENT ON COLUMN :schema_name.agent_vector_index_config.pk_column IS 'PK-колонка источника.';
COMMENT ON COLUMN :schema_name.agent_vector_index_config.content_cols IS 'Колонки, по которым строится контент (JSONB/TEXT).';
COMMENT ON COLUMN :schema_name.agent_vector_index_config.embedding_cols IS 'Колонки, которые попадают в эмбеддинг (JSONB/TEXT).';
COMMENT ON COLUMN :schema_name.agent_vector_index_config.track_column IS 'Колонка для отслеживания изменений (например, updated_at).';
COMMENT ON COLUMN :schema_name.agent_vector_index_config.enabled IS 'Включён ли индекс для сборки.';

-- ---------------------------------------------------------------------
-- agent_vector_index_store: Сериализованные FAISS-индексы (binary blob + metadata). Загружаются lib.services.cache_provider_impl при search_vector. Таблица агента (префикс agent_).
-- ---------------------------------------------------------------------
CREATE TABLE :schema_name.agent_vector_index_store (
    source VARCHAR(128) NOT NULL,
    index_binary BYTEA,
    dimension INT4,
    vector_count INT4,
    metadata JSONB,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
DISTRIBUTED BY (source);

COMMENT ON TABLE :schema_name.agent_vector_index_store IS 'Сериализованные FAISS-индексы (binary blob + metadata). Загружаются lib.services.cache_provider_impl при search_vector. Таблица агента (префикс agent_).';
COMMENT ON COLUMN :schema_name.agent_vector_index_store.source IS 'PK — имя индекса (= agent_vector_index_config.index_name).';
COMMENT ON COLUMN :schema_name.agent_vector_index_store.index_binary IS 'Сериализованный FAISS-индекс (BYTEA).';
COMMENT ON COLUMN :schema_name.agent_vector_index_store.dimension IS 'Размерность векторов.';
COMMENT ON COLUMN :schema_name.agent_vector_index_store.vector_count IS 'Количество векторов в индексе.';
COMMENT ON COLUMN :schema_name.agent_vector_index_store.metadata IS 'JSONB: метаданные индекса.';
