-- Idempotent COMMENT apply (skips missing tables)
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema = 'oarb' AND table_name = 'audit_reports') THEN
    EXECUTE 'COMMENT ON TABLE oarb.audit_reports IS ''Акты аудиторской проверки (оформленные документы по результатам)''';
    EXECUTE 'COMMENT ON COLUMN oarb.audit_reports."id" IS ''Уникальный идентификатор акта''';
    EXECUTE 'COMMENT ON COLUMN oarb.audit_reports."audit_id" IS ''Ссылка на проверку, к которой относится акт''';
    EXECUTE 'COMMENT ON COLUMN oarb.audit_reports."report_number" IS ''Номер акта (внутренняя нумерация)''';
    EXECUTE 'COMMENT ON COLUMN oarb.audit_reports."report_date" IS ''Дата составления акта''';
    EXECUTE 'COMMENT ON COLUMN oarb.audit_reports."title" IS ''Название акта / заголовок документа''';
    EXECUTE 'COMMENT ON COLUMN oarb.audit_reports."full_text" IS ''Полный текст акта (если не используется разбивка по пунктам)''';
    EXECUTE 'COMMENT ON COLUMN oarb.audit_reports."created_at" IS ''Дата и время создания записи''';
    EXECUTE 'COMMENT ON COLUMN oarb.audit_reports."updated_at" IS ''Дата и время последнего обновления''';
  END IF;
END $$;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema = 'oarb' AND table_name = 'audits') THEN
    EXECUTE 'COMMENT ON TABLE oarb.audits IS ''Проверки (плановые и внеплановые аудиторские мероприятия)''';
    EXECUTE 'COMMENT ON COLUMN oarb.audits."id" IS ''Уникальный идентификатор проверки''';
    EXECUTE 'COMMENT ON COLUMN oarb.audits."title" IS ''Наименование / тема проверки''';
    EXECUTE 'COMMENT ON COLUMN oarb.audits."audit_type" IS ''Тип проверки (плановая, внеплановая и т.д.)''';
    EXECUTE 'COMMENT ON COLUMN oarb.audits."planned_date" IS ''Плановая дата проведения проверки''';
    EXECUTE 'COMMENT ON COLUMN oarb.audits."actual_date" IS ''Фактическая дата завершения проверки''';
    EXECUTE 'COMMENT ON COLUMN oarb.audits."status" IS ''Текущий статус проверки''';
    EXECUTE 'COMMENT ON COLUMN oarb.audits."auditee_entity" IS ''Проверяемый объект (подразделение, компания, филиал)''';
    EXECUTE 'COMMENT ON COLUMN oarb.audits."created_at" IS ''Дата и время создания записи''';
    EXECUTE 'COMMENT ON COLUMN oarb.audits."updated_at" IS ''Дата и время последнего обновления''';
  END IF;
END $$;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema = 'oarb' AND table_name = 'report_items') THEN
    EXECUTE 'COMMENT ON TABLE oarb.report_items IS ''Пункты (разделы) акта аудиторской проверки с текстовым наполнением''';
    EXECUTE 'COMMENT ON COLUMN oarb.report_items."id" IS ''Уникальный идентификатор пункта акта''';
    EXECUTE 'COMMENT ON COLUMN oarb.report_items."report_id" IS ''Ссылка на акт, которому принадлежит пункт''';
    EXECUTE 'COMMENT ON COLUMN oarb.report_items."item_number" IS ''Номер пункта (может содержать буквы, цифры, иерархию)''';
    EXECUTE 'COMMENT ON COLUMN oarb.report_items."item_title" IS ''Название пункта (заголовок раздела)''';
    EXECUTE 'COMMENT ON COLUMN oarb.report_items."item_content" IS ''Текст содержания пункта''';
    EXECUTE 'COMMENT ON COLUMN oarb.report_items."order_index" IS ''Порядковый индекс для ручной сортировки''';
    EXECUTE 'COMMENT ON COLUMN oarb.report_items."created_at" IS ''Дата и время создания записи''';
    EXECUTE 'COMMENT ON COLUMN oarb.report_items."updated_at" IS ''Дата и время последнего обновления''';
  END IF;
