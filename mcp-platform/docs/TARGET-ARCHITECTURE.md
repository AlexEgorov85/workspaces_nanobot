# Целевая архитектура: два MCP

**Статус:** заменяет раздел «Целевая схема» в `MIGRATION.md` и 4 сервера в `architecture.html`.
**Ветка:** `refactor/mcp-platform` · **BASE_COMMIT:** `8ef9d08`

---

## 1. Решение

| | |
|---|---|
| **Серверы** | `data-mcp` (PostgreSQL), `vector-mcp` (векторные индексы) |
| **Хранилища** | PostgreSQL — единственный стор. FAISS — в памяти процесса |
| **DuckDB** | **удаляется полностью.** Считается лишней абстракцией |
| **Агент** | `nanobot-ai==0.3.5` не меняется. Знает только LLM, диалог, сессию, MCP-клиент |
| **Домены** | audit и legal — сервисы поверх общих библиотек. **Не серверы** |
| **Наполнение серверов** | Динамический реестр: `tools/*.py` → авто-регистрация. `server.py` не знает список инструментов заранее — см. §3.1 |

Цепочка данных сокращается на один hop:

```
было:  oarb.audit_vectors (PG) → DuckDB-снапшот → FAISS в памяти
стало: oarb.audit_vectors (PG) → FAISS в памяти
```

Векторы уже хранятся в PostgreSQL (`gateway.vector.index.storage_table`),
поэтому **`vector-mcp` не нуждается в модели эмбеддингов** — он только
загружает готовые векторы в FAISS. Эмбеддинги считает отдельная задача
(`tools/build_vectors.py`), которая пишет в PG.

---

## 2. Главная находка аудита: первый MCP уже написан

`workspace/utils/db.py` (1063 строки) — это **не** код, который надо изобретать.
Это ровно то, что описано для `data-mcp`:

| Требование | Как реализовано сейчас |
|---|---|
| Выделенное количество подключений | `DBManager`: N потоков `_Worker`, у каждого своё psycopg2-соединение |
| Работа через очередь | `_Job`, `_submit` / `_take_job` / `_requeue` — одна очередь на все соединения |
| Одно задание на соединение | `_acquire_lease` / `_release_lease` |
| Транзакция с привязкой к вызывающему | `transaction()` берёт lease вызывающего, не возвращает его в пул |
| Backoff и деградация | `_connect_with_backoff`, `connect_max_retries`, `pool_timeout` |
| Асинхронный доступ | `_AsyncConnectionWrapper`, `async_*` |

И это **единственный** production-пул в проекте. Из 18 точек доступа к
PostgreSQL владеет соединениями одна. Запрет на дублирование не договорённость,
а тест: `tests/test_storage_hybridization.py` и `test_session_cold_sync_service.py`
проверяют, что в модулях сессий не встречаются `psycopg2.pool`,
`ThreadedConnectionPool`, `create_pool`.

**Следствие:** `data-mcp` — это перенос существующего модуля в отдельный проект
плюс MCP-поверхность поверх него. Не новая подсистема.

---

## 3. Топология

```
                     ┌──────────────────────────────────────┐
                     │  Nanobot 0.3.5  (не изменяется)      │
                     │  LLM · диалог · сессия · сжатие      │
                     │  встроенные tool'ы · MCP-клиент      │
                     └───────┬──────────────┬───────────────┘
                             │ MCP          │ MCP
              ┌──────────────┘              └──────────────┐
              ▼                                            ▼
    ┌───────────────────────┐                 ┌───────────────────────┐
    │       data-mcp        │                 │      vector-mcp       │
    ├───────────────────────┤                 ├───────────────────────┤
    │ tools/*.py            │                 │ tools/*.py            │
    │   ↓ auto-discovery    │                 │   ↓ auto-discovery    │
    │ ToolRegistry          │                 │ ToolRegistry          │
    │   query_sql (ro)      │                 │   vector_search       │
    │   log_event (write)   │                 │   list_indexes        │
    │   history_search      │                 │   index_stats         │
    │   schema_check        │                 └───────────┬───────────┘
    │   upsert_records      │                             │ читает векторы
    └───────────┬───────────┘                             ▼
                │                            ┌───────────────────────┐
                │                            │  FAISS — в памяти     │
                │                            │  без персиста          │
                │                            └───────────────────────┘
                │                            
    ┌───────────▼───────────────────────────────────────────────┐
    │  libs/  db.py (пул+очередь) · sql_safety · jsonb ·         │
    │         clean_text · document (парсинг офисных)            │
    └───────────┬───────────────────────────────────────────────┘
                ▼
    ┌───────────────────────────────────────────────────────────┐
    │                      PostgreSQL / Greenplum                │
    │  oarb.* (аудит) · public.agent_* (runtime, логи, сессии)  │
    └───────────────────────────────────────────────────────────┘

    Домены поверх libs, без собственных MCP-серверов:
      audit-analyzer   predefined-скрипты, генерация SQL
      legal-summarizer документы, чанкинг, retrieval, суммаризация
```

