"""Генератор apply_all_comments.sql из schema.json + extra описаний."""
import json
from pathlib import Path


def _sql_escape(s: str) -> str:
    return s.replace("'", "''")


def render_table(name: str, full: str, comment: str, columns: dict) -> list[str]:
    """Рендер COMMENT для одной таблицы."""
    out = []
    if comment and comment.strip():
        out.append(f"COMMENT ON TABLE {full} IS '{_sql_escape(comment)}';")
    for col_name, col_comment in columns.items():
        col_comment = (col_comment or "").strip()
        if col_comment:
            out.append(f"COMMENT ON COLUMN {full}.\"{col_name}\" IS '{_sql_escape(col_comment)}';")
    return out


# 1. Базовые таблицы из schema.json (workspace/skills/audit_analyzer/cache/schema.json)
data = json.loads(Path(r"workspace/skills/audit_analyzer/cache/schema.json").read_text(encoding="utf-8"))
schema_name = data["schema"]
audit_tables = {}
for tbl_name, tbl in data["tables"].items():
    cols = {cn: (cc.get("comment") or "") for cn, cc in tbl.get("columns", {}).items()}
    audit_tables[f"{schema_name}.{tbl_name}"] = (tbl.get("comment") or "", cols)


# 2. predefined_scripts
predefined_scripts = {
    "public.agent_predefined_scripts": (
        "Реестр предопределённых SQL-скриптов навыка audit_analyzer. "
        "Источник истины для режима --mode predefined. "
        "JSONB-колонка parameters повторяет структуру dataclass ParamDefinition: "
        "{param_name: {type, required, default, description, validation}}. "
        "Копируется в DuckDB-кэш через db_additional_tables (config project.json) "
        "и читается в run-time через db_loader.load_registry().",
        {
            "name": "PK — уникальное имя скрипта. Используется в CLI: --script <name>. "
            "Имя должно быть валидным идентификатором (^[a-z][a-z0-9_]*$) — иначе "
            "f-string в CacheProvider.query_sql может сломать SQL.",
            "description": "Краткое описание для меню/подсказок (1-2 строки). Показывается в list_available().",
            "sql_template": "SQL-шаблон с Jinja2-подобными блоками: "
            "{% if param %}...{% endif %} (условные блоки) и :param_name (плейсхолдеры). "
            "При выполнении DynamicQueryBuilder: рендерит условия, подставляет :param → %s, "
            "добавляет LIMIT :max_rows.",
            "parameters": "JSONB: {param_name: ParamDefinition}. ParamDefinition имеет поля: "
            "type (like/exact/limit/number/date/enum/boolean), required, default, "
            "description, validation (опц., для vector-резолва).",
            "max_rows_default": "Лимит строк по умолчанию (добавляется в LIMIT). "
            "Если передан --params с полем type=limit, перекрывает default.",
            "returns": "Что возвращает скрипт (для документации и LLM-промпта в --mode generated_sql).",
            "long_description": "Подробное описание для LLM-промпта: что делает, когда использовать, edge cases.",
            "created_at": "Время создания записи (при первой INSERT).",
            "updated_at": "Время последнего изменения (выставляется кодом при INSERT/UPDATE).",
        },
    )
}

# 3. audit_vectors
audit_vectors = {
    "oarb.audit_vectors": (
        "Векторные эмбеддинги для семантического поиска audit_analyzer.",
        {
            "id": "PK эмбеддинга (BIGINT IDENTITY).",
            "source": "Имя индекса (= agent_vector_index_config.index_name).",
            "content": "Текст для отображения.",
            "search_text": "Текст по которому строился эмбеддинг.",
            "table": "Короткое имя исходной таблицы.",
            "pk_value": "PK исходной строки (TEXT для совместимости с UUID/BIGINT/INTEGER).",
            "chunk_index": "Номер чанка (если строка длинная).",
            "chunk_count": "Общее количество чанков строки.",
            "row_data": "Полная строка исходных данных (JSONB).",
            "embedding": "Векторный эмбеддинг float32 (REAL[]).",
            "content_hash": "MD5 от search_text (для инкрементальных обновлений).",
            "max_src_track": "MAX(track_column) в источнике.",
            "synced_at": "Время последней синхронизации.",
            "created_at": "Время создания записи в этой таблице.",
        },
    )
}