END $$;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema = 'oarb' AND table_name = 'violations') THEN
    EXECUTE 'COMMENT ON TABLE oarb.violations IS ''Отклонения (нарушения, проблемы), выявленные в ходе аудита''';
    EXECUTE 'COMMENT ON COLUMN oarb.violations."id" IS ''Уникальный идентификатор отклонения''';
    EXECUTE 'COMMENT ON COLUMN oarb.violations."audit_id" IS ''Ссылка на проверку, в ходе которой выявлено отклонение''';
    EXECUTE 'COMMENT ON COLUMN oarb.violations."report_id" IS ''Ссылка на акт, в котором зафиксировано отклонение''';
    EXECUTE 'COMMENT ON COLUMN oarb.violations."item_id" IS ''Ссылка на конкретный пункт акта, к которому относится отклонение''';
    EXECUTE 'COMMENT ON COLUMN oarb.violations."violation_code" IS ''Код нарушения по внутреннему классификатору (если используется)''';
    EXECUTE 'COMMENT ON COLUMN oarb.violations."description" IS ''Подробное описание сути отклонения''';
    EXECUTE 'COMMENT ON COLUMN oarb.violations."recommendation" IS ''Рекомендации аудитора по устранению нарушения''';
    EXECUTE 'COMMENT ON COLUMN oarb.violations."severity" IS ''Критичность нарушения (Низкая, Средняя, Высокая)''';
    EXECUTE 'COMMENT ON COLUMN oarb.violations."status" IS ''Текущий статус работы с нарушением''';
    EXECUTE 'COMMENT ON COLUMN oarb.violations."responsible" IS ''Ответственное лицо или подразделение за устранение''';
    EXECUTE 'COMMENT ON COLUMN oarb.violations."deadline" IS ''Плановая дата устранения нарушения''';
    EXECUTE 'COMMENT ON COLUMN oarb.violations."created_at" IS ''Дата и время фиксации отклонения в системе''';
    EXECUTE 'COMMENT ON COLUMN oarb.violations."updated_at" IS ''Дата и время последнего изменения записи''';
  END IF;
END $$;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema = 'public' AND table_name = 'predefined_scripts') THEN
    EXECUTE 'COMMENT ON TABLE public.predefined_scripts IS ''Реестр предопределённых SQL-скриптов навыка audit_analyzer. Источник истины для режима --mode predefined. JSONB-колонка parameters повторяет структуру dataclass ParamDefinition: {param_name: {type, required, default, description, validation}}.''';
    EXECUTE 'COMMENT ON COLUMN public.predefined_scripts."name" IS ''Уникальное имя скрипта (используется в CLI: --script <name>).''';
    EXECUTE 'COMMENT ON COLUMN public.predefined_scripts."description" IS ''Краткое описание для меню/подсказок.''';
    EXECUTE 'COMMENT ON COLUMN public.predefined_scripts."sql_template" IS ''SQL-шаблон с Jinja2-подобными блоками {% if param %} и :param_name плейсхолдерами.''';
    EXECUTE 'COMMENT ON COLUMN public.predefined_scripts."parameters" IS ''Параметры скрипта как JSONB: {param_name: ParamDefinition}.''';
    EXECUTE 'COMMENT ON COLUMN public.predefined_scripts."max_rows_default" IS ''Лимит строк по умолчанию (добавляется в LIMIT).''';
    EXECUTE 'COMMENT ON COLUMN public.predefined_scripts."returns" IS ''Что возвращает скрипт (для документации).''';
    EXECUTE 'COMMENT ON COLUMN public.predefined_scripts."long_description" IS ''Подробное описание для LLM-промпта.''';
    EXECUTE 'COMMENT ON COLUMN public.predefined_scripts."created_at" IS ''Время создания записи.''';
    EXECUTE 'COMMENT ON COLUMN public.predefined_scripts."updated_at" IS ''Время последнего изменения (обновляется триггером).''';
  END IF;
