-- ============================================================================
-- COMMENT ON TABLE / COMMENT ON COLUMN для всех таблиц проекта.
-- Сгенерировано tools/generate_comments_sql.py из schema.json + extra.
-- Идемпотентно: COMMENT ON ... заменяет существующий.
-- Запуск: psql "$DATABASE_URL" -f sql/comments/apply_all_comments.sql
-- ============================================================================


-- ---- oarb.audit_reports ----
COMMENT ON TABLE oarb.audit_reports IS 'Акты аудиторской проверки (оформленные документы по результатам)';
COMMENT ON COLUMN oarb.audit_reports."id" IS 'Уникальный идентификатор акта';
COMMENT ON COLUMN oarb.audit_reports."audit_id" IS 'Ссылка на проверку, к которой относится акт';
COMMENT ON COLUMN oarb.audit_reports."report_number" IS 'Номер акта (внутренняя нумерация)';
COMMENT ON COLUMN oarb.audit_reports."report_date" IS 'Дата составления акта';
COMMENT ON COLUMN oarb.audit_reports."title" IS 'Название акта / заголовок документа';
COMMENT ON COLUMN oarb.audit_reports."full_text" IS 'Полный текст акта (если не используется разбивка по пунктам)';
COMMENT ON COLUMN oarb.audit_reports."created_at" IS 'Дата и время создания записи';
COMMENT ON COLUMN oarb.audit_reports."updated_at" IS 'Дата и время последнего обновления';

-- ---- oarb.audits ----
COMMENT ON TABLE oarb.audits IS 'Проверки (плановые и внеплановые аудиторские мероприятия)';
COMMENT ON COLUMN oarb.audits."id" IS 'Уникальный идентификатор проверки';
COMMENT ON COLUMN oarb.audits."title" IS 'Наименование / тема проверки';
COMMENT ON COLUMN oarb.audits."audit_type" IS 'Тип проверки (плановая, внеплановая и т.д.)';
COMMENT ON COLUMN oarb.audits."planned_date" IS 'Плановая дата проведения проверки';
COMMENT ON COLUMN oarb.audits."actual_date" IS 'Фактическая дата завершения проверки';
COMMENT ON COLUMN oarb.audits."status" IS 'Текущий статус проверки';
COMMENT ON COLUMN oarb.audits."auditee_entity" IS 'Проверяемый объект (подразделение, компания, филиал)';
COMMENT ON COLUMN oarb.audits."created_at" IS 'Дата и время создания записи';
COMMENT ON COLUMN oarb.audits."updated_at" IS 'Дата и время последнего обновления';

-- ---- oarb.report_items ----
COMMENT ON TABLE oarb.report_items IS 'Пункты (разделы) акта аудиторской проверки с текстовым наполнением';
COMMENT ON COLUMN oarb.report_items."id" IS 'Уникальный идентификатор пункта акта';
COMMENT ON COLUMN oarb.report_items."report_id" IS 'Ссылка на акт, которому принадлежит пункт';
COMMENT ON COLUMN oarb.report_items."item_number" IS 'Номер пункта (может содержать буквы, цифры, иерархию)';
COMMENT ON COLUMN oarb.report_items."item_title" IS 'Название пункта (заголовок раздела)';
COMMENT ON COLUMN oarb.report_items."item_content" IS 'Текст содержания пункта';
COMMENT ON COLUMN oarb.report_items."order_index" IS 'Порядковый индекс для ручной сортировки';
COMMENT ON COLUMN oarb.report_items."created_at" IS 'Дата и время создания записи';
COMMENT ON COLUMN oarb.report_items."updated_at" IS 'Дата и время последнего обновления';