# 4. vector_index_config
vector_index_config = {
    "public.agent_vector_index_config": (
        "КОНФИГУРАЦИЯ сборки векторных индексов. "
        "Описывает ЧТО строить: имя индекса, исходная таблица, колонки для content/embedding, "
        "колонка-маркер изменений. Не содержит самих векторов — только метаданные сборки. "
        "Используется tools/build_vectors.py.",
        {
            "index_name": "PK — уникальное имя индекса (= source в audit_vectors, = source в agent_vector_index_store).",
            "source_table": "Короткое имя для колонки source в audit_vectors. Должно совпадать с index_name.",
            "src_table": "Исходная таблица (schema.table), из которой берутся строки для эмбеддинга.",
            "pk_column": "Колонка первичного ключа в исходной таблице (для join с agent_vector_index_store.metadata).",
            "content_cols": "TEXT[] — колонки исходной таблицы, которые попадают в audit_vectors.content (для отображения).",
            "embedding_cols": "JSONB — словарь {col_name: {chunk: bool}} — какие колонки эмбеддингить и чанковать ли.",
            "track_column": "Колонка исходной таблицы для инкрементальных обновлений (обычно updated_at).",
            "enabled": "False — пропустить индекс при сборке (например, при отключении).",
            "created_at": "Время создания записи конфига.",
            "updated_at": "Время последнего изменения конфига.",
        },
    )
}

# 5. vector_index_store
vector_index_store = {
    "public.agent_vector_index_store": (
        "СЕРИАЛИЗОВАННЫЕ FAISS-ИНДЕКСЫ (binary blob + metadata). "
        "Одна строка на source (= index_name из agent_vector_index_config). "
        "Строится из audit_vectors инструментами build_vectors.py: "
        "собираются все векторы одного source в faiss.IndexFlatIP/IVFFlat, "
        "сериализуются в BYTEA. Загружается lib.services.cache_provider_impl при search_vector. "
        "Контраст с audit_vectors: audit_vectors — это сырьё (по чанкам с метаданными), "
        "agent_vector_index_store — готовый поисковый индекс (быстрый ANN).",
        {
            "source": "PK — имя индекса (= index_name из agent_vector_index_config, = source в audit_vectors).",
            "index_binary": "Сериализованный FAISS-индекс (pickle/bytes). Десериализуется при search_vector.",
            "metadata": "JSONB: {pk_value: {source, chunk_index, row_id, ...}} — связь FAISS-индекса с audit_vectors.",
            "dimension": "Размерность векторов (должна совпадать с embedding в audit_vectors).",
            "vector_count": "Количество векторов в индексе (контроль согласованности с audit_vectors).",
            "updated_at": "Время последней пересборки индекса.",
        },
    )
}