END $$;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema = 'oarb' AND table_name = 'audit_vectors') THEN
    EXECUTE 'COMMENT ON TABLE oarb.audit_vectors IS ''Векторные эмбеддинги для семантического поиска audit_analyzer.''';
    EXECUTE 'COMMENT ON COLUMN oarb.audit_vectors."id" IS ''PK эмбеддинга (BIGINT IDENTITY).''';
    EXECUTE 'COMMENT ON COLUMN oarb.audit_vectors."source" IS ''Имя индекса (= vector_index_config.index_name).''';
    EXECUTE 'COMMENT ON COLUMN oarb.audit_vectors."content" IS ''Текст для отображения.''';
    EXECUTE 'COMMENT ON COLUMN oarb.audit_vectors."search_text" IS ''Текст по которому строился эмбеддинг.''';
    EXECUTE 'COMMENT ON COLUMN oarb.audit_vectors."table" IS ''Короткое имя исходной таблицы.''';
    EXECUTE 'COMMENT ON COLUMN oarb.audit_vectors."pk_value" IS ''PK исходной строки (TEXT для совместимости с UUID/BIGINT/INTEGER).''';
    EXECUTE 'COMMENT ON COLUMN oarb.audit_vectors."chunk_index" IS ''Номер чанка (если строка длинная).''';
    EXECUTE 'COMMENT ON COLUMN oarb.audit_vectors."chunk_count" IS ''Общее количество чанков строки.''';
    EXECUTE 'COMMENT ON COLUMN oarb.audit_vectors."row_data" IS ''Полная строка исходных данных (JSONB).''';
    EXECUTE 'COMMENT ON COLUMN oarb.audit_vectors."embedding" IS ''Векторный эмбеддинг float32 (REAL[]).''';
    EXECUTE 'COMMENT ON COLUMN oarb.audit_vectors."content_hash" IS ''MD5 от search_text (для инкрементальных обновлений).''';
    EXECUTE 'COMMENT ON COLUMN oarb.audit_vectors."max_src_track" IS ''MAX(track_column) в источнике.''';
    EXECUTE 'COMMENT ON COLUMN oarb.audit_vectors."synced_at" IS ''Время последней синхронизации.''';
    EXECUTE 'COMMENT ON COLUMN oarb.audit_vectors."created_at" IS ''Время создания записи в этой таблице.''';
  END IF;
END $$;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema = 'oarb' AND table_name = 'vector_index_config') THEN
    EXECUTE 'COMMENT ON TABLE oarb.vector_index_config IS ''Конфигурация сборки векторных индексов (audit_analyzer).''';
    EXECUTE 'COMMENT ON COLUMN oarb.vector_index_config."index_name" IS ''Уникальное имя индекса.''';
    EXECUTE 'COMMENT ON COLUMN oarb.vector_index_config."source_table" IS ''Короткое имя для колонки source в audit_vectors.''';
    EXECUTE 'COMMENT ON COLUMN oarb.vector_index_config."src_table" IS ''Исходная таблица (schema.table).''';
    EXECUTE 'COMMENT ON COLUMN oarb.vector_index_config."pk_column" IS ''Колонка первичного ключа в исходной таблице.''';
    EXECUTE 'COMMENT ON COLUMN oarb.vector_index_config."content_cols" IS ''Колонки для content (отображение).''';
    EXECUTE 'COMMENT ON COLUMN oarb.vector_index_config."embedding_cols" IS ''Колонки для эмбеддинга (JSONB).''';
    EXECUTE 'COMMENT ON COLUMN oarb.vector_index_config."track_column" IS ''Колонка для ORDER BY (track изменений).''';
    EXECUTE 'COMMENT ON COLUMN oarb.vector_index_config."enabled" IS ''Индекс активен.''';
    EXECUTE 'COMMENT ON COLUMN oarb.vector_index_config."created_at" IS ''Время создания записи.''';
    EXECUTE 'COMMENT ON COLUMN oarb.vector_index_config."updated_at" IS ''Время последнего изменения.''';
  END IF;
END $$;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema = 'oarb' AND table_name = 'vector_index_store') THEN
    EXECUTE 'COMMENT ON TABLE oarb.vector_index_store IS ''Сериализованные FAISS-индексы (binary blob + metadata). Загружаются lib.services.cache_provider_impl при search_vector.''';
    EXECUTE 'COMMENT ON COLUMN oarb.vector_index_store."source" IS ''PK — имя индекса (= index_name из vector_index_config).''';
    EXECUTE 'COMMENT ON COLUMN oarb.vector_index_store."index_binary" IS ''Сериализованный FAISS-индекс (pickle/bytes).''';
    EXECUTE 'COMMENT ON COLUMN oarb.vector_index_store."metadata" IS ''Метаданные индекса (id↔source↔pk_value mapping).''';
    EXECUTE 'COMMENT ON COLUMN oarb.vector_index_store."dimension" IS ''Размерность векторов в индексе.''';
    EXECUTE 'COMMENT ON COLUMN oarb.vector_index_store."vector_count" IS ''Количество векторов в индексе.''';
    EXECUTE 'COMMENT ON COLUMN oarb.vector_index_store."updated_at" IS ''Время последней пересборки индекса.''';
  END IF;