### 3.1 Реестр инструментов: сервер наполняется сам

`server.py` **не должен знать список инструментов заранее.** Он поднимает
реестр, отдаёт его MCP-клиенту и завершает работу. Список приходит из файлов.

```
tools/*.py
   ↓ discovery      (rglob по каталогу tools/)
   ↓ import module
   ↓ create_tool(container)
   ↓ validate
   ↓ registry.register(ToolDefinition)
   ↓ FastMCP — готово
```

**Контракт инструмента.** Tool не занимается регистрацией сам: он возвращает
описание, а превращать его в MCP-инструмент умеет реестр. Это разделение и
есть то, что делает `server.py` нечувствительным к составу инструментов.

```python
# servers/data/tools/query_sql.py
def create_tool(container):
    def query_sql(sql: str, limit: int = 100) -> str:
        return container.data.query(sql, limit)

    return ToolDefinition(
        name="query_sql",
        description="Read-only SQL к доменным и runtime-таблицам",
        handler=query_sql,
        category="read",
        version="1.0",
        enabled=True,
        tags=("sql", "read"),
        permissions=("read",),
    )
```

`ToolDefinition` — единая структура в `libs/enterprise_common`. `enabled`,
`tags` и `permissions` не влияют на загрузку; они существуют, чтобы позже
отключать инструмент без удаления файла и группировать discovery-описание.

**Добавление инструмента** = один файл + перезапуск сервера. `server.py` не
трогается. Это заменяет нынешнюю схему, где состав enterprise-tool'ов агента
задан кодом в `project_tool_loader` и в `ApplicationContext`.

**Capability самодостаточна.** Skill и инструменты лежат рядом, но не внутри
друг друга, и оба ведут к сервису:

```
capabilities/<name>/
├── skill/SKILL.md    # инструкция — Nanobot подхватывает своим механизмом Skills
├── tools/*.py        # операции — MCP
└── service/          # реализация
```

Skill не вставляется в tool-файл: у него другой жизненный цикл (его читает
агент, а не сервер) и другой транспорт. Смешивать их в одном модуле — значит
завязать MCP-сервер на формат, который читает фреймворк.

**Валидация при загрузке — и отказ целиком.** Проверяется: модуль
импортируется; `create_tool` существует; возвращается `ToolDefinition`;
`name` и `description` непустые; `name` уникален в пределах сервера;
`handler` — callable; схема аргументов валидна.

> Ошибка одного `tools/*.py` = **startup FAILED целиком**, с указанием файла и
> `name`. Частично загруженный сервер хуже мёртвого: агент считает, что
> capability существует, и падает позже, на реальном запросе пользователя,
> без трассировки до причины.

**Без hot reload.** Реестр иммутабелен в пределах процесса: добавил файл →
перезапустил сервер. Горячая перерегистрация тянет за собой смену схемы
уже опубликованного инструмента, удаление инструмента, состояние старого
handler'а и частично загруженные наборы — и решает задачу, которой у нас нет.

**Граница безопасности.** Автообнаружение — это не доверие. Каталог `tools/`
является доверенной зоной: туда попадает только код репозитория, и весь
каталог исполняется с правами процесса сервера. Отдельный манифест
`tool.json` + `entrypoint` (loader перестаёт угадывать точку входа) — это
правильное следующее усложнение, когда инструментов станет много; оно меняет
loader, а не registry, и не требует переписывания инструментов.

**Как это ложится на два сервера.** Берётся идея единого механизма, а не
обязательно один процесс: `ToolDefinition` + `ToolRegistry` + загрузчик — одна
реализация в `libs/enterprise_common`, а `data-mcp` и `vector-mcp` — два её
экземпляра со своими каталогами `tools/`. Два процесса остаются по оси
доверия: `data-mcp` пишет в PostgreSQL, `vector-mcp` держит FAISS в памяти.
Объединение их в один процесс — конфигурация загрузчика (сервер с двумя
деревьями capability), а не переписывание, поэтому решение не блокируется.

---

## 4. Классизация

### 4.1 → `data-mcp`

| Модуль | Строк | Почему |
|---|---:|---|
| `workspace/utils/db.py` | 1063 | Ядро: пул, очередь, лизы, транзакции, async |
| `workspace/utils/jsonb.py` | 52 | Декодирование JSONB — формат провода PostgreSQL |
| `workspace/utils/clean_text.py` | 44 | Чистка control-символов, потому что PostgreSQL их не принимает в `text` |
| `lib/utils/sql_safety.py` | 425 | AST-политика read-only. **Поверхность агента — read-only** |
| `workspace/tools/history_search_tool.py` | 602 | Поиск по `agent_gateway_logs` — именованная операция чтения |
| `benchmarks/db.py` | 340 | `BenchmarkDB` — именованная операция записи |
| `tools/migrate.py` | 252 | Раннер миграций схемы |
| `tools/apply_test_profile_tables.py` | 97 | Применение DDL тест-профиля |
| `lib/services/schema_validation.py` | 284 | **Остаётся в агенте.** Проверяет наличие его же runtime-таблиц; `fetch` внедряется, поэтому транспорт перенаправляется на `data-mcp` |
| `lib/utils/text_utils.py` | 96 | `sanitize_value` / `truncate_middle` — потребители это skill'ы и `history_search_tool` |
| `lib/services/db_logging_service.py` | 1142 | Пакетная запись `agent_gateway_logs` / `agent_question_runs` |