# 6. session_*
session = {
    "public.agent_session_meta": (
        "Метаданные сессий nanobot. Заменяет JSONL-файлы в workspace/sessions/. "
        "Управляется PGSessionManager (lib/session/pg_session_manager.py). "
        "Таблица агента (префикс agent_).",
        {
            "session_key": 'PK — уникальный ключ сессии (например, "telegram:12345").',
            "created_at": "Время создания сессии.",
            "updated_at": "Время последнего изменения.",
            "last_consolidated": "Последний seq, до которого сообщения консолидированы.",
            "metadata": "Произвольные метаданные сессии (user_id, channel, ...).",
        },
    ),
    "public.agent_session_messages": (
        "Сообщения чата в рамках сессии (append-only по session_key+seq). "
        "Таблица агента (префикс agent_).",
        {
            "id": "PK сообщения.",
            "session_key": "FK-логически на agent_session_meta.session_key (FK не объявлено для GP).",
            "seq": "Порядковый номер сообщения в сессии (0, 1, 2, ...).",
            "role": "Роль: user / assistant / system / tool.",
            "content": "Текст сообщения.",
            "msg_timestamp": "Оригинальный timestamp из upstream (text для совместимости).",
            "tool_calls": "JSONB: список вызовов инструментов ассистентом.",
            "tool_call_id": "ID вызова инструмента.",
            "name": "Имя tool-функции.",
            "reasoning_content": "Цепочка рассуждений модели.",
            "thinking_blocks": "JSONB: расширенное reasoning для thinking-моделей.",
            "media": "JSONB: вложения (картинки, файлы, ...).",
            "cli_apps": "JSONB: список CLI-приложений, доступных в сообщении.",
            "mcp_presets": "JSONB: MCP-конфигурация.",
            "injected_event": "Маркер инжектированного события (webhook/timer).",
            "_command": "Внутренний флаг: системная команда.",
            "_channel_delivery": "Внутренний флаг: доставлено в канал.",
            "created_at": "Время записи в БД.",
        },
    ),
    "public.agent_conversation_messages": (
        "Таблица обмена сообщениями канала PostgresChannel / Web-чата (Streamlit). "
        "Агент опрашивает входящие (status=pending), отвечает и пишет ответ обратно "
        "в эту же таблицу. Единотабличная схема (роль в role, рассуждения в metadata.reasoning). "
        "Таблица агента (префикс agent_).",
        {
            "id": "PK — уникальный ID сообщения (UUID).",
            "chat_id": "ID чата / диалога.",
            "user_id": "ID отправителя (пользователь или агент).",
            "role": "Роль: user / assistant / system / tool.",
            "content": "Текст сообщения.",
            "media": "JSONB: вложения (картинки, файлы, ...).",
            "metadata": "JSONB: дополнительные метаданные (reasoning, session, ...).",
            "reply_to": "ID родительского сообщения (для связки ответ—вопрос).",
            "buttons": "JSONB: интерактивные кнопки/инлайн-клавиатура.",
            "status": "Статус: pending / processing / completed (конвейер канала).",
            "created_at": "Время создания сообщения.",
            "updated_at": "Время последнего изменения (статус/reasoning).",
        },
    ),
}

# 7. logs
logs = {
    "public.agent_question_runs": (
        "Контекст вопроса/прогона: пользователь, агент, статус, вопрос/ответ, summary. "
        "Одна строка на request_id. Не дублируется на каждое событие лога. "
        "Полный текст вопроса/ответа в question/response, media — вложения. "
        "Таблица агента (префикс agent_).",
        {
            "request_id": "PK — ID сообщения, вызвавшего обработку.",
            "created_at": "Время регистрации вопроса.",
            "updated_at": "Время последнего изменения (status/summary).",
            "session_id": "Ключ сессии (channel:chat_id).",
            "user_id": "ID пользователя (sender_id).",
            "chat_id": "ID чата.",
            "channel": "Канал (telegram/cli/etc).",
            "agent_id": "Агент, обрабатывающий вопрос.",
            "parent_agent_id": "Для подагента — родительский агент.",
            "parent_request_id": "Для подагента — request_id родительского вопроса.",
            "is_subagent": "True, если это подагент.",
            "status": "running / finished / error.",
            "summary": "Краткое описание: финальный ответ (обрезанный) или описание задачи.",
            "question": "Полный текст вопроса (сообщения пользователя), без обрезки.",
            "response": "Полный текст ответа агента, без обрезки.",
            "media": "JSON-список вложений (media): пути/URL файлов, приложенных пользователем или агентом.",
        },
    ),
    "public.agent_gateway_logs": (
        "Структурированный журнал событий агента. "
        "Стройный: контекст вопроса в agent_question_runs (по request_id), "
        "здесь — только то, что относится к конкретному событию. "
        "Таблица агента (префикс agent_).",
        {
            "id": "PK события (UUID).",
            "timestamp": "Время события.",
            "level": "Уровень логирования: DEBUG/INFO/WARN/ERROR.",
            "event_type": "Тип события (tool_call, agent_run, ...).",
            "request_id": "FK-логически на agent_question_runs.request_id.",
            "session_id": "Денормализованный channel:chat_id для удобства.",
            "channel": "Канал (telegram/cli/etc).",
            "actor": "Кто инициировал событие (user/agent/system).",
            "name": "Имя инструмента / задачи / сущности события.",
            "summary": "Краткое текстовое описание события.",
            "payload": "JSONB: детальные данные события.",
            "metadata": "JSONB: дополнительные метаданные.",
        },
    ),
}

