# Tool Usage Notes

Tool signatures are provided automatically via function calling.
This file documents non-obvious constraints and usage patterns.

## exec — Safety Limits

- Commands have a configurable timeout (default 60s)
- Dangerous commands are blocked (rm -rf, format, dd, shutdown, etc.)
- Output is truncated at 10,000 characters
- `restrictToWorkspace` config can limit file access to the workspace

## glob — File Discovery

- Use `glob` to find files by pattern before falling back to shell commands
- Simple patterns like `*.py` match recursively by filename
- Use `entry_type="dirs"` when you need matching directories instead of files
- Use `head_limit` and `offset` to page through large result sets
- Prefer this over `exec` when you only need file paths

## grep — Content Search

- Use `grep` to search file contents inside the workspace
- Default behavior returns only matching file paths (`output_mode="files_with_matches"`)
- Supports optional `glob` filtering plus `context_before` / `context_after`
- Supports `type="py"`, `type="ts"`, `type="md"` and similar shorthand filters
- Use `fixed_strings=true` for literal keywords containing regex characters
- Use `output_mode="files_with_matches"` to get only matching file paths
- Use `output_mode="count"` to size a search before reading full matches
- Use `head_limit` and `offset` to page across results
- Prefer this over `exec` for code and history searches
- Binary or oversized files may be skipped to keep results readable

## cron — Scheduled Reminders

- Please refer to cron skill for usage.

## history_search — поиск по долговечному журналу агента

`history_search` — кастомный инструмент (см. `workspace/tools/history_search_tool.py`).
Ищет по `agent_gateway_logs` — журналу, который переживает context compaction
(в отличие от `agent_conversation_messages`). Полезно, когда пользователь
ссылается на старое сообщение или результат, который выпал из контекста.

**Параметры:**

- `query` (опц.) — подстрока для ILIKE-поиска по `summary` и `payload::text`.
- `event_type` (опц.) — один из `context_compacted`, `tool_call`,
  `tool_result`, `llm_call`, `run_finished`, `subagent_run_finished`, `inbound`.
- `tool_name` (опц.) — имя инструмента для фильтрации `tool_call` /
  `tool_result`. Удобно для поиска истории конкретного инструмента.
- `since` / `until` (опц.) — ISO-8601 таймстамп.
- `session_scope` (опц., дефолт `current`) — `current` (только текущая
  сессия) или `all` (по всем сессиям).
- `limit` (опц.) — максимум событий (по конфигу `max_rows`).

**Примеры:**

- «Какие файлы я прикладывал?» →
  `history_search(event_type="tool_call", tool_name="read_file")`
- «Когда последний раз сжимался контекст?» →
  `history_search(event_type="context_compacted", session_scope="current")`
- «Что я писал про договор аренды?» →
  `history_search(query="договор аренды", event_type="llm_call")`

**Замечания:**

- Для поиска файлов используй `tool_call` / `tool_result` (там аргументы
  и пути), а НЕ выдуманные типы (`file_attached`, `file_created`,
  `document_summarized` — таких нет в журнале).
- Если результат пустой — отвечай «не найдено в истории», не выдумывай.

## legal_summarizer_query — follow-up по уже проанализированному документу

Кастомный tool (`workspace/tools/legal_summarizer_query.py`). Возвращает
структурные данные по сохранённой `operation_id` **без перепарсинга PDF** —
читает manifest/result/chunks навыка `legal_summarizer` из
`data_store/cache/skills/legal_summarizer/<operation_id>/`.

**Зачем:** иначе на follow-up-вопрос («сколько статей?», «какие разделы?»,
«что в чанке N?») агент вынужден через `exec`+pdfplumber повторно
извлекать текст документа (200+ сек, часто падает на кириллице в Windows-cp1251).

**Параметры:**

- `operation_id` (обяз.) — поле `result.operation_id` из предыдущего ответа `legal_summarizer`.
- `field` (дефолт `stats`) — `stats | articles | chunks | sections | tree | all`.
- `max_chunk_summary_chars` (опц., дефолт 1500) — обрезка summary чанка для `field=chunks`.

**Когда звать:**

- Сразу после `--confirm` саммари вернуло `operation_id` → запомни его для follow-up'ов.
- Любой вопрос про уже проанализированный документ: «сколько статей?», «какие
  разделы?», «что в чанке 5?», «назови все части» и т.п.

**Примеры:**

- «Сколько статей в документе?» → `legal_summarizer_query(operation_id="<op_id>", field="articles")` → `{article_count: N}`
- «Какие разделы?» → `legal_summarizer_query(operation_id="<op_id>", field="sections")`
- «О чём чанк 12?» → `legal_summarizer_query(operation_id="<op_id>", field="chunks")` → массив с `chunk_id`, `summary`, `section_path`.

**Не делать:**

- Не вызывай `pdfplumber`/`pdftotext` через `exec` для подсчёта статей —
  есть `legal_summarizer_query`. Это и быстрее, и кириллица не сломается.
- Не передавай в `field` значения вне списка — будет отказ с понятной ошибкой.

## nl_sql_generate — генерация SELECT по запросу на естественном языке