⚠️ **Масштаб, который недооценивается:** `db_logging_service.py` — не лист, а
хребет событий агента. На него ссылаются **19 файлов в `lib/`**, из них
`runtime_patcher.py` содержит 26 вызовов, а `application_context.py` — 38.
Хорошая новость: сама запись чистая — неблокирующая `queue.Queue` → один поток
воркера → батчевый INSERT, и **сервис не владеет соединением**, а берёт общее
через `run(...)`. Перенос писателя в `data-mcp` превращает горячий путь агента
в MCP-клиент на каждом событии tool'а, каждом ходе и баннере старта.
`db_logging_bus.py` при этом **остаётся в агенте**: он знает форму сообщений
nanobot, писатель — нет.

**Оговорка по `history_search_tool.py`:** его гарантия изоляции привязана к
`nanobot.agent.tools.context.RequestContext`. Если перенести SQL, не перенеся
идентичность, инструмент начнёт отдавать глобальные результаты. Изоляция по
`session_id` / `user_id` должна стать частью контракта операции, а не заботой
вызывающего. В агенте остаётся тонкий адаптер ~30 строк.

### 4.2 → `vector-mcp`

| Модуль | Строк | Почему |
|---|---:|---|
| `lib/services/vector_index_service.py` | 71 | Сборка FAISS в памяти |
| `lib/services/text_splitter.py` | 217 | Чанкинг текста перед эмбеддингом |
| `tools/build_vectors.py` | 1023 | Сборка и прогрев. **Не dev-утилита:** FAISS в памяти, пересборка обязательна на каждом старте |
| `tools/check_indexes.py` | 285 | Сверка объявленных индексов с рантаймом |
| `lib/services/cache_provider_impl.py` | 476 | **Бо́льшая часть — векторная обвязка:** `get_embedding`, `read_embedding_config`, `read_vector_index_config`, `compute/verify_index_signature`, `list_runtime_vector_indexes`. Умирает только `_capture_schema_meta` |
| `lib/services/preload_service.py` | 318 | Вся его работа — `store.preload_indexes()`, то есть сборка FAISS в памяти |
| `lib/utils/duckdb_query.py` | 338 **частично** | `build_faiss_index`, `group_vector_hits`, `build_raw_items` — **единственный в репозитории код сборки FAISS.** Извлечь ДО удаления файла |
| `lib/services/cache_provider.py` | 440 частично | Сам класс умирает, но концепции `SearchResult` и `IndexIntegrityError` векторные — переехать должны они |

⚠️ `duckdb_query.py` и `cache_provider.py` — **смешанные модули под DuckDB-этикеткой.**
Удалять их целиком нельзя: `vector-mcp` придётся заново выводить группировку
чанков и косинусную нормализацию с нуля. Сначала извлечение, потом удаление.

Конфигурация индексов переезжает как есть: `project.json::gateway.vector.index.indexes`
(по индексу: таблица, pk, content/embedding-колонки, track-колонка, chunk_size,
chunk_overlap, metric).

### 4.3 → `libs/`

| Модуль | Строк | Почему |
|---|---:|---|
| `workspace/utils/office_files.py` | 304 | Парсинг PDF/DOCX/XLSX/PPTX. Внутренняя зависимость, не capability агента |
| `workspace/skills/office_files/SKILL.md` | — | Документирует этот модуль |
| `workspace/skills/legal_summarizer/**` | ~30 000 | Доменная логика. Ноль импортов `nanobot`, ноль обращений к БД |
| `workspace/skills/audit_analyzer/**` | ~2 500 | Доменная логика поверх data/vector |

`legal_summarizer` — самый чистый актив проекта: 80 модулей и 142 тестовых файла
с **полным отсутствием** зависимостей от `nanobot`, `psycopg2` и DuckDB.
Переносится тривиально.

### 4.4 Умирает

**Кластер DuckDB** — ~2 600 строк, которые действительно умирают
(из 3 600 строк кластера; остальное переезжает в `vector-mcp`):

| Модуль | Строк | Что умирает |
|---|---:|---|
| `lib/services/duckdb_cache_store.py` | 1455 | целиком — ATTACH, блокировки, `_read_conn` на вызов |
| `lib/services/cache_load_service.py` | 472 | целиком — он существует только чтобы наполнить файл снапшота |
| `lib/services/cache_provider.py` | 440 | ABC и `open_cache_provider` для локального файла |
| `lib/utils/duckdb_query.py` | 338 | `run_query`, `explain_query`, `build_schema`, `rewrite_duck_sql` |
| `lib/services/cache_provider_impl.py` | 476 | только `_capture_schema_meta` |

**Каскадом — с оговорками:**