END $$;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema = 'public' AND table_name = 'session_meta') THEN
    EXECUTE 'COMMENT ON TABLE public.session_meta IS ''Метаданные сессий nanobot. Заменяет JSONL-файлы в workspace/sessions/. Управляется PGSessionManager (lib/session/pg_session_manager.py).''';
    EXECUTE 'COMMENT ON COLUMN public.session_meta."session_key" IS ''PK — уникальный ключ сессии (например, "telegram:12345").''';
    EXECUTE 'COMMENT ON COLUMN public.session_meta."created_at" IS ''Время создания сессии.''';
    EXECUTE 'COMMENT ON COLUMN public.session_meta."updated_at" IS ''Время последнего изменения.''';
    EXECUTE 'COMMENT ON COLUMN public.session_meta."last_consolidated" IS ''Последний seq, до которого сообщения консолидированы.''';
    EXECUTE 'COMMENT ON COLUMN public.session_meta."metadata" IS ''Произвольные метаданные сессии (user_id, channel, ...).''';
  END IF;
END $$;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema = 'public' AND table_name = 'session_messages') THEN
    EXECUTE 'COMMENT ON TABLE public.session_messages IS ''Сообщения чата в рамках сессии (append-only по session_key+seq).''';
    EXECUTE 'COMMENT ON COLUMN public.session_messages."id" IS ''PK сообщения.''';
    EXECUTE 'COMMENT ON COLUMN public.session_messages."session_key" IS ''FK-логически на session_meta.session_key (FK не объявлено для GP).''';
    EXECUTE 'COMMENT ON COLUMN public.session_messages."seq" IS ''Порядковый номер сообщения в сессии (0, 1, 2, ...).''';
    EXECUTE 'COMMENT ON COLUMN public.session_messages."role" IS ''Роль: user / assistant / system / tool.''';
    EXECUTE 'COMMENT ON COLUMN public.session_messages."content" IS ''Текст сообщения.''';
    EXECUTE 'COMMENT ON COLUMN public.session_messages."msg_timestamp" IS ''Оригинальный timestamp из upstream (text для совместимости).''';
    EXECUTE 'COMMENT ON COLUMN public.session_messages."tool_calls" IS ''JSONB: список вызовов инструментов ассистентом.''';
    EXECUTE 'COMMENT ON COLUMN public.session_messages."tool_call_id" IS ''ID вызова инструмента.''';
    EXECUTE 'COMMENT ON COLUMN public.session_messages."name" IS ''Имя tool-функции.''';
    EXECUTE 'COMMENT ON COLUMN public.session_messages."reasoning_content" IS ''Цепочка рассуждений модели.''';
    EXECUTE 'COMMENT ON COLUMN public.session_messages."thinking_blocks" IS ''JSONB: расширенное reasoning для thinking-моделей.''';
    EXECUTE 'COMMENT ON COLUMN public.session_messages."media" IS ''JSONB: вложения (картинки, файлы, ...).''';
    EXECUTE 'COMMENT ON COLUMN public.session_messages."cli_apps" IS ''JSONB: список CLI-приложений, доступных в сообщении.''';
    EXECUTE 'COMMENT ON COLUMN public.session_messages."mcp_presets" IS ''JSONB: MCP-конфигурация.''';
    EXECUTE 'COMMENT ON COLUMN public.session_messages."injected_event" IS ''Маркер инжектированного события (webhook/timer).''';
    EXECUTE 'COMMENT ON COLUMN public.session_messages."_command" IS ''Внутренний флаг: системная команда.''';
    EXECUTE 'COMMENT ON COLUMN public.session_messages."_channel_delivery" IS ''Внутренний флаг: доставлено в канал.''';
    EXECUTE 'COMMENT ON COLUMN public.session_messages."created_at" IS ''Время записи в БД.''';
  END IF;