Кастомный tool (`workspace/tools/nl_sql_generate.py`). Преобразует
NL-запрос в SELECT по whitelist'у зарегистрированных таблиц, валидирует
через EXPLAIN и выполняет в общем DuckDB-кеше. Заменил режим
`generated_sql` навыка `audit_analyzer` в виде generic tool.

**Архитектура (pipeline):**

1. `ColumnDescriptionsTool.lookup(query)` — подсказки термин→колонка.
2. `SchemaFormatter` (internal service) — описание схемы из DuckDB-снимка.
3. `NlSqlRunner.run(query, hints_block=hints)` — LLM retry-цикл +
   validate_sql + EXPLAIN + execute в общем кэше.
4. JSON-ответ с `sql`, `columns`, `rows`, `row_count`.

**Параметры:**

- `query` (обяз.) — запрос на естественном языке.
- `max_rows` (опц., дефолт 1000) — лимит возвращаемых строк.
- `no_few_shot` (опц., дефолт false) — пропустить few-shot из реестра.
- `skip_hints` (опц., дефолт false) — не вызывать `column_descriptions`.
- `hints_max_matches` (опц., дефолт 5) — сколько hints подмешать в system prompt.
- `context` (опц.) — история чата для LLM.

**Когда звать:**

- Любой NL→SELECT: «сколько X по Y?», «топ-N объектов по …», «динамика по …».
- Когда `duckdb_query` написан руками, но не уверен в правильных именах колонок.

**Примеры:**

- «Сколько аудитов в 2024?» → `nl_sql_generate(query="сколько аудитов в 2024")` → `{status, sql, columns: [...], rows: [...]}`
- «Топ-5 организаций по нарушениям» → `nl_sql_generate(query="топ-5 организаций по нарушениям", max_rows=5)`

**Не делать:**

