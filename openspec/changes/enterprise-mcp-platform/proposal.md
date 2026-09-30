# Enterprise-слой в один MCP-сервер, DuckDB удаляется

## Why

Агент (`nanobot-ai==0.3.5`) должен перестать быть носителем enterprise-логики.
Сейчас он совмещает три роли: рантайм диалога, транспорт сообщений и подсистему
доступа к данным. Каждая следующая роль добавлена «поверх» и стоила отдельного
кода в `lib/`.

### Данные — единственный крупный блок вне агента

Инвентаризация (73 файла `lib/`, `workspace/`, `tools/`, `benchmarks/`):

| Зона | Импорты `nanobot.*` |
|---|---:|
| `workspace/skills/**` | **0** |
| `workspace/tools/**` | 6 (3 tool-файла) |
| `lib/**` | 74 |

Домены уже не зависят от агента. Зависимость сосредоточена в `lib/`, который
 трогать нельзя, и в трёх tool-файлах. Значит перенос — это **смена места
жительства**, а не вытаскивание кода из фреймворка.

### Пул PostgreSQL уже централизован — его не нужно изобретать

`workspace/utils/db.py` (1063 строки) реализует ровно требуемую схему:
N потоков `_Worker` с выделенными соединениями, очередь `_Job`
(`_submit`/`_take_job`/`_requeue`), лизы на соединение
(`_acquire_lease`/`_release_lease`), `transaction()` с привязкой к lease
вызывающего, backoff, `pool_timeout`. Это **единственный** production-пул
в проекте: из 18 точек доступа к PostgreSQL владеет соединениями одна, и это
запрещено тестом (`tests/test_storage_hybridization.py`).

**Следствие:** capability `data` — это перенос существующего модуля плюс MCP-поверхность,
а не новая подсистема.

### DuckDB — лишняя абстракция

Цепочка данных сегодня: `oarb.audit_vectors (PG) → DuckDB-снапшот → FAISS`.
Векторы **уже лежат в PostgreSQL** (`gateway.vector.index.storage_table`),
поэтому посредник между базой и индексом ничего не добавляет. Стоимость:

* ~2 600 строк production-кода, которые умирают;
* 10 тестовых модулей, 64 из 159 тестовых файлов затрагиваются (40%);
* класс операционных проблем: блокировка файла на NFS, «устарел ли снимок»,
  путь `gateway.cache.local_path`;
* `TableRegistry` (347) существует только чтобы перечислить таблицы для
  снапшота.

Побочно исчезает весь этот класс проблем, а цепочка теряет один hop.

### 12 патчей — часть из них заменима хуками

`nanobot-ai==0.3.5` даёт 18 методов `AgentHook`, мутабельный `AgentHookContext`,
`EventSink` в per-turn контексте, `AgentTurnHookFactory` и 23 события. Проверка
чтением установленного пакета показала: 4 патча заменяются хуками или
собственным классом, 1 удаляется, ещё 4 уходят по мере миграции. Подробно —
`docs/architecture/runtime-patcher-inventory.md`, раздел «Поверхность расширения».

Проект уже решалось один раз: change `runtime-patcher-composition-cleanup` вынес
`project_tools` в загрузчик, `compact_tracking` — в подписку, `compact_command` —
в upstream-builtin. Приём рабочий.

### Состав инструментов должен задаваться файлами, а не кодом

Сегодня набор enterprise-tool'ов агента зашит в код: `project_tool_loader`
сканирует `workspace/tools/*.py`, но регистрация, валидация и подключение к
`AgentLoop` выполняются в `ApplicationContext` и в самом `server.py`. Добавление
операции — это правка кода сборки, а не добавление файла.

Правильная граница: `server.py` **не знает список инструментов заранее**.
Каталог `capabilities/*/tools/*.py` обходится при старте, каждый файл отдаёт
`ToolDefinition` через `create_tool(container)`, реестр превращает его в
MCP-инструмент. Добавление операции = один файл + перезапуск сервера.

### Один MCP-сервер, а не по одному на capability