| Модуль | Строк | Что умирает |
|---|---:|---|
| `lib/core/skill_registration.py` | 98 | Регистрирует skill-ресурсы в `TableRegistry` |
| `lib/core/skill_config.py` | 325 | `get_in_memory_cache_path` и `build_cache_provider` |
| `lib/core/project_settings.py` | 759 | секция `CacheSettings`; остальное остаётся |
| `lib/services/runtime_health.py` | 210 | проверки компонент `duckdb_cache` и `vector_search` — перенаправить на два сервера |
| `lib/core/application_context.py` | 1816 | 88 строк кэш-обвязки из 1816: `resolve_cache_path`, `_warn_if_cache_path_on_nfs`, `_init_cache_runtime`, `check_duckdb_cache`, `check_vector_search` |
| `lib/services/__init__.py` | 8 | текст «DuckDB-кеш» в docstring устаревает |

> ✅ **Хорошая новость для шага 1:** `runtime_patcher.py` (2252 строки) не нужно
> трогать. Его единственное упоминание кэша — неиспользуемый DI-параметр
> `cache_store: Any = None` (`:583`) с комментарием «резерв для будущих патчей».

> ⚠️ **`table_registry.py` (347) НЕ умирает целиком.** Имя обманывает: это не
> маппинг PG→DuckDB, а реестр ресурсов с тремя живыми потребителями, не связанными
> со снапшотом:
> * `resources_by_label("scripts_registry")` — резолвит **таблицу PostgreSQL**
>   для `audit_analyzer` (через `skill_config.py:85-102`). Удаление сломает
>   SQL-скрипты аудита в первый же день;
> * `register_infra("vector.storage")` — регистрирует `oarb.audit_vectors`;
> * `tracking_column_for` — описывает колонку-маркер в PG.
>
> Умирает только агрегация имён в список загрузки для `CacheLoadService`
> (`application_context.py:1388`). Остальное расходится: векторная часть →
> конфиг `vector-mcp`, `scripts_registry` → `audit-analyzer`.

**Мёртвый код, обнаруженный попутно** (уже сломан, не связан с миграцией):

| Файл | Причина |
|---|---|
| `workspace/utils/structure_cache.py` | Импортирует `extract_structure`, которого **нет** в проекте. `ImportError` |
| `tools/extract_office_structure.py` | То же самое |
| `sql/vectors/create_vector_index_config.sql` | Помечен в шапке как LEGACY, кодом не читается |
| `sql/vectors/create_vector_index_store.sql` | Таблица уже удалена миграцией `V003` |
| `workspace/skills/audit_analyzer/err1.log` | Случайный артефакт в каталоге skill'а |
| `workspace/data_store/cache/**/*.py` (83 файла) | Черновые скрипты прошлых сессий |
| `lib/utils/table_utils.py` (36) | **AGENTS.md устарел:** описывает использование в `_make_sync_services` (не существует, 0 совпадений) и `tools/build_vectors.py` (не импортирует). Единственный импортёр — `tests/test_table_utils.py` |
| `lib/utils/retry.py` (70) | Два импортёра, и оба сами уходят: `cache_provider_impl.py:300`, `llm_client.py:28` |
| `project.json::gateway.vector.index.default_root` | Помечен в самом конфиге как DEPRECATED |

**Отдельные кандидаты на удаление — на ваше решение:**

| Файл | Строк | Зачем существует |
|---|---:|---|
| `tools/release_v251.py` / `release_v252.py` | 120 / 169 | Одноразовые скрипты релизов уже вышедших тегов |
| `tools/test_audit.py` | 735 | AST-аудит тестов, выгружает json. Дублирует `tests/` + CI |
| `tools/legacy_audit.py` | 484 | Сторож от регрессий легаси. Умирает вместе с рефакторингом |
| `tools/demo_internal_fallback.py` | 142 | Демо внутреннего fallback AgentLoop |
| `tools/smoke_post_cleanup.py` | 176 | Смоук под один change |
| `tools/validate_component_specs.py` | 274 | Валидация структуры `openspec/` — процессный артефакт |
| `tools/architecture_guard.py` | 78 | Сторож от трёх преждевременных абстракций |
| `workspace/hooks/debug_stream_diag.py` | 70 | Сам помечен как временный диагностический |
| `workspace/tools/example.py` | 131 | Шаблон, **но зарегистрирован как живой tool `ExampleTool`** |
| `scripts/backfill_media_aw.py` | — | Одноразовый бэкфилл media-JSONB |

### 4.5 Остаётся в агенте