-- ---- oarb.violations ----
COMMENT ON TABLE oarb.violations IS 'Отклонения (нарушения, проблемы), выявленные в ходе аудита';
COMMENT ON COLUMN oarb.violations."id" IS 'Уникальный идентификатор отклонения';
COMMENT ON COLUMN oarb.violations."audit_id" IS 'Ссылка на проверку, в ходе которой выявлено отклонение';
COMMENT ON COLUMN oarb.violations."report_id" IS 'Ссылка на акт, в котором зафиксировано отклонение';
COMMENT ON COLUMN oarb.violations."item_id" IS 'Ссылка на конкретный пункт акта, к которому относится отклонение';
COMMENT ON COLUMN oarb.violations."violation_code" IS 'Код нарушения по внутреннему классификатору (если используется)';
COMMENT ON COLUMN oarb.violations."description" IS 'Подробное описание сути отклонения';
COMMENT ON COLUMN oarb.violations."recommendation" IS 'Рекомендации аудитора по устранению нарушения';
COMMENT ON COLUMN oarb.violations."severity" IS 'Критичность нарушения (Низкая, Средняя, Высокая)';
COMMENT ON COLUMN oarb.violations."status" IS 'Текущий статус работы с нарушением';
COMMENT ON COLUMN oarb.violations."responsible" IS 'Ответственное лицо или подразделение за устранение';
COMMENT ON COLUMN oarb.violations."deadline" IS 'Плановая дата устранения нарушения';
COMMENT ON COLUMN oarb.violations."created_at" IS 'Дата и время фиксации отклонения в системе';
COMMENT ON COLUMN oarb.violations."updated_at" IS 'Дата и время последнего изменения записи';

-- ---- public.agent_predefined_scripts ----
COMMENT ON TABLE public.agent_predefined_scripts IS 'Реестр предопределённых SQL-скриптов навыка audit_analyzer. Источник истины для режима --mode predefined. JSONB-колонка parameters повторяет структуру dataclass ParamDefinition: {param_name: {type, required, default, description, validation}}. Копируется в DuckDB-кэш через db_additional_tables (config project.json) и читается в run-time через db_loader.load_registry().';
COMMENT ON COLUMN public.agent_predefined_scripts."name" IS 'PK — уникальное имя скрипта. Используется в CLI: --script <name>. Имя должно быть валидным идентификатором (^[a-z][a-z0-9_]*$) — иначе f-string в CacheProvider.query_sql может сломать SQL.';
COMMENT ON COLUMN public.agent_predefined_scripts."description" IS 'Краткое описание для меню/подсказок (1-2 строки). Показывается в list_available().';
COMMENT ON COLUMN public.agent_predefined_scripts."sql_template" IS 'SQL-шаблон с Jinja2-подобными блоками: {% if param %}...{% endif %} (условные блоки) и :param_name (плейсхолдеры). При выполнении DynamicQueryBuilder: рендерит условия, подставляет :param → %s, добавляет LIMIT :max_rows.';
COMMENT ON COLUMN public.agent_predefined_scripts."parameters" IS 'JSONB: {param_name: ParamDefinition}. ParamDefinition имеет поля: type (like/exact/limit/number/date/enum/boolean), required, default, description, validation (опц., для vector-резолва).';
COMMENT ON COLUMN public.agent_predefined_scripts."max_rows_default" IS 'Лимит строк по умолчанию (добавляется в LIMIT). Если передан --params с полем type=limit, перекрывает default.';
COMMENT ON COLUMN public.agent_predefined_scripts."returns" IS 'Что возвращает скрипт (для документации и LLM-промпта в --mode sql).';
COMMENT ON COLUMN public.agent_predefined_scripts."long_description" IS 'Подробное описание для LLM-промпта: что делает, когда использовать, edge cases.';
COMMENT ON COLUMN public.agent_predefined_scripts."created_at" IS 'Время создания записи (при первой INSERT).';
COMMENT ON COLUMN public.agent_predefined_scripts."updated_at" IS 'Время последнего изменения (обновляется триггером agent_predefined_scripts_touch_updated_at).';

-- ---- oarb.audit_vectors ----
COMMENT ON TABLE oarb.audit_vectors IS 'Векторные эмбеддинги для семантического поиска audit_analyzer.';
COMMENT ON COLUMN oarb.audit_vectors."id" IS 'PK эмбеддинга (BIGINT IDENTITY).';
COMMENT ON COLUMN oarb.audit_vectors."source" IS 'Имя индекса (= agent_vector_index_config.index_name).';
COMMENT ON COLUMN oarb.audit_vectors."content" IS 'Текст для отображения.';
COMMENT ON COLUMN oarb.audit_vectors."search_text" IS 'Текст по которому строился эмбеддинг.';
COMMENT ON COLUMN oarb.audit_vectors."table" IS 'Короткое имя исходной таблицы.';
COMMENT ON COLUMN oarb.audit_vectors."pk_value" IS 'PK исходной строки (TEXT для совместимости с UUID/BIGINT/INTEGER).';
COMMENT ON COLUMN oarb.audit_vectors."chunk_index" IS 'Номер чанка (если строка длинная).';
COMMENT ON COLUMN oarb.audit_vectors."chunk_count" IS 'Общее количество чанков строки.';
COMMENT ON COLUMN oarb.audit_vectors."row_data" IS 'Полная строка исходных данных (JSONB).';
COMMENT ON COLUMN oarb.audit_vectors."embedding" IS 'Векторный эмбеддинг float32 (REAL[]).';
COMMENT ON COLUMN oarb.audit_vectors."content_hash" IS 'MD5 от search_text (для инкрементальных обновлений).';
COMMENT ON COLUMN oarb.audit_vectors."max_src_track" IS 'MAX(track_column) в источнике.';
COMMENT ON COLUMN oarb.audit_vectors."synced_at" IS 'Время последней синхронизации.';
COMMENT ON COLUMN oarb.audit_vectors."created_at" IS 'Время создания записи в этой таблице.';