END $$;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema = 'public' AND table_name = 'question_runs') THEN
    EXECUTE 'COMMENT ON TABLE public.question_runs IS ''Контекст вопроса/прогона: пользователь, агент, статус, summary. Одна строка на request_id. Не дублируется на каждое событие лога.''';
    EXECUTE 'COMMENT ON COLUMN public.question_runs."request_id" IS ''PK — ID сообщения, вызвавшего обработку.''';
    EXECUTE 'COMMENT ON COLUMN public.question_runs."created_at" IS ''Время регистрации вопроса.''';
    EXECUTE 'COMMENT ON COLUMN public.question_runs."updated_at" IS ''Время последнего изменения (status/summary).''';
    EXECUTE 'COMMENT ON COLUMN public.question_runs."session_id" IS ''Ключ сессии (channel:chat_id).''';
    EXECUTE 'COMMENT ON COLUMN public.question_runs."user_id" IS ''ID пользователя (sender_id).''';
    EXECUTE 'COMMENT ON COLUMN public.question_runs."chat_id" IS ''ID чата.''';
    EXECUTE 'COMMENT ON COLUMN public.question_runs."channel" IS ''Канал (telegram/cli/etc).''';
    EXECUTE 'COMMENT ON COLUMN public.question_runs."agent_id" IS ''Агент, обрабатывающий вопрос.''';
    EXECUTE 'COMMENT ON COLUMN public.question_runs."parent_agent_id" IS ''Для подагента — родительский агент.''';
    EXECUTE 'COMMENT ON COLUMN public.question_runs."parent_request_id" IS ''Для подагента — request_id родительского вопроса.''';
    EXECUTE 'COMMENT ON COLUMN public.question_runs."is_subagent" IS ''True, если это подагент.''';
    EXECUTE 'COMMENT ON COLUMN public.question_runs."status" IS ''running / finished / error.''';
    EXECUTE 'COMMENT ON COLUMN public.question_runs."summary" IS ''Финальный ответ или описание задачи.''';
  END IF;
END $$;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema = 'public' AND table_name = 'agent_question_runs') THEN
    EXECUTE 'COMMENT ON TABLE public.agent_question_runs IS ''Контекст вопроса/прогона: пользователь, агент, статус, вопрос/ответ, summary. Одна строка на request_id. Не дублируется на каждое событие лога. Полный текст вопроса/ответа в question/response, media — вложения. Таблица агента (префикс agent_).''';
    EXECUTE 'COMMENT ON COLUMN public.agent_question_runs."request_id" IS ''PK — ID сообщения, вызвавшего обработку.''';
    EXECUTE 'COMMENT ON COLUMN public.agent_question_runs."created_at" IS ''Время регистрации вопроса.''';
    EXECUTE 'COMMENT ON COLUMN public.agent_question_runs."updated_at" IS ''Время последнего изменения (status/summary).''';
    EXECUTE 'COMMENT ON COLUMN public.agent_question_runs."session_id" IS ''Ключ сессии (channel:chat_id).''';
    EXECUTE 'COMMENT ON COLUMN public.agent_question_runs."user_id" IS ''ID пользователя (sender_id).''';
    EXECUTE 'COMMENT ON COLUMN public.agent_question_runs."chat_id" IS ''ID чата.''';
    EXECUTE 'COMMENT ON COLUMN public.agent_question_runs."channel" IS ''Канал (telegram/cli/etc).''';
    EXECUTE 'COMMENT ON COLUMN public.agent_question_runs."agent_id" IS ''Агент, обрабатывающий вопрос.''';
    EXECUTE 'COMMENT ON COLUMN public.agent_question_runs."parent_agent_id" IS ''Для подагента — родительский агент.''';
    EXECUTE 'COMMENT ON COLUMN public.agent_question_runs."parent_request_id" IS ''Для подагента — request_id родительского вопроса.''';
    EXECUTE 'COMMENT ON COLUMN public.agent_question_runs."is_subagent" IS ''True, если это подагент.''';
    EXECUTE 'COMMENT ON COLUMN public.agent_question_runs."status" IS ''running / finished / error.''';
    EXECUTE 'COMMENT ON COLUMN public.agent_question_runs."summary" IS ''Краткое описание: финальный ответ (обрезанный) или описание задачи.''';
    EXECUTE 'COMMENT ON COLUMN public.agent_question_runs."question" IS ''Полный текст вопроса (сообщения пользователя), без обрезки.''';
    EXECUTE 'COMMENT ON COLUMN public.agent_question_runs."response" IS ''Полный текст ответа агента, без обрезки.''';
    EXECUTE 'COMMENT ON COLUMN public.agent_question_runs."media" IS ''JSON-список вложений (media): пути/URL файлов, приложенных пользователем или агентом.''';
  END IF;