| Модуль | Строк | Зачем существует |
|---|---:|---|
| `cli_agent.py` | 294 | Терминальный REPL оператора |
| `gateway.py` | 522 | Долгоживущий сервер: каналы, пул воркеров, запуск Streamlit |
| `streamlit_app.py` | 669 | Веб-интерфейс для нетехнических пользователей |
| `lib/services/runtime_patcher.py` | 2252 | Патчи AgentLoop под enterprise-поведение. **Не переносим** |
| `lib/core/application_context.py` | 1816 | Точка сборки сервисов, lifecycle |
| `lib/channels/postgres_channel.py` | 2190 | Транспорт сообщений + мультимашинная аренда задач |
| `lib/services/context_compaction.py` | 597 | Сжатие контекста — внутреннее действие агента |
| `lib/hooks/*` | 913 | Аудит tool'ов, живой вывод, per-turn логирование |
| `lib/session/pg_session_manager.py` | 137 | Холодное зеркало сессий поверх upstream |
| `lib/services/session_cold_sync_service.py` | 669 | JSONL → PG зеркало |
| `workspace/hooks/session_file_redirect_hook.py` | 417 | Не даёт агенту разбрасывать файлы по проекту |
| `workspace/hooks/recent_files_hook.py` | 125 | Прикрепляет созданные файлы к ответу пользователю |
| `workspace/utils/media.py` | 259 | Единый формат вложений для всех каналов |
| `workspace/utils/session_file_store.py` | 431 | Сессии на диске переживают рестарт |
| `workspace/utils/session_key.py` | 119 | Одна конвенция имени папки сессии |
| `workspace/tools/compact_context.py` | 177 | Агент сам сжимает свой промпт |
| `workspace/tools/legal_summarizer_query.py` | 360 | IPC-шим: запуск legal-сервиса как подпроцесса. **БД не трогает** |
| `benchmarks/*` (кроме `db.py`) | 2 331 | Замер качества ответа агента end-to-end |
| `tools/diagnose_startup.py` | 442 | Сверка стартового лога с каноническим инвентарём |
| `tools/scan_nanobot_inventory.py` | 176 | Карта зависимостей от nanobot для апгрейда |
| `tools/audit_nanobot_contracts.py` | 194 | Проверка, что импортируемые символы nanobot существуют |
| `tools/check_worker_pool_integrity.py` | 201 | Диагностика аренды задач воркерами |

### 4.6 Модули, у которых нет владельца

Аудит нашёл три модуля, которые не помещаются ни в один ящик целевой схемы.
Все три — с **нулевым** числом импортов `nanobot` и без связи со слоем данных,
то есть формально они уже независимы, но никто их не забирает.

| Модуль | Строк | Проблема |
|---|---:|---|
| `lib/services/llm_client.py` | 199 | httpx-клиент с retry для OpenAI-совместимого чата. Потребители: домены-скиллы и `benchmarks/evaluator.py`. **Кто владеет LLM-выходом после расщепления?** |
| `lib/services/llm_config.py` | 88 | Резолвит провайдера/модель/ключ из `agents.defaults` + `providers.*` |
| `lib/core/skill_config.py` | 325 | Фасад настроек skill'а. Единственные потребители — `workspace/skills/*`, которые уезжают |

Предлагаемое разрешение (нужно ваше подтверждение): **два независимых LLM-клиента.**
Агент сохраняет свой провайдер для диалога; доменные сервисы получают собственный
клиент в `libs/llm` со своей конфигурацией и своими ключами. Обратный вызов
агента через MCP невозможен — это не провайдер инструментов, а канал управления.

`skill_config.py` в этом случае теряет смысл: каждый домен читает **свой**
конфиг из конфига платформы, а не из `project.json` агента.

### 4.7 Требует вашего решения

| Вопрос | Почему нельзя решить однозначно |
|---|---|
| **Документы: сервер или tool?** | `office_files` — skill без кода, он велит агенту импортировать `office_files.py`. Убрав MCP, агент теряет доступ к офисным файлам. Варианты: (A) нативный tool агента, и `python-docx`/`openpyxl`/`pypdf` остаются в его зависимостях; (B) один инструмент на отдельном сервере, агент остаётся лёгким. Это прямое столкновение с целью «установка Nanobot не тянет enterprise-стек» |
| **`ExampleTool`** | Шаблон зарегистрирован как рабочий инструмент. Модель может его вызвать. Убрать из инвентаря или оставить? |
| **`benchmarks/`** | Меряет агента end-to-end. Если агент перестаёт быть толстым, половина бенчмарков теряет смысл |
| **Юридический `legal_summarizer_query`** | Сейчас — подпроцесс на stdio. Можно оставить как есть, можно заменить на MCP-вызов. Влияет на 360 строк vs ~30 |

---

## 5. Логирование: путь

```
хук (в процессе агента)
  └─► локальный буфер, ограниченный размером
        └─► батчевый flush ──► data-mcp: log_event
                               └─► agent_gateway_logs / agent_question_runs
```

Три обязательных условия:

1. **Не блокирует ход.** Если `data-mcp` недоступен — теряются логи, но не
   ходы. Переполнение буфера — дроп, а не backpressure.
2. **Петля не замыкается.** `data-mcp` обязан писать о своих сбоях локально
   (файл или stderr). Иначе отказ data-mcp сделает недиагностируемым ровно
   то, что сломалось.
3. **Политика хранения одна.** `logging.db.retention_days` и purge пустых
   outbound переезжают в `data-mcp` как есть.

---