-- ---- public.agent_vector_index_config ----
COMMENT ON TABLE public.agent_vector_index_config IS 'КОНФИГУРАЦИЯ сборки векторных индексов. Описывает ЧТО строить: имя индекса, исходная таблица, колонки для content/embedding, колонка-маркер изменений. Не содержит самих векторов — только метаданные сборки. Используется tools/build_vectors.py.';
COMMENT ON COLUMN public.agent_vector_index_config."index_name" IS 'PK — уникальное имя индекса (= source в audit_vectors, = source в agent_vector_index_store).';
COMMENT ON COLUMN public.agent_vector_index_config."source_table" IS 'Короткое имя для колонки source в audit_vectors. Должно совпадать с index_name.';
COMMENT ON COLUMN public.agent_vector_index_config."src_table" IS 'Исходная таблица (schema.table), из которой берутся строки для эмбеддинга.';
COMMENT ON COLUMN public.agent_vector_index_config."pk_column" IS 'Колонка первичного ключа в исходной таблице (для join с agent_vector_index_store.metadata).';
COMMENT ON COLUMN public.agent_vector_index_config."content_cols" IS 'TEXT[] — колонки исходной таблицы, которые попадают в audit_vectors.content (для отображения).';
COMMENT ON COLUMN public.agent_vector_index_config."embedding_cols" IS 'JSONB — словарь {col_name: {chunk: bool}} — какие колонки эмбеддингить и чанковать ли.';
COMMENT ON COLUMN public.agent_vector_index_config."track_column" IS 'Колонка исходной таблицы для инкрементальных обновлений (обычно updated_at).';
COMMENT ON COLUMN public.agent_vector_index_config."enabled" IS 'False — пропустить индекс при сборке (например, при отключении).';
COMMENT ON COLUMN public.agent_vector_index_config."created_at" IS 'Время создания записи конфига.';
COMMENT ON COLUMN public.agent_vector_index_config."updated_at" IS 'Время последнего изменения конфига.';

-- ---- public.agent_vector_index_store ----
COMMENT ON TABLE public.agent_vector_index_store IS 'СЕРИАЛИЗОВАННЫЕ FAISS-ИНДЕКСЫ (binary blob + metadata). Одна строка на source (= index_name из agent_vector_index_config). Строится из audit_vectors инструментами build_vectors.py: собираются все векторы одного source в faiss.IndexFlatIP/IVFFlat, сериализуются в BYTEA. Загружается lib.services.cache_provider_impl при search_vector. Контраст с audit_vectors: audit_vectors — это сырьё (по чанкам с метаданными), agent_vector_index_store — готовый поисковый индекс (быстрый ANN).';
COMMENT ON COLUMN public.agent_vector_index_store."source" IS 'PK — имя индекса (= index_name из agent_vector_index_config, = source в audit_vectors).';
COMMENT ON COLUMN public.agent_vector_index_store."index_binary" IS 'Сериализованный FAISS-индекс (pickle/bytes). Десериализуется при search_vector.';
COMMENT ON COLUMN public.agent_vector_index_store."metadata" IS 'JSONB: {pk_value: {source, chunk_index, row_id, ...}} — связь FAISS-индекса с audit_vectors.';
COMMENT ON COLUMN public.agent_vector_index_store."dimension" IS 'Размерность векторов (должна совпадать с embedding в audit_vectors).';
COMMENT ON COLUMN public.agent_vector_index_store."vector_count" IS 'Количество векторов в индексе (контроль согласованности с audit_vectors).';
COMMENT ON COLUMN public.agent_vector_index_store."updated_at" IS 'Время последней пересборки индекса.';