# 8. benchmark
benchmark = {
    "public.agent_benchmark_runs": (
        "Мета-информация о прогонах бенчмарков (один прогон = один набор тестов). "
        "Управляется benchmarks/db.py. Таблица агента (префикс agent_).",
        {
            "id": "PK прогона (UUID).",
            "suite_name": "Имя тестового набора.",
            "suite_tags": "JSONB: теги набора (smoke/full/regression).",
            "config": "JSONB: конфигурация прогона.",
            "total_items": "Всего вопросов в прогоне.",
            "passed_items": "Сколько вопросов прошло.",
            "total_score": "Сумма score по всем вопросам.",
            "avg_score": "Средний score по вопросам.",
            "duration_sec": "Длительность прогона, сек.",
            "started_at": "Время начала.",
            "finished_at": "Время завершения (NULL пока идёт).",
        },
    ),
    "public.agent_benchmark_results": (
        "Результаты по каждому вопросу бенчмарка. Связаны с agent_benchmark_runs по run_id. "
        "Таблица агента (префикс agent_).",
        {
            "id": "PK результата (UUID).",
            "run_id": "FK на agent_benchmark_runs.id.",
            "item_id": "ID тестового вопроса.",
            "item_name": "Человекочитаемое имя вопроса.",
            "difficulty": "Сложность (1-5 или шкала suite).",
            "category": "Категория (sql/reasoning/...).",
            "item_type": "single (один шаг) | multi_step.",
            "passed": "True, если ответ прошёл проверку.",
            "score": "Оценка 0.0–1.0 (от автотеста).",
            "response": "Ответ агента (text).",
            "tools_used": "JSONB: список вызванных инструментов.",
            "skills_activated": "JSONB: список активированных навыков.",
            "total_iterations": "Количество итераций агента.",
            "duration_sec": "Длительность ответа, сек.",
            "error": "Текст ошибки (если была).",
            "llm_judge_score": "Оценка LLM-judge (если использовался).",
            "details": "JSONB: произвольные детали прогона.",
            "created_at": "Время создания записи.",
        },
    ),
}


# Сборка
out_path = Path(r"sql/comments/apply_all_comments.sql")
out_path.parent.mkdir(parents=True, exist_ok=True)

lines = []
lines.append("-- ============================================================================")
lines.append("-- COMMENT ON TABLE / COMMENT ON COLUMN для всех таблиц проекта.")
lines.append("-- Сгенерировано tools/generate_comments_sql.py из schema.json + extra.")
lines.append("-- Идемпотентно: COMMENT ON ... заменяет существующий.")
lines.append("-- Запуск: psql \"$DATABASE_URL\" -f sql/comments/apply_all_comments.sql")
lines.append("-- ============================================================================")
lines.append("")

for full, (comment, columns) in {**audit_tables, **predefined_scripts, **audit_vectors,
                                   **vector_index_config, **vector_index_store,
                                   **session, **logs, **benchmark}.items():
    lines.append("")
    lines.append(f"-- ---- {full} ----")
    lines.extend(render_table(full, full, comment, columns))

out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"Written: {out_path} ({len(lines)} lines, {len(audit_tables) + len(predefined_scripts) + len(audit_vectors) + len(vector_index_config) + len(vector_index_store) + len(session) + len(logs) + len(benchmark)} tables)")