## 6. Конфигурация: что умирает

| Ключ | Судьба |
|---|---|
| `gateway.cache.local_path` | **Удалить** — нечего кэшировать на диск |
| `gateway.vector.index.default_root` | **Удалить** — путь на диске |
| `gateway.vector.index.enable` / `backend` / `storage_table` | Сохраняются, переезжают в конфиг `vector-mcp` |
| `gateway.vector.index.indexes.*` | Сохраняются, переезжают в конфиг `vector-mcp` |
| `skills.*.tables` / `vector_indexes` | **Удалить** — существовали для реестра снапшота |
| `logging.db.*` | Переезжает в конфиг `data-mcp` |
| `channels.postgres.pool.*` | Переезжает в конфиг `data-mcp` (размер пула — его ответственность) |

Правки затронут `tests/test_config_keys.py` (`REQUIRED_KEYS`) и
`tests/test_docs_consistency.py`, который сейчас проверяет упоминания DuckDB
в документации.

---

## 7. Тесты

**64 из 159 тестовых файлов** затрагиваются — 40% набора.

| Куда | Что |
|---|---|
| Удалить | `test_duckdb_cache_store.py` (368), `test_cache_provider_meta.py`, `test_single_cache_interface.py` (452), `test_cache_no_file_hold.py`, `test_cache_provider_open_failure.py`, `test_cache_provider_mode.py`, `test_cache_load_service.py`, `test_table_registry.py` (531), `test_skill_cache_boundary.py`, `test_shared_cache_path_across_profiles.py` |
| Перенести в `mcp-platform` | Тесты `data-mcp`, `vector-mcp`, `libs/document` |
| Переписать | `test_application_context*` (5 файлов) — они на ~300 строк ссылаются на снимаемую подсистему |
| Обновить | `test_config_keys.py`, `test_docs_consistency.py`, `test_project_settings.py` (798) |

**Baseline надо пересобрать после удаления DuckDB, а не до.** Строка допустимых
падений (4 предсуществующих) станет другой. Чинить старые тесты удалённой
подсистемы — работа без смысла.

---

## 8. Порядок работ

> **Поправка после аудита `lib/`.** В прошлой редакции я ставил удаление DuckDB
> первым. Это неверно: код сборки FAISS живёт **внутри** DuckDB-модулей и читает
> векторы из снапшота (`duckdb_query.build_faiss_index`). Удалив DuckDB раньше
> `vector-mcp`, мы гасим векторный поиз и вынуждены отлаживать два класса отказа
> одновременно. Ниже — исправленный порядок.

| # | Шаг | Основание |
|---|---|---|
| 0 | Удалить сломанный мёртвый код (`structure_cache.py`, `extract_office_structure.py`, `table_utils.py`, `retry.py`) | Уже сломан или уже мёртв. Мешают инвентаризации, чинится за минуты |
| 1 | `libs/data` ← `workspace/utils/db.py` + `sql_safety` + `jsonb` + `clean_text` | Модуль уже чист и покрыт тестами; это перенос, а не постройка |
| 2 | `data-mcp`: MCP-поверхность над `libs/data` | Тонкий слой: схемы инструментов, маппинг ошибок |
| 3 | **`vector-mcp`**: сборка FAISS **напрямую из PostgreSQL** | Заменяет путь «снапшот → FAISS». Переносит `build_faiss_index`, `group_vector_hits`, `build_raw_items` и векторную половину `cache_provider_impl.py` |
| 4 | **Удаление DuckDB** одним коммитом, без MCP-работы | К этому моменту векторный поиск уже не зависит от снапшота. Затрагивает `lib/services/`, 2 утилиты, 88 строк в `application_context.py` и 10 тестовых модулей. Проверяемо пересборкой baseline |
| 5 | Логирование через `data-mcp` | Буфер в агенте, батчевый flush. `db_logging_bus.py` остаётся в агенте |
| 6 | `libs/document` + перенос `legal_summarizer` | Самый чистый актив, ноль связности |
| 7 | `audit-analyzer` поверх `data-mcp` / `vector-mcp` | CLI-граница уже существует. Не забыть `scripts_registry`-резолв из `TableRegistry` |
| 8 | Разделение зависимостей | Убрать офисные и векторные пакеты из `requirements.txt` агента, если не выбран вариант (A) по документам |

**Пересобрать baseline после шага 4, не до.** Строка допустимых падений (4
предсуществующих) после удаления станет другой; чинить тесты удалённой
подсистемы — работа без смысла.

---

## 9. Открытые решения

1. **Документы** — нативный tool агента (A) или отдельный сервер (B)? Определяет, выполнима ли цель «установка Nanobot не тянет enterprise-стек».
2. **Кто считает эмбеддинги** — `tools/build_vectors.py` как отдельная задача (текущий план) или часть `vector-mcp`?
3. **Границы `data-mcp`** — доступен ли read-only SQL произвольной сложности агенту, или только именованные операции? Первое проще, второе безопаснее: произвольный `ILIKE` по 24 таблицам рано или поздно даст стоимость на полкорпуса.
**Закрыто:** `agent_worker_claims`, `benchmarks/`, `streamlit`, `example.py` — см. §10.
Разделы «судьба `example.py`» и «судьба `benchmarks/`» предыдущих редакций сняты.