- Не вызывай `duckdb_query` с самописным SELECT, если не уверен в схеме —
  ` `nl_sql_generate` сам подтянет hints и описание таблиц.
- Не пытайся использовать DDL/DML — tool зарубит через `validate_sql`.

## column_descriptions — структурированные подсказки термин→колонка

Кастомный tool (`workspace/tools/column_descriptions.py`). Возвращает
словарь подсказок для подмешивания в system prompt `nl_sql_generate`.
Заменяет бывший `workspace/skills/audit_analyzer/scripts/column_hints.py`.

**Аргументы:**

- `term` (опц.) — термин/фраза для поиска (case-insensitive).
- `match_all` (опц., дефолт false) — вернуть все entries.
- `max_matches` (опц., дефолт 20) — лимит matches.

**Конфиг** (`config.json::tools.column_descriptions`):

```json
{
  "enable": true,
  "data_file": "data_store/column_descriptions.json",
  "max_result_chars": 16000
}
```

Формат `data_file` (словарь generic, конкретные таблицы — на стороне
skill'а):

```json
{
  "synonym 1|synonym 2|синоним": [
    "schema.table.column"
  ]
}
```

Ключ может содержать `|` — список синонимов; совпадение с любым из них
считается положительным.

**Когда звать:** в большинстве случаев звать напрямую не нужно —
`nl_sql_generate` сам подтянет hints через in-process lookup
(`lib.services.column_descriptions.ColumnDescriptionsResolver`).
Прямой вызов имеет смысл, если хочешь заранее посмотреть подсказки
перед генерацией SQL или при отладке словаря.

**Пример:**

- «Какие колонки подходят для синонима?» →
  `column_descriptions(term="...")` →
  `{matches: [{terms: [...], columns: ["schema.table.column"]}]}`

## run_predefined_script — выполнение готового SQL из реестра

Кастомный tool (`workspace/tools/run_predefined_script.py`). Достаёт
SQL-шаблон из таблицы `public.agent_predefined_scripts` (зарегистрирована
как `TableResource(label="scripts_registry")`), проверяет параметры по
JSONB-схеме и выполняет SELECT в общем DuckDB-кеше. Не ходит в БД
напрямую и не делает собственный SQL execution: использует
`CacheProvider.query_sql` и `validate_sql` (тот же SELECT-only gate,
что `nl_sql_generate` / `duckdb_query`).

**Когда звать:**

- Есть готовый SQL-рецепт в реестре, и пользователь явно назвал скрипт
  (или имя легко восстановимо из запроса). Скрипт уже имеет
  параметризованный SQL и JSONB-схему параметров.
- Запрос стабильный, повторяемый и хочется детерминированного ответа
  без LLM-генерации.

**Когда НЕ звать:**

- Запрос свободной формы → `nl_sql_generate`.
- Уже есть точный SELECT → `duckdb_query`.
- Не знаешь имя скрипта или не уверен, что он существует — сначала
  посмотри `references/predefined_scripts.md` (или БД напрямую).

**Параметры:**

- `name` (обяз.) — PK скрипта в `public.agent_predefined_scripts`.
- `params` (опц.) — словарь `{param_name: value}` для JSONB-схемы
  `parameters`. Валидируется (required/default/type/choices/pattern/
  min/max/min_length/max_length). Лишние ключи → ошибка.
- `max_rows` (опц.) — переопределить `max_rows_default` из реестра
  (но не больше `gateway.run_predefined_script.max_rows`).

**Конфиг (`project.json::gateway.run_predefined_script`):**

```json
{
  "gateway": {
    "run_predefined_script": {
      "enable": true,
      "max_rows": 1000,
      "max_result_chars": 50000
    }
  }
}
```

**Примеры:**

- Сводка по статусам аудитов (скрипт без параметров):
  `run_predefined_script(name="audit_status_summary")` →
  `{status, name, sql, params: [], columns: [...], rows: [...], row_count, returned_rows, truncated}`
- Нарушения за период:
  `run_predefined_script(name="violations_by_period", params={"date_from": "2024-01-01", "date_to": "2024-12-31"})`

**Замечания:**

- SQL-шаблон хранится в БД, но **всё равно проходит `validate_sql`** —
  никаких исключений для predefined. Если шаблон содержит DDL/DML —
  tool откажет с `error_type: invalid_script`.
- Параметры передаются через `?`-placeholder'ы (DuckDB-стиль), а не
  строковой интерполяцией — SQL injection исключён на уровне движка.
- Если имя скрипта не указано в запросе пользователя — лучше сначала
  уточнить, чем угадывать. Agent сам выбирает predefined script по описанию
  в `SKILL.md` («Predefined scripts»); никакого auto-resolution в tool'е нет.

## vector_search — семантический поиск по FAISS-индексу

Кастомный tool (`workspace/tools/vector_search_tool.py`). Generic-поиск по
заранее зарегистрированному FAISS-индексу. Tool **не знает про домен** —
`index_name` выбирается Agent'ом на основании каталога в `SKILL.md`
(раздел «Vector indexes»).

**Архитектура:**

1. `CacheProvider.search_vector(query, index_name, top_k, threshold)` —
   абстрактный интерфейс к FAISS; конкретная реализация регистрируется
   runtime'ом.
2. Нормализация результата в JSON-контракт `{status, query, index_name,
   results: [{id, score, text, metadata}], count, truncated}`.
3. STALE/INVALID detection — поставщик (provider) помечает meta через
   `_signature_status` при загрузке индекса; tool пробрасывает
   `index_warning` если индекс требует пересборки.

**Параметры:**

- `query` (обяз.) — поисковый запрос на естественном языке.
- `index_name` (обяз.) — имя FAISS-индекса. Используй только имена из каталога
  в `SKILL.md` (`audits_index`, `violations_index`, `audit_reports_index`).
- `top_k` (опц., дефолт `gateway.vector_search.default_top_k=5`,
  потолок `max_top_k=50`) — сколько ближайших результатов вернуть.
- `threshold` (опц., дефолт `gateway.vector_search.default_threshold=0.0`,
  диапазон `[0.0, 1.0]`) — минимальная cosine-схожесть.

**Когда звать:**

- Семантический поиск: «найди похожие нарушения», «проверки по X»,
  «отчёты с выводами о …».
- Когда важен **смысл**, а не точные числа/фильтры.

**Когда НЕ звать:**

- COUNT / GROUP BY / ORDER BY → `nl_sql_generate`.
- Точный `id` → `duckdb_query WHERE id = ?`.
- Фильтры по конкретным колонкам (`severity`, `status`, `date`) →
  `nl_sql_generate`.
- Сложные JOIN'ы → `nl_sql_generate`.

**Конфиг (`project.json::gateway.vector_search.*`):**

```json
{
  "gateway": {
    "vector_search": {
      "enable": true,
      "default_top_k": 5,
      "max_top_k": 50,
      "default_threshold": 0.0,
      "max_query_chars": 4000,
      "max_result_chars": 16000,
      "timeout_sec": 30
    }
  }
}
```

**Пример:**

- «Найди нарушения про пожарную безопасность» →
  `vector_search(query="пожарная безопасность", index_name="violations_index", top_k=5, threshold=0.5)`
  → `{status, query, index_name, results: [...], count, truncated}`.

**Замечания:**

- Tool не выбирает `index_name` сам — Agent делает это по каталогу в `SKILL.md`.
- Если `index_name` не зарегистрирован — `error_type: missing_index` или
  `missing_provider`.
- `IndexIntegrityError` (STALE/INVALID) → `error_type: stale_index` /
  `invalid_index` с рекомендацией `rebuild via tools/build_vectors.py`.

## Capability layer для audit_analyzer

В режиме на `audit_analyzer` Agent выбирает один из трёх инструментов:

| Tool | Назначение | Когда |
|---|---|---|
| `run_predefined_script` | Выполнить готовый SQL из `public.agent_predefined_scripts` | Запрос точно соответствует скрипту из `SKILL.md` |
| `vector_search` | Семантический поиск по FAISS | Запрос про **смысл**, индекс есть в `SKILL.md` |
| `nl_sql_generate` | LLM-генерация SELECT | Fallback: всё остальное |

Agent сам читает `SKILL.md` и делает выбор. Ни один tool не делает
auto-routing или классификацию запроса.