END $$;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema = 'public' AND table_name = 'gateway_logs') THEN
    EXECUTE 'COMMENT ON TABLE public.gateway_logs IS ''Структурированный журнал событий агента. Стройный: контекст вопроса в question_runs (по request_id), здесь — только то, что относится к конкретному событию.''';
    EXECUTE 'COMMENT ON COLUMN public.gateway_logs."id" IS ''PK события (UUID).''';
    EXECUTE 'COMMENT ON COLUMN public.gateway_logs."timestamp" IS ''Время события.''';
    EXECUTE 'COMMENT ON COLUMN public.gateway_logs."level" IS ''Уровень логирования: DEBUG/INFO/WARN/ERROR.''';
    EXECUTE 'COMMENT ON COLUMN public.gateway_logs."event_type" IS ''Тип события (tool_call, agent_run, ...).''';
    EXECUTE 'COMMENT ON COLUMN public.gateway_logs."request_id" IS ''FK-логически на question_runs.request_id.''';
    EXECUTE 'COMMENT ON COLUMN public.gateway_logs."session_id" IS ''Денормализованный channel:chat_id для удобства.''';
    EXECUTE 'COMMENT ON COLUMN public.gateway_logs."channel" IS ''Канал (telegram/cli/etc).''';
    EXECUTE 'COMMENT ON COLUMN public.gateway_logs."actor" IS ''Кто инициировал событие (user/agent/system).''';
    EXECUTE 'COMMENT ON COLUMN public.gateway_logs."name" IS ''Имя инструмента / задачи / сущности события.''';
    EXECUTE 'COMMENT ON COLUMN public.gateway_logs."summary" IS ''Краткое текстовое описание события.''';
    EXECUTE 'COMMENT ON COLUMN public.gateway_logs."payload" IS ''JSONB: детальные данные события.''';
    EXECUTE 'COMMENT ON COLUMN public.gateway_logs."metadata" IS ''JSONB: дополнительные метаданные.''';
  END IF;
END $$;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema = 'public' AND table_name = 'agent_gateway_logs') THEN
    EXECUTE 'COMMENT ON TABLE public.agent_gateway_logs IS ''Структурированный журнал событий агента. Стройный: контекст вопроса в agent_question_runs (по request_id), здесь — только то, что относится к конкретному событию. Таблица агента (префикс agent_).''';
    EXECUTE 'COMMENT ON COLUMN public.agent_gateway_logs."id" IS ''PK события (UUID).''';
    EXECUTE 'COMMENT ON COLUMN public.agent_gateway_logs."timestamp" IS ''Время события.''';
    EXECUTE 'COMMENT ON COLUMN public.agent_gateway_logs."level" IS ''Уровень логирования: DEBUG/INFO/WARN/ERROR.''';
    EXECUTE 'COMMENT ON COLUMN public.agent_gateway_logs."event_type" IS ''Тип события (tool_call, agent_run, ...).''';
    EXECUTE 'COMMENT ON COLUMN public.agent_gateway_logs."request_id" IS ''FK-логически на agent_question_runs.request_id.''';
    EXECUTE 'COMMENT ON COLUMN public.agent_gateway_logs."session_id" IS ''Денормализованный channel:chat_id для удобства.''';
    EXECUTE 'COMMENT ON COLUMN public.agent_gateway_logs."channel" IS ''Канал (telegram/cli/etc).''';
    EXECUTE 'COMMENT ON COLUMN public.agent_gateway_logs."actor" IS ''Кто инициировал событие (user/agent/system).''';
    EXECUTE 'COMMENT ON COLUMN public.agent_gateway_logs."name" IS ''Имя инструмента / задачи / сущности события.''';
    EXECUTE 'COMMENT ON COLUMN public.agent_gateway_logs."summary" IS ''Краткое текстовое описание события.''';
    EXECUTE 'COMMENT ON COLUMN public.agent_gateway_logs."payload" IS ''JSONB: детальные данные события.''';
    EXECUTE 'COMMENT ON COLUMN public.agent_gateway_logs."metadata" IS ''JSONB: дополнительные метаданные.''';
  END IF;