Вторая редакция плана заводила три процесса — `data-mcp`, `vector-mcp`,
`llm-mcp` — и обосновывала это «уровнем доверия». Обоснование не выдержало
проверки: граница, которая реально имеет значение, — **агент ↔ enterprise**, и
её уже проводит сам протокол MCP. Разделение разбивало наш собственный код от
нашего собственного кода.

За него платили конкретно:

* **join невозможен.** `audit_analyzer` отвечает на вопрос, требующий одновременно
  SQL по `oarb.*` и поиска по FAISS. В двух процессах это два MCP-вызова, и
  модель склеивает результат у себя в голове;
* **один вопрос превращается в N вызовов к N серверам** — ровно тот
  антипаттерн, который первая редакция уже отвергла;
* **транзакция через границу процессов невозможна**;
* **три `server.py`, три записи в `mcpServers`, три health-check'а, три точки
  отказа при старте.**

Что разделение покупало — изоляцию тяжёлой загрузки FAISS и зависшего вызова
LLM. Первая снимается **ленивой сборкой индекса** по первому векторному
запросу (индекс — сотни тысяч векторов, сборка десятки секунд), вторая —
очередью и таймаутом на сетевом вызове.

Итог: **один процесс `enterprise-mcp`**, capability раскладываются по
каталогам. Capability ≠ процесс: если одна потребует изоляции, она переедет в
отдельный процесс конфигурацией загрузчика, без правки файлов операций.

## What Changes

- **BREAKING (операционное):** `agent_worker_claims` и протокол
  claim/lease/heartbeat/reclaim/heal удаляются. Агент становится
  **однопроцессным**; замена — опрос с `FOR UPDATE SKIP LOCKED`.
  Теряется отказоустойчивость уровня HA.
- **REMOVED:** `agent_worker_claims`, `benchmarks/`, `streamlit_app.py`,
  `lib/services/subprocess_manager.py`, `workspace/tools/example.py`
  (зарегистрирован как живой `ExampleTool`), `tools/legacy_audit.py`,
  `tools/test_audit.py`.
- **REMOVED:** DuckDB-кластер — `duckdb_cache_store.py`, `cache_load_service.py`,
  `cache_provider.py`, DuckDB-часть `duckdb_query.py` и `cache_provider_impl.py`;
  `skill_registration.py`; cache-API в `skill_config.py`; секция `CacheSettings`;
  88 строк кэш-обвязки в `application_context.py`.
- **ADDED:** `enterprise-mcp` — **один** MCP-процесс со всеми инструментами.
  Внутри — capability-каталоги `capabilities/<name>/{skill/SKILL.md,
  tools/*.py, service/}`, наполняемые динамическим реестром: `server.py` не
  содержит `@mcp.tool()` вручную и не знает состав инструментов. Ошибка любого
  одного файла валит старт целиком. Hot reload не делается.
- **ADDED:** capability `data` — пул, очередь, произвольный **read-only SQL**
  под AST-валидацией и именованные операции (`history_search`, `log_event`,
  `upsert_records`, `schema_check`). Произвольный write-SQL не выдаётся.
  У `query_sql` два предохранителя против дорогих запросов: AST-фильтр и
  `statement_timeout`, задаваемый сервером.
- **ADDED:** capability `vectors` — сборка FAISS в памяти напрямую из
  PostgreSQL, **лениво по первому запросу**. Модель эмбеддингов не требуется:
  векторы предрассчитаны.
- **ADDED:** capability `llm` — доменный вызов LLM как операция `complete`.
  `llm_client.py` (199) + `llm_config.py` (88) переезжают в `libs/llm` и
  перестают использоваться агентом.
- **MODIFIED:** доступ к офисным файлам остаётся **нативным tool'ом агента**.
  `office_files.py` не переезжает в `mcp-platform`; `python-docx`, `openpyxl`,
  `pypdf`, `python-pptx` остаются в зависимостях агента. Цель «установка
  Nanobot не тянет enterprise-стек» сознательно не выполняется в этой части.
- **MODIFIED:** логирование идёт хук → локальный буфер → батчевый flush →
  `enterprise-mcp: log_event`. Не блокирует ход; переполнение — дроп; петля не
  замыкается.
- **MODIFIED:** из 12 патчей **8 удаляются**, `tool_limits` и
  `context_governor` остаются частичными, `exec_limits` и `subagent_logging`
  остаются по структурным причинам. В инвентарь добавлена колонка
  «Условие удаления».