4. **Кто владеет LLM-выходом** — `llm_client.py` + `llm_config.py` (287 строк)
   остались без владельца. Предлагаю два независимых клиента: агент со своим
   провайдером для диалога, домены со своим в `libs/llm`. Нужно подтверждение.
5. **`schema_validation.py`** — остаётся в агенте (проверяет наличие его же
   runtime-таблиц), но ходит в БД через внедрённый адаптер. Перенаправляется
   на `data-mcp`, а не переезжает в него.
6. **Redis-канал** — снимает ли однопроцессность (решение §10.1) смысл
   `redis_channel.py` (378) и `message_exchange.py` (171)?

---

## 10. Принятые решения об удалении

### 10.1 Мультимашинная аренда задач

`agent_worker_claims` и протокол claim/lease/heartbeat/reclaim/heal удаляются.
Агент становится **однопроцессным**.

| Что уходит | Строк / объём |
|---|---|
| `lib/channels/postgres_channel.py` — протокол аренды | ~1 200 из 2 190 (оценка по 85 упоминаниям) |
| `tools/check_worker_pool_integrity.py` | 201 |
| `sql/workers/` — 4 файла | DROP-миграция |
| Настройки `channels.postgres.*` | 8 ключей: `claims_table`, `lease_interval`, `claim_strategy`, `unstick_interval`, `max_stuck_retries`, `max_concurrent`, `processing_timeout`, `error_retry_delay` |
| Тесты | ~5 файлов |
| Документация | `lib/channels/README.md`, `AGENTS.md`, `CHANGELOG.md` |

Замена в `postgres_channel`: простой опрос с `FOR UPDATE SKIP LOCKED`.
**Потеря:** отказоустойчивость уровня HA. Принимается осознанно.

### 10.2 Бенчмарки

`benchmarks/` (2 327) + `benchmarks/db.py` (340) + `tools/legal_benchmark.py` (197),
2 таблицы в `sql/benchmarks/`, ~5 тестовых файлов, секция `benchmark.*`.

Вместе уходят `tools/legacy_audit.py` (484) и `tools/test_audit.py` (735) —
процессные артефакты, не runtime.

**Потеря:** end-to-end замер качества ответа. 4 101 юнит-тест остаются, но они
проверяют компоненты, а не ответ пользователю.

### 10.3 Streamlit

`streamlit_app.py` (669) + `lib/services/subprocess_manager.py` (149, существует
**только** для его запуска) + логика spawn в `gateway.py`.

`workspace/utils/media.py` (259) сокращается — каналы ещё его читают.
Тесты: `test_streamlit_app.py` уходит, `test_profile_lifecycle.py` — частично.
Настройки `streamlit.*` удаляются.

**Потеря:** веб-интерфейс как продуктовая поверхность.

### 10.4 Итог по остатку агента

| | Строк |
|---|---:|
| Сейчас | 21 781 |
| После трёх удалений | ~16 800 |
| После замены патчей на хуки | ~15 500 |

---

## 11. Патчи: что заменяется хуками

Инвентарь 12 патчей — в `docs/architecture/runtime-patcher-inventory.md`.
Проверено, что даёт `nanobot-ai==0.3.5`:

* **`AgentHook` — 18 методов**, включая `after_execute_tool`, `on_error`,
  `on_finally`, `before_iteration`;
* **`AgentTurnHookContext.events: EventSink`** — `publish` + `accepts(type)`.
  Публикация событий из хука — штатный механизм: собственный `AgentProgressHook`
  в `nanobot` делает ровно это;
* **`AgentTurnHookContext.metadata` / `.attributes`** — мутабельные `dict`,
  передаваемые каждой `AgentTurnHookFactory`: второй канал для per-turn данных,
  чище ключей в `OutboundMessage.metadata`;
* **`finalize_content(context, content) -> str | None`** вызывается в
  `agent/runner.py` в трёх местах — **единственная** хук-точка, подменяющая
  значение;
* **`AgentTurnHookFactory = Callable[[AgentTurnHookContext], AgentHook | None]`** —
  официальная фабрика per-turn хуков, цепочка собирается в `agent/turn_hooks.py`;
* **23 события**, в том числе `TurnCompleted` (несёт `outcome`, `failure_kind`,
  `failure_error_kind`, `failure_attempts`), `TurnEndEvent`, `SessionTurnPersisted`,
  `SessionTurnStarted`, `ContextCompactionEvent`, `RecoveryStateEvent`,
  `RetryStatusEvent`, `RetryWaitEvent`.