-- ---- public.agent_session_meta ----
COMMENT ON TABLE public.agent_session_meta IS 'Метаданные сессий nanobot. Заменяет JSONL-файлы в workspace/sessions/. Управляется PGSessionManager (lib/session/pg_session_manager.py). Таблица агента (префикс agent_).';
COMMENT ON COLUMN public.agent_session_meta."session_key" IS 'PK — уникальный ключ сессии (например, "telegram:12345").';
COMMENT ON COLUMN public.agent_session_meta."created_at" IS 'Время создания сессии.';
COMMENT ON COLUMN public.agent_session_meta."updated_at" IS 'Время последнего изменения.';
COMMENT ON COLUMN public.agent_session_meta."last_consolidated" IS 'Последний seq, до которого сообщения консолидированы.';
COMMENT ON COLUMN public.agent_session_meta."metadata" IS 'Произвольные метаданные сессии (user_id, channel, ...).';

-- ---- public.agent_session_messages ----
COMMENT ON TABLE public.agent_session_messages IS 'Сообщения чата в рамках сессии (append-only по session_key+seq). Таблица агента (префикс agent_).';
COMMENT ON COLUMN public.agent_session_messages."id" IS 'PK сообщения.';
COMMENT ON COLUMN public.agent_session_messages."session_key" IS 'FK-логически на agent_session_meta.session_key (FK не объявлено для GP).';
COMMENT ON COLUMN public.agent_session_messages."seq" IS 'Порядковый номер сообщения в сессии (0, 1, 2, ...).';
COMMENT ON COLUMN public.agent_session_messages."role" IS 'Роль: user / assistant / system / tool.';
COMMENT ON COLUMN public.agent_session_messages."content" IS 'Текст сообщения.';
COMMENT ON COLUMN public.agent_session_messages."msg_timestamp" IS 'Оригинальный timestamp из upstream (text для совместимости).';
COMMENT ON COLUMN public.agent_session_messages."tool_calls" IS 'JSONB: список вызовов инструментов ассистентом.';
COMMENT ON COLUMN public.agent_session_messages."tool_call_id" IS 'ID вызова инструмента.';
COMMENT ON COLUMN public.agent_session_messages."name" IS 'Имя tool-функции.';
COMMENT ON COLUMN public.agent_session_messages."reasoning_content" IS 'Цепочка рассуждений модели.';
COMMENT ON COLUMN public.agent_session_messages."thinking_blocks" IS 'JSONB: расширенное reasoning для thinking-моделей.';
COMMENT ON COLUMN public.agent_session_messages."media" IS 'JSONB: вложения (картинки, файлы, ...).';
COMMENT ON COLUMN public.agent_session_messages."cli_apps" IS 'JSONB: список CLI-приложений, доступных в сообщении.';
COMMENT ON COLUMN public.agent_session_messages."mcp_presets" IS 'JSONB: MCP-конфигурация.';
COMMENT ON COLUMN public.agent_session_messages."injected_event" IS 'Маркер инжектированного события (webhook/timer).';
COMMENT ON COLUMN public.agent_session_messages."_command" IS 'Внутренний флаг: системная команда.';
COMMENT ON COLUMN public.agent_session_messages."_channel_delivery" IS 'Внутренний флаг: доставлено в канал.';
COMMENT ON COLUMN public.agent_session_messages."created_at" IS 'Время записи в БД.';

-- ---- public.agent_conversation_messages ----
COMMENT ON TABLE public.agent_conversation_messages IS 'Таблица обмена сообщениями канала PostgresChannel / Web-чата (Streamlit). Агент опрашивает входящие (status=pending), отвечает и пишет ответ обратно в эту же таблицу. Единотабличная схема (роль в role, рассуждения в metadata.reasoning). Таблица агента (префикс agent_).';
COMMENT ON COLUMN public.agent_conversation_messages."id" IS 'PK — уникальный ID сообщения (UUID).';
COMMENT ON COLUMN public.agent_conversation_messages."chat_id" IS 'ID чата / диалога.';
COMMENT ON COLUMN public.agent_conversation_messages."user_id" IS 'ID отправителя (пользователь или агент).';
COMMENT ON COLUMN public.agent_conversation_messages."role" IS 'Роль: user / assistant / system / tool.';
COMMENT ON COLUMN public.agent_conversation_messages."content" IS 'Текст сообщения.';
COMMENT ON COLUMN public.agent_conversation_messages."media" IS 'JSONB: вложения (картинки, файлы, ...).';
COMMENT ON COLUMN public.agent_conversation_messages."metadata" IS 'JSONB: дополнительные метаданные (reasoning, session, ...).';
COMMENT ON COLUMN public.agent_conversation_messages."reply_to" IS 'ID родительского сообщения (для связки ответ—вопрос).';
COMMENT ON COLUMN public.agent_conversation_messages."buttons" IS 'JSONB: интерактивные кнопки/инлайн-клавиатура.';
COMMENT ON COLUMN public.agent_conversation_messages."status" IS 'Статус: pending / processing / completed (конвейер канала).';
COMMENT ON COLUMN public.agent_conversation_messages."created_at" IS 'Время создания сообщения.';
COMMENT ON COLUMN public.agent_conversation_messages."updated_at" IS 'Время последнего изменения (статус/reasoning).';