END $$;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema = 'public' AND table_name = 'benchmark_runs') THEN
    EXECUTE 'COMMENT ON TABLE public.benchmark_runs IS ''Мета-информация о прогонах бенчмарков (один прогон = один набор тестов). Управляется benchmarks/db.py.''';
    EXECUTE 'COMMENT ON COLUMN public.benchmark_runs."id" IS ''PK прогона (UUID).''';
    EXECUTE 'COMMENT ON COLUMN public.benchmark_runs."suite_name" IS ''Имя тестового набора.''';
    EXECUTE 'COMMENT ON COLUMN public.benchmark_runs."suite_tags" IS ''JSONB: теги набора (smoke/full/regression).''';
    EXECUTE 'COMMENT ON COLUMN public.benchmark_runs."config" IS ''JSONB: конфигурация прогона.''';
    EXECUTE 'COMMENT ON COLUMN public.benchmark_runs."total_items" IS ''Всего вопросов в прогоне.''';
    EXECUTE 'COMMENT ON COLUMN public.benchmark_runs."passed_items" IS ''Сколько вопросов прошло.''';
    EXECUTE 'COMMENT ON COLUMN public.benchmark_runs."total_score" IS ''Сумма score по всем вопросам.''';
    EXECUTE 'COMMENT ON COLUMN public.benchmark_runs."avg_score" IS ''Средний score по вопросам.''';
    EXECUTE 'COMMENT ON COLUMN public.benchmark_runs."duration_sec" IS ''Длительность прогона, сек.''';
    EXECUTE 'COMMENT ON COLUMN public.benchmark_runs."started_at" IS ''Время начала.''';
    EXECUTE 'COMMENT ON COLUMN public.benchmark_runs."finished_at" IS ''Время завершения (NULL пока идёт).''';
  END IF;
END $$;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema = 'public' AND table_name = 'benchmark_results') THEN
    EXECUTE 'COMMENT ON TABLE public.benchmark_results IS ''Результаты по каждому вопросу бенчмарка. Связаны с benchmark_runs по run_id.''';
    EXECUTE 'COMMENT ON COLUMN public.benchmark_results."id" IS ''PK результата (UUID).''';
    EXECUTE 'COMMENT ON COLUMN public.benchmark_results."run_id" IS ''FK на benchmark_runs.id.''';
    EXECUTE 'COMMENT ON COLUMN public.benchmark_results."item_id" IS ''ID тестового вопроса.''';
    EXECUTE 'COMMENT ON COLUMN public.benchmark_results."item_name" IS ''Человекочитаемое имя вопроса.''';
    EXECUTE 'COMMENT ON COLUMN public.benchmark_results."difficulty" IS ''Сложность (1-5 или шкала suite).''';
    EXECUTE 'COMMENT ON COLUMN public.benchmark_results."category" IS ''Категория (sql/reasoning/...).''';
    EXECUTE 'COMMENT ON COLUMN public.benchmark_results."item_type" IS ''single (один шаг) | multi_step.''';
    EXECUTE 'COMMENT ON COLUMN public.benchmark_results."passed" IS ''True, если ответ прошёл проверку.''';
    EXECUTE 'COMMENT ON COLUMN public.benchmark_results."score" IS ''Оценка 0.0–1.0 (от автотеста).''';
    EXECUTE 'COMMENT ON COLUMN public.benchmark_results."response" IS ''Ответ агента (text).''';
    EXECUTE 'COMMENT ON COLUMN public.benchmark_results."tools_used" IS ''JSONB: список вызванных инструментов.''';
    EXECUTE 'COMMENT ON COLUMN public.benchmark_results."skills_activated" IS ''JSONB: список активированных навыков.''';
    EXECUTE 'COMMENT ON COLUMN public.benchmark_results."total_iterations" IS ''Количество итераций агента.''';
    EXECUTE 'COMMENT ON COLUMN public.benchmark_results."duration_sec" IS ''Длительность ответа, сек.''';
    EXECUTE 'COMMENT ON COLUMN public.benchmark_results."error" IS ''Текст ошибки (если была).''';
    EXECUTE 'COMMENT ON COLUMN public.benchmark_results."llm_judge_score" IS ''Оценка LLM-judge (если использовался).''';
    EXECUTE 'COMMENT ON COLUMN public.benchmark_results."details" IS ''JSONB: произвольные детали прогона.''';
    EXECUTE 'COMMENT ON COLUMN public.benchmark_results."created_at" IS ''Время создания записи.''';
  END IF;
END $$;