| # | Патч | Вердикт | Чем заменяется |
|---|---|---|---|
| 10 | `turn_delivery_fail` | **подкласс** | `AgentLoop(turn_delivery_factory=...)` — публичный параметр, валидация только по шине, а конструкций `TurnDelivery(` в пакете две и обе в фабрике. Подкласс `TurnDeliveryFactory` покрывает 100%. Инжектить в gateway и CLI, сохранив `WebuiTurnRoutePolicy` |
| 2 | `save_turn` | **удалить** | Следствие патча 1: результат персистится в момент возврата tool'а, усечение в `_save_turn` не теряет оригинал. Upstream санитайзит через `_sanitize_persisted_blocks` |
| 7 | `async_save` | **нашим классом** | `agent.sessions` — наш `PGSessionManager`. Обёртка на `ThreadPoolExecutor` делается при создании в `session_storage.py` |
| 11 | `session_content_cleanup` | **нашим классом** | Чистка NUL — забота PostgreSQL; `clean_text.py` уже делает это и уезжает в `libs/data` |
| 8 | `session_dir_watch` | **удалить** | Гейт выключен по умолчанию, тестов нет |
| 1 | `context_governor` | **удалить** | **Upstream уже работает.** `workspace` и `max_tool_result_chars` прокинуты: `config/schema.py` → `AgentLoop` → `AgentRunSpec` → `ContextGovernanceConfig` → `maybe_persist_tool_result`. Патч переписывал работающую функцию |
| 4 | `exec_timeout_cap` | **удалить** | `tools.exec.timeout` уже прокинут (`0` = без лимита); остаток — подкласс `ExecTool`. Обоснование «legal 7–10 мин» отпадает с переездом legal в MCP |
| 5 | `tool_limits` | **частично** | 3 из 5 целей читаются как `self.<attr>` → подкласс `Tool` под тем же именем. `search._DEFAULT_HEAD_LIMIT` и `_DEFAULT_FILE_HEAD_LIMIT` — голые глобалы, не перехватываются, но они лишь дефолты: per-call `head_limit` есть |
| 6 | `assemble_outbound` | **частично** | `_final_turn` → `TurnEndEvent`. Хук на стадии `run` с `await bus.publish` гарантированно раньше outbound. `media` и `_tool_audit` — решение не принято: `media` никогда не заполняется nanobot, штатный слот `_agent_ui` пишется только на этапе сборки |
| 9 | `subagent_logging` | **остаётся** | Блокер структурный: у `SubagentManager` нет параметра хука и он зашит; субагент зовёт `runner.run` напрямую, минуя цепочку хуков; `events` не передаётся → `NO_EVENTS` → ноль событий |
| 3 | `exec_limits` | **остаётся** | Потолок в глобалах модуля внутри `clamp_session_int`, а `maximum` схемы заморожен `deepcopy` при декорации — подкласс не достаёт. Нужен контрактный тест на инварианты |
| 12 | `document_text_threshold` | **уходит с документами** | Логика документная; переезжает вместе с решением по документам |

**Итог: 12 → 2 полностью необходимых + 2 частичных.** Семь патчей удаляются без замены или подклассом, и двое из них (`context_governor`, `save_turn`) оказались переписыванием работающей upstream-функции.

Полная доказательная база — `docs/architecture/nanobot-reuse-catalog.md`; перед добавлением патча каталог просматривается обязательно.

`subagent_logging` — единственный оставшийся патч с **структурным** блокером:
`_SubagentHook` конструируется жёстко, фабрики для subagent'ов нет, то есть
точка вставки отсутствует физически, а не из-за слабости хука.

### Что хук может, а что — нет

| Действие | Чем делается |
|---|---|
| наблюдение вызовов, параметров, результатов, ошибок | любой метод `AgentHook` |
| запись в БД, файлы, счётчики | побочный эффект в хуке |
| публикация события | `await turn_context.events.publish(...)` |
| передача per-turn данных | мутабельные `turn_context.metadata` / `.attributes` |
| подмена финального контента | `finalize_content` — возвращает значение |
| **подмена результата tool'а** | **только инструментом или патчем** |

Последняя строка — единственное ограничение, и в 0.3.5 оно ровно одно:
`after_execute_tool` возвращает `None`, а `execution.py` делает `return result`.

### Пять правил вместо патча

1. **Патчить наше, а не фреймворк.** Если поведение правится в
   `PGSessionManager`, патч не нужен — правь наш класс.
2. **Событие вместо инъекции в `OutboundMessage.metadata`.** Хук публикует
   событие через `turn_context.events`, потребитель подписывается. Сработало
   для `compact_tracking`, теперь то же для `_final_turn`, `_tool_audit`, `media`.
3. **Прямого запрета на хуки нет.** Проверять надо не наличие хука, а может ли
   он повлиять **именно на то, что нужно изменить**.
4. **Проверять наличие конфига до патча константы.** Для exec в 0.3.5 конфига нет.
5. **Контрактные тесты на целевой API** уже есть в `tests/contract/`.

### Что добавить в инвентарь

В `docs/architecture/runtime-patcher-inventory.md` ввести колонку
**«при каком условии исчезнет»** и проставить её для всех двенадцати. Тогда
патчи перестают быть рентой «на когда-нибудь»: у каждого есть срок, завязанный
на шаг миграции.