-- ---- public.agent_question_runs ----
COMMENT ON TABLE public.agent_question_runs IS 'Контекст вопроса/прогона: пользователь, агент, статус, вопрос/ответ, summary. Одна строка на request_id. Не дублируется на каждое событие лога. Полный текст вопроса/ответа в question/response, media — вложения. Таблица агента (префикс agent_).';
COMMENT ON COLUMN public.agent_question_runs."request_id" IS 'PK — ID сообщения, вызвавшего обработку.';
COMMENT ON COLUMN public.agent_question_runs."created_at" IS 'Время регистрации вопроса.';
COMMENT ON COLUMN public.agent_question_runs."updated_at" IS 'Время последнего изменения (status/summary).';
COMMENT ON COLUMN public.agent_question_runs."session_id" IS 'Ключ сессии (channel:chat_id).';
COMMENT ON COLUMN public.agent_question_runs."user_id" IS 'ID пользователя (sender_id).';
COMMENT ON COLUMN public.agent_question_runs."chat_id" IS 'ID чата.';
COMMENT ON COLUMN public.agent_question_runs."channel" IS 'Канал (telegram/cli/etc).';
COMMENT ON COLUMN public.agent_question_runs."agent_id" IS 'Агент, обрабатывающий вопрос.';
COMMENT ON COLUMN public.agent_question_runs."parent_agent_id" IS 'Для подагента — родительский агент.';
COMMENT ON COLUMN public.agent_question_runs."parent_request_id" IS 'Для подагента — request_id родительского вопроса.';
COMMENT ON COLUMN public.agent_question_runs."is_subagent" IS 'True, если это подагент.';
COMMENT ON COLUMN public.agent_question_runs."status" IS 'running / finished / error.';
COMMENT ON COLUMN public.agent_question_runs."summary" IS 'Краткое описание: финальный ответ (обрезанный) или описание задачи.';
COMMENT ON COLUMN public.agent_question_runs."question" IS 'Полный текст вопроса (сообщения пользователя), без обрезки.';
COMMENT ON COLUMN public.agent_question_runs."response" IS 'Полный текст ответа агента, без обрезки.';
COMMENT ON COLUMN public.agent_question_runs."media" IS 'JSON-список вложений (media): пути/URL файлов, приложенных пользователем или агентом.';

-- ---- public.agent_gateway_logs ----
COMMENT ON TABLE public.agent_gateway_logs IS 'Структурированный журнал событий агента. Стройный: контекст вопроса в agent_question_runs (по request_id), здесь — только то, что относится к конкретному событию. Таблица агента (префикс agent_).';
COMMENT ON COLUMN public.agent_gateway_logs."id" IS 'PK события (UUID).';
COMMENT ON COLUMN public.agent_gateway_logs."timestamp" IS 'Время события.';
COMMENT ON COLUMN public.agent_gateway_logs."level" IS 'Уровень логирования: DEBUG/INFO/WARN/ERROR.';
COMMENT ON COLUMN public.agent_gateway_logs."event_type" IS 'Тип события (tool_call, agent_run, ...).';
COMMENT ON COLUMN public.agent_gateway_logs."request_id" IS 'FK-логически на agent_question_runs.request_id.';
COMMENT ON COLUMN public.agent_gateway_logs."session_id" IS 'Денормализованный channel:chat_id для удобства.';
COMMENT ON COLUMN public.agent_gateway_logs."channel" IS 'Канал (telegram/cli/etc).';
COMMENT ON COLUMN public.agent_gateway_logs."actor" IS 'Кто инициировал событие (user/agent/system).';
COMMENT ON COLUMN public.agent_gateway_logs."name" IS 'Имя инструмента / задачи / сущности события.';
COMMENT ON COLUMN public.agent_gateway_logs."summary" IS 'Краткое текстовое описание события.';
COMMENT ON COLUMN public.agent_gateway_logs."payload" IS 'JSONB: детальные данные события.';
COMMENT ON COLUMN public.agent_gateway_logs."metadata" IS 'JSONB: дополнительные метаданные.';