- **MOVED:** `workspace/utils/db.py`, `sql_safety.py`, `jsonb.py`, `clean_text.py`
  → `libs/enterprise_data`. `legal_summarizer` (~30 000 строк, ноль связности)
  → capability `legal`. `office_files.py` **не переезжает** — остаётся в
  агенте.

## Capabilities

- `data/duckdb-removal` — **NEW**. DuckDB как лишний посредник; цепочка
  `PG → FAISS`.
- `data/query` — **NEW**. Capability `data`: пул и очередь как граница; на
  поверхности агента read-only SQL под AST-валидацией и именованные операции
  с изоляцией.
- `data/vectors` — **NEW**. Capability `vectors`: сборка FAISS без снапшота и
  без модели эмбеддингов, ленивая загрузка по первому запросу.
- `data/llm` — **NEW**. Capability `llm`: доменный вызов LLM как операция
  `complete`; в агенте `llm_client.py` и `llm_config.py` перестают
  использоваться.
- `runtime/document-access` — **MODIFIED**: доступ к офисным файлам остаётся
  нативным tool'ом агента, capability `documents` не заводится.
- `runtime/patch-to-hook` — **NEW**. Правило «хуки вместо monkey-patch» и
  обязательное условие удаления у каждого патча.
- `runtime/tool-registry` — **NEW**. Состав сервера задаётся файлами
  `capabilities/*/tools/*.py`, а не кодом сборки; валидация на старте и отказ
  целиком при ошибке одного файла.
- `runtime/entrypoints` — **MODIFIED**: однопроцессный агент, без Streamlit.
- `logging-db` — **MODIFIED**: писатель переезжает в `enterprise-mcp`, форма
  сообщений остаётся в агенте.
- `data/cache-provider` — **REMOVED** целиком, вместе с
  `data/vector-indexes` в части снапшота.

## Impact

- **Остаток агента:** 21 781 → ~16 800 (после удалений) → ~15 500 (после
  замены патчей).
- **Удаляется:** ~3 600 строк кэша, 3 599 бенчмарков, 818 Streamlit,
  201 `check_worker_pool_integrity`, ~1 200 протокола аренды, 131 `example.py`.
- **Миграции БД:** DROP для `agent_worker_claims`, `agent_benchmark_runs`,
  `agent_benchmark_results`. `sql/vectors/create_vector_index_config.sql` и
  `create_vector_index_store.sql` — legacy, удаляются (таблица store уже
  удалена в `V003`).
- **Настройки:** удаляются `gateway.cache.local_path`,
  `gateway.vector.index.default_root` (уже DEPRECATED), `skills.*.tables`,
  `skills.*.vector_indexes`, 8 ключей `channels.postgres.*` аренды,
  `benchmark.*`, `streamlit.*`. Правятся `tests/test_config_keys.py` и
  `tests/test_docs_consistency.py`.
- **Тесты:** пересборка baseline **после** шага удаления DuckDB, не до. Строка
  допустимых падений (4 предсуществующих) изменится.
- **Потери:** HA-отказоустойчивость, end-to-end замер качества ответа,
  веб-интерфейс, а также чистота зависимостей агента в части офисных пакетов —
  они остаются в `requirements.txt` сознательно.
- **Существующий change `drop-local-cache-read-from-pg`** описывает снимок
  как таковой. Этот change идёт дальше: снимка не остаётся.
- **Не затрагивается:** `nanobot 0.3.5` как пакет, доменные таблицы `oarb.*`,
  контур сообщений `agent_conversation_messages`.

## Out of Scope

- **Не** форкается и **не** патчится `nanobot-ai`.
- **Не** создаётся собственный `AgentLoop`, `EnterpriseAgent` или расширение
  `ToolContext`.
- **Не** переносится несколько доменов за один шаг.
- **Не** удаляется `RuntimePatcher` целиком: 5–6 патчей остаются до появления
  соответствующих extension points upstream.
- **Не** переносится логика, зависящая от Nanobot: сжатие контекста, сессии,
  форма сообщений, хуки файловой политики.
- **Не** меняется схема доменных таблиц `oarb.*`.
