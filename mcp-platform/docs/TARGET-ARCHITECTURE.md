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
    │  query_sql (ro)       │                 │  vector_search        │
    │  log_event (write)    │                 │  list_indexes         │
    │  history_search       │                 │  index_stats          │
    │  schema_check         │                 └───────────┬───────────┘
    │  upsert_records       │                             │ читает векторы
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
| `lib/services/schema_validation.py` | 284 | Проверка наличия runtime-таблиц → health-операция |
| `lib/services/db_logging_service.py` | 1142 | Пакетная запись `agent_gateway_logs` / `agent_question_runs` |
| `lib/services/db_logging_bus.py` | 166 | Шина событий логирования |

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

**Кластер DuckDB** — 3 181 строка production-кода:

| Модуль | Строк |
|---|---:|
| `lib/services/duckdb_cache_store.py` | 1455 |
| `lib/services/cache_provider_impl.py` | 476 |
| `lib/services/cache_load_service.py` | 472 |
| `lib/services/cache_provider.py` | 440 |
| `lib/utils/duckdb_query.py` | 338 |

**Каскадом за ними:**

| Модуль | Строк | Почему умирает |
|---|---:|---|
| `lib/services/table_registry.py` | 347 | Единственная задача — перечислить таблицы для снапшота |
| `lib/core/skill_registration.py` | 98 | Регистрирует skill-ресурсы в `TableRegistry` |
| `lib/core/infra_registration.py` | 54 | Регистрирует `oarb.audit_vectors` как хранилище для снапшота |
| `lib/services/preload_service.py` | 318 | Прогрев DuckDB-кэша и FAISS; остаётся только прогрев FAISS |
| `lib/core/skill_config.py` | 325 | `get_in_memory_cache_path` и прочий cache-API уходят |

**Мёртвый код, обнаруженный попутно** (уже сломан, не связан с миграцией):

| Файл | Причина |
|---|---|
| `workspace/utils/structure_cache.py` | Импортирует `extract_structure`, которого **нет** в проекте. `ImportError` |
| `tools/extract_office_structure.py` | То же самое |
| `sql/vectors/create_vector_index_config.sql` | Помечен в шапке как LEGACY, кодом не читается |
| `sql/vectors/create_vector_index_store.sql` | Таблица уже удалена миграцией `V003` |
| `workspace/skills/audit_analyzer/err1.log` | Случайный артефакт в каталоге skill'а |
| `workspace/data_store/cache/**/*.py` (83 файла) | Черновые скрипты прошлых сессий |

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

### 4.6 Требует вашего решения

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

Отличается от исходного плана: риск лежит не в доменах, а в удалении DuckDB.

| # | Шаг | Основание |
|---|---|---|
| 0 | Удалить сломанный мёртвый код (`structure_cache.py`, `extract_office_structure.py`) | Мешает инвентаризации, чинится за минуту |
| 1 | **Атомарное удаление DuckDB** одним коммитом, без MCP-работы | Затрагивает `lib/services/`, 2 утилиты, 1 skill и 10 тестовых модулей. Самостоятельно проверяемо пересборкой baseline |
| 2 | `libs/data` ← `workspace/utils/db.py` + `sql_safety` + `jsonb` + `clean_text` | Модуль уже чист и покрыт тестами; это перенос |
| 3 | `data-mcp`: MCP-поверхность над `libs/data` | Тонкий слой: схемы инструментов, маппинг ошибок |
| 4 | Логирование через `data-mcp` | Буфер в агенте, батчевый flush |
| 5 | `vector-mcp` ← `vector_index_service` + `build_vectors` + `check_indexes` | FAISS в памяти, векторы из PG |
| 6 | `libs/document` + перенос `legal_summarizer` | Самый чистый актив, ноль связности |
| 7 | `audit_analyzer` поверх `data-mcp` / `vector-mcp` | CLI-граница уже существует |
| 8 | Разделение зависимостей | Убрать офисные и векторные пакеты из `requirements.txt` агента, если не выбран вариант (A) по документам |

Шаг 1 идёт **первым**, хотя в прошлой редакции документа он был последним:
чем дольше живёт DuckDB, тем больше кода завязано на него.

---

## 9. Открытые решения

1. **Документы** — нативный tool агента (A) или отдельный сервер (B)? Определяет, выполнима ли цель «установка Nanobot не тянет enterprise-стек».
2. **Кто считает эмбеддинги** — `tools/build_vectors.py` как отдельная задача (текущий план) или часть `vector-mcp`?
3. **Границы `data-mcp`** — доступен ли read-only SQL произвольной сложности агенту, или только именованные операции? Первое проще, второе безопаснее: произвольный `ILIKE` по 24 таблицам рано или поздно даст стоимость на полкорпуса.
4. **Судьба `example.py` / dead tools** — удалять или хранить.
5. **Судьба `benchmarks/`** — остаются в агенте или уезжают.