-- ---- public.agent_benchmark_runs ----
COMMENT ON TABLE public.agent_benchmark_runs IS 'Мета-информация о прогонах бенчмарков (один прогон = один набор тестов). Управляется benchmarks/db.py. Таблица агента (префикс agent_).';
COMMENT ON COLUMN public.agent_benchmark_runs."id" IS 'PK прогона (UUID).';
COMMENT ON COLUMN public.agent_benchmark_runs."suite_name" IS 'Имя тестового набора.';
COMMENT ON COLUMN public.agent_benchmark_runs."suite_tags" IS 'JSONB: теги набора (smoke/full/regression).';
COMMENT ON COLUMN public.agent_benchmark_runs."config" IS 'JSONB: конфигурация прогона.';
COMMENT ON COLUMN public.agent_benchmark_runs."total_items" IS 'Всего вопросов в прогоне.';
COMMENT ON COLUMN public.agent_benchmark_runs."passed_items" IS 'Сколько вопросов прошло.';
COMMENT ON COLUMN public.agent_benchmark_runs."total_score" IS 'Сумма score по всем вопросам.';
COMMENT ON COLUMN public.agent_benchmark_runs."avg_score" IS 'Средний score по вопросам.';
COMMENT ON COLUMN public.agent_benchmark_runs."duration_sec" IS 'Длительность прогона, сек.';
COMMENT ON COLUMN public.agent_benchmark_runs."started_at" IS 'Время начала.';
COMMENT ON COLUMN public.agent_benchmark_runs."finished_at" IS 'Время завершения (NULL пока идёт).';

-- ---- public.agent_benchmark_results ----
COMMENT ON TABLE public.agent_benchmark_results IS 'Результаты по каждому вопросу бенчмарка. Связаны с agent_benchmark_runs по run_id. Таблица агента (префикс agent_).';
COMMENT ON COLUMN public.agent_benchmark_results."id" IS 'PK результата (UUID).';
COMMENT ON COLUMN public.agent_benchmark_results."run_id" IS 'FK на agent_benchmark_runs.id.';
COMMENT ON COLUMN public.agent_benchmark_results."item_id" IS 'ID тестового вопроса.';
COMMENT ON COLUMN public.agent_benchmark_results."item_name" IS 'Человекочитаемое имя вопроса.';
COMMENT ON COLUMN public.agent_benchmark_results."difficulty" IS 'Сложность (1-5 или шкала suite).';
COMMENT ON COLUMN public.agent_benchmark_results."category" IS 'Категория (sql/reasoning/...).';
COMMENT ON COLUMN public.agent_benchmark_results."item_type" IS 'single (один шаг) | multi_step.';
COMMENT ON COLUMN public.agent_benchmark_results."passed" IS 'True, если ответ прошёл проверку.';
COMMENT ON COLUMN public.agent_benchmark_results."score" IS 'Оценка 0.0–1.0 (от автотеста).';
COMMENT ON COLUMN public.agent_benchmark_results."response" IS 'Ответ агента (text).';
COMMENT ON COLUMN public.agent_benchmark_results."tools_used" IS 'JSONB: список вызванных инструментов.';
COMMENT ON COLUMN public.agent_benchmark_results."skills_activated" IS 'JSONB: список активированных навыков.';
COMMENT ON COLUMN public.agent_benchmark_results."total_iterations" IS 'Количество итераций агента.';
COMMENT ON COLUMN public.agent_benchmark_results."duration_sec" IS 'Длительность ответа, сек.';
COMMENT ON COLUMN public.agent_benchmark_results."error" IS 'Текст ошибки (если была).';
COMMENT ON COLUMN public.agent_benchmark_results."llm_judge_score" IS 'Оценка LLM-judge (если использовался).';
COMMENT ON COLUMN public.agent_benchmark_results."details" IS 'JSONB: произвольные детали прогона.';
COMMENT ON COLUMN public.agent_benchmark_results."created_at" IS 'Время создания записи.';
