# Разработка nanobot: audit_analyzer и универсальный слой данных

> **Назначение:** техническая документация для разработчиков. Описывает архитектуру
> навыка `audit_analyzer`, универсальный инфраструктурный слой данных
> (`lib/services`), а также **v2.0.0 bootstrap-слой** (`lib/core/ApplicationContext`,
> `lib/lifecycle/`, `lib/cli/`, `lib/services/DbLoggingService`/`RuntimePatcher`/...)
> — общий для `gateway.py` и `cli_agent.py`. Управление DuckDB-кешем,
> векторными индексами и SQL-скрипты для развёртывания нужных таблиц.
> Пользовательская документация навыка — [`workspace/skills/audit_analyzer/SKILL.md`](workspace/skills/audit_analyzer/SKILL.md).
> Обзор проекта — [`README.md`](README.md).

---

## 📋 Оглавление

1. [Архитектура](#архитектура)
2. [Сервисный слой v2.0.0 (ApplicationContext + lib/)](#сервисный-слой-v200-applicationcontext--lib)
3. [Структура проекта](#структура-проекта)
4. [Универсальный слой данных lib/services](#универсальный-слой-данных-libservices)
5. [Конфигурация навыка](#конфигурация-навыка)
6. [CLI навыка: режимы](#cli-навыка-режимы)
7. [Жизненный цикл кеша](#жизненный-цикл-кеша)
   - [Управление синхронизацией](#управление-синхронизацией)
8. [Векторная индексация](#векторная-индексация)
9. [SQL-скрипты: создание таблиц](#sql-скрипты-создание-таблиц)
10. [Полная таблица связей между файлами (v2.0.0)](#полная-таблица-связей-между-файлами-v200)
11. [Тестирование](#тестирование)
12. [Изменения и миграции](#изменения-и-миграции)
13. [Конфигурация `tools.exec` (запуск команд)](#конфигурация-tools-exec)

---

## 🏗 Архитектура

Инфраструктура (DuckDB-кеш, векторные индексы, эмбеддинги) вынесена из навыка
в **универсальный слой** `lib/services` — он не завязан на предметную область
«аудит» и может переиспользоваться любым навыком. Навык `audit_analyzer` остался
тонким CLI: он конфигурирует провайдера из своих настроек и работает с ним
напрямую (без промежуточных обёрток-шимов).

```mermaid
flowchart LR
    subgraph PG["PostgreSQL (канон)"]
        AUDITS["oarb.audits<br/>oarb.violations<br/>oarb.audit_reports<br/>oarb.report_items"]
        VECTORS["oarb.audit_vectors"]
        STORE["public.agent_vector_index_store<br/>(FAISS в BYTEA)"]
        CONFIG["public.agent_vector_index_config"]
    end

    subgraph SERVICES["lib/services (универсальный слой данных)"]
        SYNC["AuditSyncService<br/>(worker-поток,<br/>SQL через общий пул)"]
        STORE_SVC["AuditMemoryStore<br/>(in-memory DuckDB+FAISS)"]
        PROV["PostgresDuckDbProvider<br/>(CacheProvider)"]
        EMBED["get_embedding<br/>(Ollama /api/embed)"]
    end

    subgraph ARTIFACT["Файл кеша навыка"]
        DUCK["cache/audit_cache.duckdb<br/>(снимок таблиц)"]
    end

    AUDITS -->|"поллинг<br/>(incremental)"| SYNC
    VECTORS --> SYNC
    SYNC -->|"upsert_records<br/>(batch)"| STORE_SVC
    SYNC -->|"after sync"| STORE_SVC
    STORE_SVC -->|"publish()<br/>temp+os.replace"| DUCK
    VECTORS -->|"при промахе индекса"| STORE
    PROV -->|"query_sql/explain"| AUDITS
    PROV -->|"search_vector"| STORE
    PROV -->|"get_embedding"| EMBED

    GATEWAY["gateway.py<br/>(владелец кеша)<br/>AuditSyncService →<br/>AuditMemoryStore →<br/>publish()"]
    CLI["навык CLI<br/>(только чтение)"]

    GATEWAY --> SYNC
    GATEWAY --> STORE_SVC
    CLI --> DUCK
    CLI --> PROV

    classDef service fill:#d1ecf1,stroke:#0c5460,stroke-width:2px
    classDef owner fill:#fff3cd,stroke:#d39e00,stroke-width:2px
    classDef consumer fill:#f8d7da,stroke:#c82333
    class SYNC,STORE_SVC service
    class GATEWAY owner
    class CLI consumer
```

**Потоки данных**

- `gateway.py` — единственный владелец файла кеша навыка. `AuditSyncService`
  (worker-поток, единственное подключение к PG) инкрементально синхронизирует
  таблицы в `AuditMemoryStore` (чисто in-memory DuckDB), а после каждого цикла
  `store.publish()` атомарно записывает снимок (temp + `os.replace`) в файл
  кеша навыка (`in_memory_cache_path`).
- Навык CLI (`predefined`/`sql`) — запросы выполняются по опубликованному кешу
  (или напрямую по PG, если кеш выключен). Создание/обновление кеша его не
  касается. Единый интерфейс бэкенда: `get_schema / query_sql / explain`.
- `--mode vector` — семантический поиск по FAISS-индексу: провайдер загружает
  индекс из `public.agent_vector_index_store` (BYTEA), при промахе пересобирает из
  `oarb.audit_vectors`, эмбеддинг запроса получает через Ollama.

---

## 🆕 Сервисный слой v2.0.0 (ApplicationContext + lib/)

В v2.0.0 gateway и cli_agent сократились с 696/865 до 132/165 строк за счёт
вынесения всей инициализации в `ApplicationContext` (см. подробности в
`README.md` v2.0.0 changelog). Этот раздел — про
**внутреннее устройство** нового слоя, нужно при добавлении новых
сервисов или изменении lifecycle.

### Точки входа → общий bootstrap

```mermaid
flowchart TB
    GW["gateway.py<br/>232 строки<br/>(тонкий оркестратор)"]
    CLI["cli_agent.py<br/>165 строк<br/>(тонкий оркестратор)"]
    CTX["lib/core/ApplicationContext<br/>(create/start/stop)"]

    GW -->|"create(...)"| CTX
    CLI -->|"create(...)"| CTX

    CTX --> CFG_SVC["ConfigService<br/>(config.json, SETTINGS, pre-resolve env)"]
    CTX --> SESS["SessionStorageService<br/>(PGSessionManager / SessionManager)"]
    CTX --> DB_LOG["DbLoggingService<br/>(worker, batch INSERT, без JSONL-fallback)"]
    CTX --> AUDIT["AuditSyncService + AuditMemoryStore<br/>(audit_analyzer)"]
    CTX --> BUS["MessageBus<br/>(через BusFactory, с обёрткой под логгеры)"]
    CTX --> AGENT["AgentLoop<br/>(через AgentFactory,<br/>hooks=[ToolAudit],<br/>hook_factories=[DbLogging per-turn])"]
    CTX --> PATCHER["RuntimePatcher<br/>(ContextGovernor + _assemble_outbound)"]
    CTX --> PRELOAD["PreloadService<br/>(FAISS / audit_cache)"]
    CTX --> TRANS["TranscriptionService"]

    classDef bootstrap fill:#fff3cd,stroke:#d39e00,stroke-width:2px
    classDef entry fill:#d1ecf1,stroke:#0c5460,stroke-width:2px
    class CTX bootstrap
    class GW,CLI entry
```

### `lib/core/` (ApplicationContext + фабрики)

- **`application_context.py:ApplicationContext`** — единственный класс,
  собирающий все общие сервисы. Поля (помимо путей и конфига):
  `bus`, `agent`, `tool_audit_hook`, `hooks`, `session_manager`,
  `storage_mode`, `db_logging_service`, `audit_sync_service`,
  `audit_memory_store`, `config_service`, `runtime_patcher`,
  `transcription_service`, `subprocess_manager`, `preload_service`.
  Метод `start()` использует `ShutdownCoordinator` для регистрации
  сервисов; `stop()` — LIFO graceful shutdown.
  **Graceful degradation:** если БД недоступна, сервис остаётся `None`,
  gateway/cli работают без него (с предупреждением в логах).
- **`agent_factory.py:AgentFactory`** — `create(config, bus, session_manager=...,
  cron_service=..., db_logging_service=..., project_hooks=...)` → `(agent, hooks,
  hook_factories)`. `project_hooks` (плагины `workspace/hooks/`) ставятся
  ПЕРЕД `ToolAuditHook` в `hooks=`; агент создаётся ровно один раз.
  `DatabaseLoggingHook` подключается НЕ как общий инстанс, а как фабрика
  оборота (`make_db_logging_hook_factory` в `hook_factories=`) — фреймворк
  создаёт свежий инстанс на каждый оборот, изолируя состояние вопроса между
  конкурентными сессиями (см. `database_logging_hook.py`).
  Lazy-import `lib.hooks.database_logging_hook` через try/except
  (если модуль не подключён — фабрика просто не создаётся).
  Флаг `print_llm_calls` пробрасывается в фабрику оборота: при `True`
  хук печатает в терминал токены каждой LLM-итерации (включается всегда
  в CLI-режиме через `cli_agent.py`; в gateway — опцией
  `gateway.print_llm_calls`).
- **`bus_factory.py:BusFactory`** — `create()` возвращает `MessageBus`,
  опционально обернув `publish_inbound`/`publish_outbound` async-логгерами
  из `db_logging_bus.py`. **Без monkey-patch'ей**: оригинальные методы
  шины сохраняются в замыкании.

### `lib/services/` (новые сервисы v2.0.0 + старые audit/кэш)

Полный список модулей (новые и pre-existing) — см. раздел [«Полная таблица связей»](#полная-таблица-связей-между-файлами-v200) ниже. Здесь — только
**новые** (v2.0.0), с краткой мотивацией:

| Сервис | Мотивация (почему выделен) |
|--------|---------------------------|
| `config_service.py` | Дубликат `_load_runtime_config` + `SETTINGS`-аксессора между gateway и cli. Pre-resolve `${PROVIDER_API_KEY}` от .secrets.env (см. ниже). |
| `session_storage.py` | Выбор `PGSessionManager` / `SessionManager` (auto / postgres / file) с поддержкой `session_manager.json` override. |
| `runtime_patcher.py` | Все monkey-patch'и фреймворка в одном классе с fallback при изменении API nanobot: `ContextGovernor.normalize_tool_result` (persist больших результатов), `AgentLoop._save_turn` (архивация вместо усечения, см. «Ликвидация потери данных»), ограничения вывода exec/tool (`patch_exec_limits`/`patch_tool_limits`), `agent._assemble_outbound` (внедрение `_tool_audit`). |
| `channel_factory.py` | `ChannelManager` + Redis + Postgres каналы + транскрипция (вынесено из gateway). Конструктор принимает `print_worker_activity` (пробрасывается в `PostgresChannel` из `gateway.print_worker_activity`). |
| `transcription_service.py` | openai/groq key/URL/language (вынесено из gateway). |
| `subprocess_manager.py` | Streamlit spawn + terminate/kill. |
| `preload_service.py` | Разделяет FAISS preload (gateway) и audit_cache refresh (cli). |
| `db_logging_service.py` | **Новый** — структурированный журнал агента в `gateway_logs`. |
| `db_logging_bus.py` | **Новый** — обёртки `publish_inbound`/`publish_outbound` для `DbLoggingService`. |

### Pre-resolve `${VAR}` от `.secrets.env`

`nanobot._load_runtime_config` резолвит `${LLM_API_KEY}` только из
`os.environ` и при отсутствии падает `ValueError`. Между тем `config.py`
для провайдерских ключей использует провайдер-скоупинг формат
`.secrets.env`:

```
# providers: llm
api_key=XavGPsHjtNt3uOtFGUhabUuad5PRm2D0W
```

— в `os.environ` это не попадает как `LLM_API_KEY`. Решение
(`ConfigService._pre_resolve_env_refs`): прочитать `config.json`,
найти `${VAR}` плейсхолдеры, для каждого `*_API_KEY` без env — достать
ключ из `SETTINGS.providers.<любой>.api_key` (туда `config.py` уже
подставил значение) и положить в `os.environ` ДО `_load_runtime_config`.
**Gateway больше НЕ требует `export LLM_API_KEY=...` в shell.**

> **Миграция с исторического имени:** в старых конфигах `.secrets.env` мог
> быть `# providers: mistral`. Логика остаётся обратно совместимой: ключ
> из любой непустой секции `providers.*` подставляется как `LLM_API_KEY`.
> Достаточно переименовать секцию в `# providers: llm` для ясности.

### Единый резолв LLM-конфигурации: `lib/services/llm_config.py`

Резолв провайдера/модели/ключа вынесен в общий модуль
`lib/services/llm_config.py::resolve_llm_config()`: дефолт берётся из
`agents.defaults` (модель/провайдер) и `providers.<provider>`
(`apiBase`/`apiKey`) уже-резолвнутых `SETTINGS`, переопределения
(например, `skills.audit_analyzer.llm_*`) передаются через `overrides`.

Используется единообразно:
* навыком `audit_analyzer` — `scripts/skill_config.py::get_llm_config()`;
* бенчмарками — `benchmarks/runner.py::_run_suite()` (без хардкода
  провайдера при `--model`; `ensure_llm_env()` гарантирует
  `LLM_API_KEY` в env для резолва `${LLM_API_KEY}`).

Так смена модели/провайдера/ключа агента автоматически меняет LLM и в
навыке, и в бенчмарке — без дублирования секретов в трёх местах.

### Race-condition fix: callbacks ДО `ctx.start()`

`AuditSyncService` — worker-поток, который делает `initial_load` сразу
после `start()`. Если `set_on_new_records_callback(upsert_records)` ещё
не вызван к этому моменту, `_dispatch` скипает записи → DuckDB остаётся
пустым → `preload_vector_indexes` видит "нет данных" несмотря на
данные в `oarb.audit_vectors`. **Fix:** в `gateway.py:main()` callbacks
устанавливаются **ДО** `ctx.start()`. Тогда worker-тред при первом
`_do_initial_load` уже видит настроенные callbacks → `upsert_records`
вызывается → FAISS preload находит данные.

### Конкурентно-безопасное БД-логирование: per-turn инстанс `DatabaseLoggingHook`

Обороты разных сессий (вопросов) обрабатываются конкурентно
(`AgentLoop._dispatch` → `_concurrency_gate`, дефолт
`NANOBOT_MAX_CONCURRENT_REQUESTS=3`). Проблема: фреймворковый
`AgentRunHookContext` (для `after_run`) **не содержит `session_key`** —
контекст вопроса приходится хранить в самом хуке.

**Исторический баг:** один общий инстанс `DatabaseLoggingHook` на все
сессии, контекст в плоских полях (`_run_session_key`/`_request_id`).
При конкурентном переплетении оборотов чужой вопрос перезаписывал
поля между `before_`/`after_execute_tool`, и:
  * `log_tool_result` / `run_finished` получали request_id чужого вопроса;
  * `after_run` мог `finish_request`/`clear_request` чужую сессию.

**Решение — фабрика оборота.** `DatabaseLoggingHook` создаётся на
КАЖДЫЙ оборот через `make_db_logging_hook_factory(db_logging_service,
agent_id)` (`lib/hooks/database_logging_hook.py`). Фабрика получает
`AgentTurnHookContext` (там есть `session_key`), резолвит `request_id`
через `service.get_request_id(session_key)` и запекает оба значения в
конструкторе — состояние вопроса изолировано между сессиями, гонки нет.

`AgentFactory.create` передаёт фабрику в `AgentLoop` через
`hook_factories=[...]` (а не общий инстанс в `hooks=...`). `ToolAuditHook`
остаётся в `hooks=` — он bucket-безопасен по `session_key`.

`RuntimePatcher.patch_subagent_logging` использует тот же паттерн:
каждый `_SubagentLoggingHook` создаёт СВОЙ `_db_hook` на запуск
подагента (class-level shared `_db_hook` давал ту же гонку между
конкурентными субагентами).

Регрессионный тест: `tests/test_hooks_database_logging.py →
TestDatabaseLoggingHookFactory.test_concurrent_sessions_do_not_mix_request_id`
(переплетение двух сессий → `log_tool_result`/`after_run` несут свой
`request_id`).

### Ликвидация потери данных при усечении больших результатов инструментов

**Проблема.** Результаты больших инструментов терялись на нескольких уровнях:

1. **exec/shell**: nanobot режет вывод команды до
   `MAX_OUTPUT_CHARS = 50K` символов и вставляет маркер
   `... (19,761 chars truncated) ...` (`nanobot/agent/tools/shell.py`,
   `exec_session.py`), отбрасывая середину. Persist потом сохраняет
   «голову+хвост» — данные теряются безвозвратно.
2. **История сессии**: `AgentLoop._save_turn` усекает строковый результат
   инструмента до `max_tool_result_chars = 16K` символов, если результат не
   ушёл в persist раньше (в первую очередь это `read_file`).
3. **Вторичные инструменты** (`read_file`/`grep`/`list_dir`) имеют свои
   потолки с маркерами `truncated`.

**Решение (все уровни закрыты патчами `RuntimePatcher`):**

| Патч | Что делает |
|------|-----------|
| `patch_exec_limits` | Поднимает `MAX_OUTPUT_CHARS` (дефолт 500K), `DEFAULT_MAX_OUTPUT_CHARS` (100K), `ExecTool._MAX_OUTPUT`, а также подменяет `maximum` в JSON-Schema параметра `max_output_chars`/`max_output_tokens` (модель может запросить вывод >50K). Безопасно для контекста: вывод exec не exempt в persist → >`persist_threshold` уходит полным файлом в `data_store`, в контекст — ссылка. |
| `patch_tool_limits` | Поднимает `ReadFileTool._MAX_CHARS` (512K), `search._DEFAULT_HEAD_LIMIT` / `_DEFAULT_FILE_HEAD_LIMIT` (500/400), `GrepTool._MAX_FILE_BYTES` (20MB), `ListDirTool._DEFAULT_MAX` (500). |
| `patch_save_turn` | Оборачивает `AgentLoop._save_turn`: любой большой результат `role == "tool"` (строка или JSON-сериализуемый список) пишется **полным** файлом в `data_store` через `SessionFileStore` (суффикс `__<hash>` — dedupe), в историю кладётся ссылка `[Result saved to data_store/<path> (<size> KB)]` — тот же формат, что кастомный persist. Оригинальный `_save_turn` вызывается с копией сообщений, логика nanobot не дублируется. |
| `save(..., dedupe=True)` | Новый параметр `SessionFileStore.save`: повторное сохранение того же содержимого (sha1, первые 12 hex) возвращает уже существующий файл (`deduped=True`), чтобы повторные/конкурентные обороты не плодили копии. |

**Конфигурация** — `gateway.tool_result_limits` в `project.json`
(все ключи опциональны, дефолты в коде):

```jsonc
"tool_result_limits": {
  "exec_max_output_chars": 500000,
  "exec_default_output_chars": 100000,
  "read_file_max_chars": 512000,
  "grep_head_limit": 500,
  "grep_file_head_limit": 400,
  "grep_max_file_bytes": 20000000,
  "list_dir_max_entries": 500
}
```

**Fallback-поведение**: каждый патч в try/except; при изменении API nanobot —
`(False, <причина>)` в `PatchReport`, процесс не падает. `_save_turn`-обёртка
при `OSError` в persist грейсит — вызывает оригинал (поведение «как раньше»).
`read_file` остаётся exempt в `normalize_tool_result` (избегаем
read→persist→read петли).

**Реальные проверки** — `tests/test_runtime_patcher_e2e.py` (9 тестов):
настоящий `ExecTool` исполняет команду с выводом >лимита и проверяет
отсутствие маркера `chars truncated` после патча; настоящий `ReadFileTool`
читает файл >дефолтного потолка целиком; `_save_turn`-обёртка и
`ContextGovernor.normalize_tool_result` пишут **полные** файлы на диск в
`data_store/` и подменяют историю ссылкой. Каждый тест сам восстанавливает
состояние фреймворка в `finally`.

---

## 🆕 Сервисный слой v2.3.0 (MessageExchange + LLM-клиент + утилиты)

v2.3.0 добавляет поверх сервисного слоя v2.0.0 набор общих модулей, чтобы
устранить дрейф поведения между каналами, навыками и CLI-инструментами.

### `lib/channels/message_exchange.py` — общий движок каналов

`MessageExchange` — единая точка кодирования/декодирования `InboundMessage` /
`OutboundMessage`, поллинга и публикации outbound, фильтрации служебных
сообщений. `PostgresChannel` и `RedisChannel` — тонкие обёртки над ним,
`streamlit_app.py` использует тот же движок для чтения истории. Запрещено
дублировать логику polling/encoding в новых каналах — только через
`MessageExchange`.

Зависимости модуля:
- `lib/utils/media.py` — кодек media (AW-формат `{filename, file_id, mime_type,
  file_size}` + обратная совместимость со старым `{filename, data}` и
  data-URL).
- `lib/utils/media_jsonb.py` — JSONB-декодер media для PG.
- `lib/utils/outbound_filter.py` — единый фильтр служебных outbound
  (`system`, `audit`, `tool_audit`, `_assemble_outbound`-артефакты).
- `SessionFileStore` (`lib/utils/session_file_store.py`) — общий стор
  вложений под `data_store/cache/sessions/<key>/attachments/`.

При добавлении нового канала: наследовать `nanobot.channels.base.BaseChannel`
и делегировать `start/stop/send/send_delta/poll_once` в `MessageExchange`.
Подробнее — [`lib/channels/README.md`](lib/channels/README.md).

### Мульти-машинный пул воркеров (аренда задач через `agent_worker_claims`)

`PostgresChannel` разворачивается на нескольких машинах как полный gateway,
читающий одну таблицу `public.agent_conversation_messages`. Чтобы одна задача
(сообщение веб-чата) физически не обрабатывалась двумя воркерами, введена
таблица аренд `public.agent_worker_claims` (`sql/workers/`, DDL — см.
`sql/README.md`). Эксклюзивность гарантирует **UNIQUE PK `(task_id)`, а не
MVCC/`UPDATE ... WHERE status='pending'`**: два `INSERT` с одним `task_id`
невозможны, второй падает на unique-индексе.

**Инвариант:** `status='processing' ⇔ существует ровно одна claim-запись с
живым lease` для этого `task_id`.

**Статусы задач:**

| Статус | Значение | Повторяется? |
|---|---|---|
| `pending` | готова к обработке | да, сразу |
| `processing` | в работе воркера (держит claim) | нет — защищена lease |
| `error` | повторяемая ошибка | да, после `error_retry_delay` |
| `failed` | терминальный (не меняется) | нет — окончательно |
| `completed` | успешно завершена | — |

`error` и `failed` разведены: `error` (retry-каунтер в `metadata.retry_count`
не исчерпан) возвращается в пул после паузы; `failed` — immutable-терминал.
Раньше обе ситуации сводились к `failed`, и web-клиент ждал оконное время на
«появление в работе», которое могло никогда не наступить.

**Клейм (`_claim_one`) — одна транзакция:**
1. `SELECT` самого старого кандидата (`pending` или `error` с истёкшим
   backoff) без активного claim и из чата без активной `user`-задачи
   (`status='processing'`);
2. `INSERT INTO agent_worker_claims ...` — арбитр эксклюзивности; при
   `UniqueViolation` транзакция откатывается и выбирается следующий кандидат;
3. `UPDATE messages SET status='processing'` — владелец задачи живёт только
   в `agent_worker_claims.worker_id`, колонка в сообщениях не используется.

**Lease и heartbeat:** срок аренды = `processing_timeout`. Фоновая задача
`_lease_loop` каждые `lease_interval` продлевает `lease_until` своим арендам и
запускает `_reclaim_and_heal`.

**`_reclaim_and_heal` (одна транзакция):**
1. `DELETE FROM claims WHERE lease_until < NOW()` — «мёртвый» воркер
   освобождает задачи: задача → `pending` (или `failed` при исчерпании
   `max_stuck_retries`), assistant-placeholder удаляется;
2. `processing`-без-claim → `error` (аномалия, повторится после backoff);
3. orphaned assistant-placeholder (без user-пары) → `failed`;
4. висячая аренда (claim есть, а задача не в `processing`) — удаляется.

**Освобождение аренды:** `send()` (только на финальном outbound) /
`send_delta(stream_end)` / `_mark_failed` удаляют claim в той же транзакции,
что и запись `completed`/`error`/`failed`. `stop()` возвращает незавершённые
задачи в пул (`_release_all_leases`).

**Промежуточные публикации тула `message(...)` vs финал оборота.**
`MessageTool` в nanobot публикует свой outbound через шину **в момент
исполнения тула**, т.е. до завершения оборота. `send()` финализирует оборот
(claim + слот + `_msg_ctx`) **только** на маркере `metadata["_final_turn"]`
(или legacy `_turn_end` / `latency_ms`), который ставит патч
`RuntimePatcher.patch_assemble_outbound` (при подавленном финале — синтетическим
outbound). Все остальные сообщения `send()` merge'ит в assistant-строку
(`_merge_tool_delivery`): накопление `content` + media без дублей, status
остаётся `processing`, слот/claim/аренда не трогаются. Это исключает ситуацию,
когда оборот «завершался» на промежуточной публикации, а затем уходил в
`failed` через reclaim. Маркер объявлен в `lib/utils/outbound_meta.py` как
`FINAL_TURN_KEY` (не входит в `OUTBOUND_DROPPED_KEYS`, чтобы потоковые каналы
вроде Redis передавали финальный ответ как обычно). `_release_slot` в
финализации вызывается после успешной записи, а не до неё — иначе задача
снималась с heartbeat, пока claim ещё жив, и другой воркер мог её reclaim-нуть.

**Конфиг (`channels.postgres`):** `worker_id` (пусто → авто
`{hostname}:{pid}:{rand8}`, идентификация воркера в claims), `claims_table`,
`lease_interval`, `error_retry_delay`. **`streamlit.error_window_sec`** — окно
ожидания повтора `error`-задач (быв. `failed_window_sec`).

**Диагностика:** `tools/check_worker_pool_integrity.py --fix` — read-only отчёт
об инварианте `processing ⇔ claim` (или repair). Ключевой гейт — оптимизированный
интеграционный тест `tests/integration/test_worker_pool_concurrency.py`
(кейсы C1–C5, opt-in через `NANOBOT_INTEGRATION=1`).

### `lib/services/llm_client.py` — единая точка вызова LLM

Единственное место, откуда делаются запросы к LLM-провайдеру: ретраи,
таймауты, логирование через `loguru`, redaction секретов. Параметры
(API-ключ, base URL, модель) — через `config.require_setting("providers",
"llm")`. Используется навыком `audit_analyzer`, утилитой `tools/build_vectors.py`
и другими потребителями. Прямые `httpx`-вызовы к LLM в новом коде запрещены.

### `lib/utils/node_access.py` — обход настроек

Хелперы для безопасного обхода `SETTINGS` / `config.json` / `project.json`
с поддержкой `require_setting` (строгий) и `get_setting` (с fallback).
Удаляет ad-hoc `cfg.get("a", {}).get("b", default)` по кодовой базе. Потребители:
`audit_settings.py`, `application_context.py`, `cache_provider_impl.py`.

### `lib/utils/logging_utils.py` — настройка `loguru`

Один модуль с пресетами `setup(level=..., json=..., redact_keys=...)`,
вызываемый из `ApplicationContext.create()` и CLI-цикла. Гарантирует
одинаковый формат логов и redaction секретов во всех точках входа
(`gateway.py`, `cli_agent.py`, `streamlit_app.py`).

### `lib/utils/project_version.py` — версия проекта

`project_version()` возвращает версию текущего проекта. Канонический источник —
`project.json` → `project.version` (актуальный релизный тег `vX.Y.Z` без префикса
`v`), закоммичен на `master` и распространяется во все релизные ветки.
Git-теги и CHANGELOG для этого ненадёжны: релизные ветки `release/vX.Y`
ответвляются от `master` и не мержатся обратно, поэтому `git describe` и первый
релизный блок `CHANGELOG.md` на `master` отстают от актуального тега.
Fallback при отсутствии ключа — `git describe --tags`, затем `"dev"`.
Используется в стартовом баннере `gateway.py`, чтобы показать версию проекта
рядом с версией библиотеки nanobot (`__version__`).

### `lib/utils/outbound_filter.py` — фильтрация outbound

Скрывает internal-сообщения из пользовательского потока. Раньше фильтр
был в каждом канале свой → поведение в Streamlit расходилось с
Postgres/Redis. Теперь — один, через `MessageExchange`.

### `scripts/backfill_media_aw.py` — миграция media в AW-формат

Утилита для существующих развёртываний: читает `agent_conversation_messages`,
конвертирует старые `{filename, data}` в `{filename, file_id, mime_type,
file_size}` (payload → `data_store/cache/sessions/_shared/attachments/`,
в БД — только `file_id`). Идемпотентна: записи с уже проставленным
`file_id` пропускаются, HTTP/HTTPS-ссылки не трогает. CLI:
`python scripts/backfill_media_aw.py [--dry-run]`.

---

## 📁 Структура проекта

```
nanobot/
├── DEVELOPMENT.md                        # этот документ
├── tools/                                # инфраструктурные CLI-утилиты
│   └── build_vectors.py                  #   сборка векторных индексов (вне навыка)
├── sql/                                  # v2.0.0: все DDL сгруппированы по доменам
│   ├── README.md                          #   порядок применения, каталог
│   ├── session/                           #   session_meta + session_messages
│   ├── channels/                          #   seed_messages.sql (тестовые данные)
│   ├── logs/                              #   gateway_logs (DbLoggingService)
│   ├── audit_analyzer/                    #   домен oarb.* + векторы (GP)
│   ├── benchmarks/                        #   agent_benchmark_runs + agent_benchmark_results
│   └── migrations/                        #   инкрементальные миграции (например, logs)
│
├── lib/                                  #  v2.0.0: сервисный слой
│   ├── core/                             #   bootstrap ApplicationContext + фабрики
│   │   ├── application_context.py        #     create/start/stop, связывает все общие сервисы
│   │   ├── agent_factory.py              #     AgentLoop + ToolAudit hook + фабрика DatabaseLogging (per-turn)
│   │   └── bus_factory.py                #     MessageBus + обёртки publish_inbound/outbound
│   ├── services/                         #   сервисный слой (v2.0.0 + pre-existing)
│   │   ├── config_service.py             #    SETTINGS-аксессор + pre-resolve env + таймауты
│   │   ├── session_storage.py            #    выбор PGSessionManager / SessionManager
│   │   ├── runtime_patcher.py            #    все monkey-patch'и (ContextGovernor + _assemble_outbound)
│   │   ├── channel_factory.py            #    ChannelManager + Redis/Postgres каналы
│   │   ├── transcription_service.py      #    openai/groq key/URL/language
│   │   ├── subprocess_manager.py         #    Streamlit spawn + terminate/kill
│   │   ├── preload_service.py            #    FAISS preload + audit_cache refresh
│   │   ├── db_logging_service.py         #    worker, batch INSERT, без JSONL-fallback, get_stats()
│   │   ├── db_logging_bus.py             #    обёртки publish_inbound/outbound
│   │   ├── llm_config.py                 #    resolve_llm_config() — общий резолв LLM для навыка/бенчмарка
│   │   ├── audit_memory_store.py         #     in-memory DuckDB-зеркало + атомарный publish()
│   │   ├── audit_sync_service.py         #     фоновый поллинг PG (worker-поток)
│   │   ├── cache_provider.py             #     интерфейс CacheProvider + SearchResult
│   │   ├── cache_provider_impl.py        #     PostgresDuckDbProvider + фабрика и модульные функции
│   │   ├── text_splitter.py              #     чанкование текстов для индексаторов
│   │   # DDL для DbLoggingService (gateway_logs) — в sql/logs/
│   ├── cli/                              #  вынесено из cli_agent.py
│   │   ├── console_loop.py               #   REPL + typewriter + consume_outbound
│   │   ├── display_config.py             #   DisplayConfig
│   │   └── hook_loader.py                #   сканирование workspace/hooks/*.py
│   ├── hooks/                            #  фреймворковые хуки (не плагины)
│   │   ├── base_tool_tracking_hook.py    #     общий каркас для tool-хуков
│   │   ├── tool_audit_hook.py            #     хук аудита вызовов инструментов
│   │   └── database_logging_hook.py      #     AgentHook для tool-событий + run_finished в БД; per-turn инстанс через make_db_logging_hook_factory
│   ├── lifecycle/                        #  цикл запуска и graceful shutdown
│   │   ├── gateway_runner.py             #   run_forever с exponential backoff (1с → 30с)
│   │   └── shutdown_coordinator.py       #   LIFO graceful shutdown
│   ├── channels/                         #   каналы
│   │   ├── postgres_channel.py           #     канал через таблицу agent_conversation_messages
│   │   └── redis_channel.py              #     канал через Redis-очереди (BRPOP/LPUSH)
│   ├── session/                          #   хранилище сессий
│   │   └── pg_session_manager.py         #     хранение сессий в PostgreSQL (без JSONL)
│   └── (см. lib/core/, lib/cli/, lib/lifecycle/ выше)
│
├── workspace/                            # runtime-данные и плагины-хуки
│   ├── hooks/                            # плагины: самодостаточные AgentHook (cls(workspace_dir=...))
│   │   ├── session_file_redirect_hook.py #     перенаправление write/edit + media тула message в data_store/cache/sessions/
│   │   └── recent_files_hook.py          #     сбор созданных файлов для auto-attach в media
│   ├── skills/audit_analyzer/            # навык: тонкий CLI поверх провайдера
│   │   ├── SKILL.md                      #   пользовательская документация
│   │   ├── audit_analyze.bat / .sh       #   точки входа
│   │   ├── scripts/
│   │   │   ├── cli.py                    #   парсинг аргументов, маршрутизация режимов
│   │   │   ├── skill_config.py           #   конфиг из SETTINGS + build_cache_provider()
│   │   │   ├── database.py               #   Database (прямой PG, fallback) + QueryBackend
│   │   │   ├── sql_mode.py               #   режим sql: LLM → SQL → EXPLAIN → выполнение
│   │   │   ├── predefined_mode.py        #   режим predefined: готовые SQL-шаблоны
│   │   │   ├── predefined.py             #   резолв параметров (+ векторный поиск по source)
│   │   │   ├── scripts_registry.py       #   ScriptDefinition / ParamDefinition / реестр
│   │   │   ├── llm.py                    #   LLM-клиент (OpenAI-compatible HTTP)
│   │   │   └── output.py                 #   форматирование JSON-вывода
│   │   └── tests/
│   │       └── e2e_test.py               #   сквозной тест навыка (нужна живая БД)
│   └── skills/office_files/              # навык: чтение docx/xlsx/xls/pdf/pptx/csv/txt
│       ├── SKILL.md                      #   пользовательская документация
│       └── (utils: workspace/utils/office_files.py)
│
├── gateway.py                            #  v2.0.0: 132 строки, тонкий оркестратор
├── cli_agent.py                          #  v2.0.0: 165 строк, тонкий оркестратор
├── streamlit_app.py                      # [web-клиент, не через ApplicationContext]
├── config.py                             # SETTINGS (project.json + config.json + .secrets.env)
└── project.json                          # конфигурация (channels.*, skills.*, gateway, cli, logging.db)
```

---

## ⚙️ Конфигурация `tools.exec` (запуск команд)

Секция `tools.exec` в `config.json` управляет инструментом `exec` (запуск shell-команд).
Реализация — `nanobot/agent/tools/shell.py` (`ExecTool`, `ExecToolConfig`), точка запуска
процесса — `ExecTool._spawn()` (shell.py:515), сборка окружения — `_build_env()`
(shell.py:695).

### Как процесс реально запускается

- **Windows**: `exec` не наследует окружение родителя целиком — запускается
  `pwsh`/`powershell` через `asyncio.create_subprocess_exec(..., env=env)` (shell.py:548).
  `env` — **минимальный** набор: `SYSTEMROOT`, `COMSPEC`, `USERPROFILE`, `TEMP`, `PATHEXT`,
  `PATH`, `PYTHONUNBUFFERED` и т.д. (shell.py:706), плюс переменные из `allowedEnvKeys`
  (shell.py:726).
- **Linux**: запускается `bash -c "<command>"` (shell.py:556). Linux-ветка `_build_env()`
  (shell.py:731) передаёт **ещё меньше**: только `HOME`, `LANG`, `TERM`, `PYTHONUNBUFFERED`
  + `allowedEnvKeys`. Родительский `PATH` и прочие переменные **не пробрасываются**.
- Привязка к конкретному Python-окружению — через `pathPrepend` + `allowedEnvKeys`
  (на Linux), либо `login: true` для pyenv/conda (bash с `-l` прочитает профиль юзера,
  shell.py:559).

### Параметры (JSONC `config.json`, `tools.exec`)

| Ключ | Тип / дефолт | Назначение |
|---|---|---|
| `enable` | `bool` (`true`) | Включает/отключает `exec`. `false` — модель не запускает команды (`ExecTool.enabled`, shell.py:176). |
| `timeout` | `int` (`60`) | Жёсткий таймаут команды в секундах; `0` — без лимита (`_resolve_timeout`, shell.py:400). Таймаут по вызову модели капится до 600 (shell.py:247), конфиговый капки не имеет. |
| `pathPrepend` | `str` (`""`) | Дополняет `PATH` в **начале**. Linux: инъекция `export PATH="<prepend>:$PATH"` в команду (`_wrap_path_export`, shell.py:502); Windows: дописывает в `env["PATH"]` (`_compose_path`, shell.py:474). |
| `pathAppend` | `str` (`""`) | Дополняет `PATH` в **конце** (`$NANOBOT_PATH_APPEND`). |
| `sandbox` | `str` (`""`) | Обёртка команды в песочницу через `wrap_command` (shell.py:31, 467). На Windows не поддерживается — логируется warning, запуск без песочницы (shell.py:460). |
| `allowedEnvKeys` | `list[str]` (`[]`) | Какие переменные из окружения родителя дописать в минимальное `env` субпроцесса. На Linux почти ничего не наследуется, поэтому сюда передают `VIRTUAL_ENV`, `PYTHONPATH`, секреты (`DATABASE_URL`) и т.д. Секреты вне списка в субпроцесс не попадают (изоляция, shell.py:703). |
| `allowPatterns` | `list[str]` (`[]`) | Regex-паттерны команд, **явно разрешённые**. Приоритет над `denyPatterns`. Если задан — команда выполняется только когда **каждый** топ-сегмент (`&&`, `||`, `;`, `|`) матчится под один из паттернов (shell.py:761). |
| `denyPatterns` | `list[str]` (`[]`) | Regex-паттерны запрещённых команд (RE-search по команде в нижнем регистре, shell.py:766). Добавляются к жёстко зашитому дефолтному списку (`rm -rf`, `del /f`, `mkfs`, `dd if=`, `shutdown`, fork bomb и т.д., shell.py:214-232). |

### Пример: привязка к конкретному venv (Linux)

```json
"exec": {
  "enable": true,
  "timeout": 120,
  "pathPrepend": "/home/user/venv/bin",
  "pathAppend": "",
  "sandbox": "",
  "allowedEnvKeys": ["DATABASE_URL", "VIRTUAL_ENV", "PYTHONPATH"],
  "allowPatterns": [],
  "denyPatterns": []
}
```

- `pathPrepend` → `python`/`pip`/`activate` резолвятся из `/home/user/venv/bin`.
- `allowedEnvKeys` → в субпроцесс попадают `VIRTUAL_ENV`, `PYTHONPATH`, `DATABASE_URL`.
- Для pyenv/conda, где PATH собирается в профиле, надёжнее `login: true` (bash с `-l`,
  shell.py:559) — но отдельного конфиг-ключа нет, нужна правка `ExecToolConfig`.

### Примеры `allowPatterns` / `denyPatterns`

- `allowPatterns`: `["^git .*", "^python .*", "^ls .*"]` — пропускает цепочки вида
  `git add . && python run.py` (оба сегмента матчатся); `python run.py && rm -rf x`
  **заблокируется**, т.к. `rm` нет в allowlist.
- `denyPatterns`: `["rm -rf /", "drop database", "curl http://"]` — запрещает конкретные
  команды в дополнение к встроенному списку.

---

## 🔌 Универсальный слой данных lib/services

**Интерфейс** — `lib/services/cache_provider.py`:

- `CacheProvider` (ABC): `is_ready()`, `refresh()`, `check_stale()`,
  `preload_indexes()`, `search_vector()`, `query_sql()`, `explain()`,
  `get_schema()`, `close()`.
- `SearchResult` (dataclass): `content`, `score`, `source`, `table`, `pk_value`,
  `chunk`, `matched_chunks`, `row`.

**Реализация** — `lib/services/cache_provider_impl.py`:

- `PostgresDuckDbProvider` — единый провайдер: DuckDB-кеш + векторные индексы.
  Конструктор принимает только конфигурацию (схему, таблицы, пути, модель
  эмбеддинга) — без привязки к «аудиту».
- Модульные функции: `get_embedding()` (Ollama `/api/embed`),
  `load_cache_from_postgres(cache_path, db_config)`, `check_cache_stale(...)`,
  `read_vector_index_config(cfg)` (конфиг индексов — только из БД,
  `agent_vector_index_config`), `read_embedding_config(cfg)` (через
  `audit_vector_settings()`), `build_cache_provider(cfg, base_dir)` (фабрика
  провайдера из конфиг-секции навыка).
- Тяжёлые зависимости (`duckdb`, `psycopg2`, `faiss`, `numpy`, `pyarrow`, `httpx`)
  импортируются **лениво** внутри методов — импорт модуля остаётся лёгким,
  и gateway может управлять жизненным циклом без побочных эффектов.
- Если передан `dsn` — провайдер сам вызывает `utils.db.configure(dsn)`
  (идемпотентно), поэтому пригоден для использования автономно.

**Единый интерфейс бэкенда запросов.** `Database` (прямой PG) и
`PostgresDuckDbProvider` (кеш) реализуют одинаковые методы
`get_schema / query_sql / explain` (протокол `QueryBackend` в
`scripts/database.py`), поэтому режимы `predefined`/`sql` работают с любым
бэкендом без ветвлений.

**Фабрика провайдера** — универсальная `lib.services.cache_provider_impl.build_cache_provider(cfg, base_dir)`
собирает провайдера из конфиг-секции навыка (DuckDB-кеш, индексы, эмбеддинг).
Навык делегирует ей через `scripts/skill_config.build_cache_provider()`, тот же
набор настроек читает `gateway.py::_build_audit_services()` и индексатор
`tools/build_vectors.py`.

## 🔌 Единый пул соединений PostgreSQL (`workspace/utils/db.py`)

Чтобы на сервере никогда не было десятков параллельных подключений к PG
(проблема «too many connections» решается на уровне архитектуры, а не ретраями),
все подсистемы пользуются **одним общим пулом** соединений:

- **Одна job-очередь + пул воркеров** (по умолчанию `min_conn=1`, `max_conn=4`).
  Каждый воркер — поток, владеющий ровно одним psycopg2-соединением; он берёт
  задачи из общей очереди и выполняет их последовательно.
- **Публичный API** сохраняется: `execute/fetch/fetchone/fetchval`, async-варианты
  через `asyncio.to_thread`, `transaction()/async_transaction()`,
  `configure/resolve_dsn/run/set_pool_config/get_stats/start/shutdown`.
- **Транзакции — эксклюзивная аренда соединения** (`lease_id`): пока `with
  transaction()` жив, воркер первого вызова выполняет только задачи этой
  транзакции, а свободные воркеры продолжают обслуживать обычные задачи.
  Транзакция работает через прокси-объекты `_ConnectionProxy/_CursorProxy`
  (поддерживают справочные атрибуты psycopg2 — `autocommit`, `closed`,
  `commit()/rollback()/cursor()` и т.п.).
- **Транзакции честно ждут в очереди.** Если все воркеры заняты лизами,
  транзакция НЕ падает с ошибкой через `pool_timeout`, а ждёт освобождения
  воркера (как обычные job-ы). `pool_timeout` — порог диагностического
  warning («no free worker for lease …»), а не лимит ожидания. Если воркер
  был зализован, а `begin`-задача упала (обрыв соединения, полная очередь),
  lease гарантированно возвращается в пул (в `_acquire_lease` — `try/except`).
- **Реконнект с backoff** — внутри воркера: при обрыве соединение закрывается
  и пересоздаётся с экспоненциальной паузой (`reconnect_backoff_sec` →
  `reconnect_backoff_max_sec`); retry-able задачи переподнимаются до
  `job_max_retries`. После `connect_max_retries` неудачных подключений воркер
  сдаётся с ошибкой — сервисы, ждущие `run()`, не блокируются навсегда.
- **Неподключённые воркеры уступают очередь подключённым.** Если часть
  воркеров не смогла установить соединение (например, исчерпан лимит
  `CONNECTION LIMIT` роли или БД недоступна), они не отнимают задачи у живых:
  в `_take_job` такой воркер берёт обычную задачу, только когда в пуле нет ни
  одного воркера с живым соединением. Пока хотя бы один воркер подключён —
  вся очередь обслуживается им, неподключённые не тратят время на
  retry-connect. При полной недоступности БД задачи быстро падают с ошибкой
  подключения, а не висят в очереди вечно. Транзакции (`lease_id != 0`) на
  это правило не влияют — их воркер забирает безусловно.
- **Настройка** — `project.json → channels.postgres.pool`:
  `min_conn`, `max_conn`, `pool_timeout`, `queue_maxsize`,
  `reconnect_backoff_sec`, `reconnect_backoff_max_sec`, `connect_max_retries`,
  `idle_timeout_sec`, `job_max_retries`. `ApplicationContext.create()` читает эту
  секцию и применяет через `set_pool_config()`; `ctx.start()/stop()` вызывают
  `utils.db.start()/shutdown()`.

**Кто ходит в БД через пул:** `DbLoggingService`, `AuditSyncService`,
`PGSessionManager`, `PostgresChannel`, `session_storage`, `streamlit_app.py`,
инструменты и `cache_provider_impl` (bulk-load снимает соединение пула на всё
время копирования). Ни один сервис-поток не держит собственного psycopg2-
соединения — соединение выдаёт пул на время запроса/транзакции.

---

## ⚙️ Конфигурация навыка

Секция `skills.audit_analyzer` в `project.json`:

| Ключ | Назначение | Значение / по умолчанию |
|------|-----------|-------------|
| `llm_provider` / `llm_model` / `llm_api_base` | LLM для генерации SQL | `minimax` / `MiniMax-M3` / `https://api.minimax.io/v1` (единая с агентом; любой OpenAI-compatible: Mistral, OpenAI, MiniMax, Ollama, vLLM) |
| `llm_max_tokens` / `llm_temperature` | Параметры генерации | `8192` / `0.1` |
| `db_schema` | Схема с таблицами аудита | `oarb` |
| `db_tables` | Таблицы, доступные агенту | `audit_reports, audits, report_items, violations` (значение project.json; код по умолч. — пустой список) |
| `in_memory_enabled` | Включить DuckDB-кеш | `true` |
| `in_memory_engine` | Движок кеша | `duckdb` |
| `in_memory_cache_path` | Путь к файлу кеша (отн. навыка) | `cache/audit_cache.duckdb` |
| `poll_interval_sec` | Период инкрементального поллинга PG в `AuditSyncService` | `60` |
| `full_resync_every` | Полная перезагрузка таблицы каждые N циклов (сверка удалений) | `10` |
| `embedding_base_url` | Ollama `/api/embed` | `http://localhost:11434/api/embed` |
| `embedding_model` | Модель эмбеддинга | `mxbai-embed-large:latest` |
| `embedding_dimension` | Размерность вектора | `1024` |
| `mode_vector_db_table` | Таблица сырых векторов (источник индекса) | `oarb.audit_vectors` (значение project.json; код по умолч. — пусто) |
| `mode_vector_store_table` | Таблица сериализованных FAISS-индексов | `public.agent_vector_index_store` |
| `cli_default_mode` | Режим по умолчанию (резерв; CLI требует `--mode`) | `predefined` |
| `cli_max_retries` | Ретраи HTTP-запросов LLM-клиента (`llm.py`) | `3` |
| `cli_timeout_sec` | Таймаут запроса к LLM (`llm.py`) | `60` |

> Примечание: ретраи *генерации* SQL в режиме `sql` захардкожены в
> `sql_mode.py` (`MAX_RETRIES = 2` → до 3 попыток) и от `cli_max_retries`
> не зависят.

DSN подключается только через `channels.postgres.dsn` в `project.json`
(обычно `"${DATABASE_URL}"` из `.secrets.env`) через `utils.db.resolve_dsn()`.
Частичные ключи `host`/`port`/`dbname`/`user` удалены с v2.1.0 — подключение
без полного DSN невозможно. Навык собственного DSN не хранит.

---

## 🚀 CLI навыка: режимы

Точка входа: `workspace/skills/audit_analyzer/audit_analyze.bat` (или
`.sh`), либо `python scripts/cli.py`.

```
audit_analyze --mode {predefined,sql,vector} [опции]
```

| Режим | Назначение | Ключевые флаги |
|-------|-----------|----------------|
| `predefined` | Выполнение готовых SQL-шаблонов из реестра | `--script`, `--params` |
| `sql` | Генерация SELECT через LLM по текстовому запросу | `--query`, `--context` |
| `vector` | Семантический поиск по FAISS-индексу | `--query`, `--index-name`, `--top-k`, `--threshold`, `--vector-index` |

Примеры:

```bash
# predefined — готовый шаблон с параметрами
audit_analyze --mode predefined --script analytics_by_year_month --params '{"year": 2024}'

# sql — генерация SQL через LLM и выполнение
audit_analyze --mode sql --query 'сколько аудитов было в 2024 по месяцам'

# vector — топ-3 по схожести
audit_analyze --mode vector --query 'пожарная безопасность' --index-name audits_index --top-k 3

# vector — всё выше порога 0.7
audit_analyze --mode vector --query 'статусы аудитов' --index-name audits_index --threshold 0.7
```

**Как выбирается бэкенд запросов:** если `in_memory_enabled: true` — CLI строит
провайдера (`build_cache_provider()`), открывает DuckDB-кеш на чтение и работает
по нему; иначе — `Database` (прямой PostgreSQL). Кеш создаёт и обновляет
**gateway** (см. раздел «Жизненный цикл кеша»); CLI про это не знает. Если файла
кеша нет — CLI завершается с `FileNotFoundError`: «Кеш создаёт и обновляет
gateway автоматически — запустите его (python gateway.py)».

**Векторный поиск в predefined:** строковые параметры с
`validation.vector_source` (например, `violation_code`, `auditee_entity`,
`audit_type`) резолвятся через семантический поиск — провайдер подставляет
лучшее совпадение из индекса `{source}_index`.

---

## 🔄 Жизненный цикл кеша

**Владелец файла кеша навыка — `gateway.py`.** Навык (CLI) про создание и
обновление кеша больше не знает: `--force` удалён.

Пара сервисов строится в `gateway.py::_build_audit_services()` (возвращает
`(None, None)`, если `in_memory_enabled` выключен, нет DSN или таблиц):

- **`AuditSyncService`** — единственный владелец подключения к PostgreSQL
  (worker-поток). При старте выполняет полную загрузку таблиц, далее каждые
  `poll_interval_sec` (по умолч. 60 с) инкрементально опрашивает таблицы по
  track-колонке (`updated_at`; для `audit_vectors` — `id`). Новые/изменённые
  строки передаёт в callback `on_new_records` → `AuditMemoryStore.upsert_records`.
  Структуру таблиц собирает из PG `information_schema` (+ `pg_description`):
  колонки, типы, NOT NULL, комментарии → callback `on_schema` →
  `AuditMemoryStore.ensure_schema` (создание пустых таблиц, типы из PG).
  Каждые `full_resync_every` циклов (по умолч. 10) делает полную перезагрузку
  таблицы через `on_replace_records` → `AuditMemoryStore.replace_records`
  (сверка удалённых строк; курсор поллинга не откатывается).
- **`AuditMemoryStore`** — живое зеркало в чисто in-memory DuckDB
  (`cache_path=""`) + FAISS-индексы. `ensure_schema()` создаёт таблицы с типами
  из PG и сохраняет комментарии + исходные PG-типы в мета-таблицу
  `__nanobot_meta.__schema_meta` (входит в снимок). `get_schema()` возвращает
  исходные PG-типы и комментарии (без них — DuckDB-тип из information_schema).
  `publish()` атомарно записывает снимок таблиц (ATTACH во временный файл →
  `os.replace`) в `publish_path` = `in_memory_cache_path` навыка. Без изменений
  (`_dirty` = False) файл не перезаписывается; если снимок занят читателем
  (CLI) — публикация откладывается до следующего цикла, ошибка не теряет данные.

Схема в `gateway.py::run()`:

```
_build_audit_services() ──► (store, sync_service)
sync_service.set_on_new_records_callback(store.upsert_records)
sync_service.set_on_replace_records_callback(store.replace_records)
sync_service.set_on_schema_callback(store.ensure_schema)
sync_service.set_on_sync_callback(store.publish)     # снимок после каждого цикла
sync_service.start(initial_load=True)                # полная загрузка + поллинг
_preload_vector_indexes(store)                       # прогрев FAISS в память
...
(finally) store.publish() → store.close()            # финальный снимок при выходе
```

**Правило одного писателя.** DuckDB допускает только один процесс-писатель на
файл, поэтому gateway никогда не открывает целевой файл на запись: он пишет во
временный файл и атомарно подменяет его `os.replace()`. Навык (CLI) открывает
опубликованный снимок только на чтение и видит целостные данные в любой момент.

**cli_agent.py** по-прежнему содержит унаследованную фоновую загрузку/свежесть
кеша (`load_cache_from_postgres` / `check_cache_stale`) как резерв; основной
владелец и источник снимка — `gateway.py`.

### 🔧 Управление синхронизацией

#### Полный цикл обновления данных (что происходит по шагам)

1. **Старт gateway** (`gateway.py::run()`): `_build_audit_services()` читает
   секцию `skills.audit_analyzer` из `project.json`. Сервисы создаются, только
   если `in_memory_enabled: true`, задан DSN и есть таблицы; иначе — `(None, None)`
   и синхронизация не запускается.
2. **Initial load**: `AuditSyncService._do_initial_load()` для каждой таблицы из
   `db_tables` (+ таблица векторов) делает:
   - `_fetch_schema()` — запрос структуры из PG `information_schema.columns`
     + `pg_description` (колонки, типы, NOT NULL, комментарии таблиц/колонок);
   - `_ensure_table_schema()` → колбека `on_schema` → `store.ensure_schema()` —
     создаёт таблицу в in-memory DuckDB **с типами из PG** (включая пустые);
   - `_fetch_all()` → `SELECT *` → колбека `on_new_records` → `store.upsert_records()`.
3. **Поллинг**: worker-поток каждые `poll_interval_sec` вызывает
   `_poll_table()`: `SELECT * WHERE "<track>" > <последняя_метка>`. Track-колонка:
   `updated_at` (доменные таблицы) или `id` (`audit_vectors`). Новые/изменённые
   строки → `upsert_records()` (upsert по `id`: DELETE + INSERT).
4. **Публикация снимка**: после initial load и каждого цикла поллинга
   `_fire_sync_callback()` → `store.publish()` — атомарный снимок (ATTACH во
   временный файл → `os.replace`) в файл кеша навыка. `publish()` — no-op, если
   данных не менялось (`_dirty = False`) или `publish_path` пуст.
5. **Полная пересинхронизация** (сверка удалений): каждые `full_resync_every`
   циклов поллинга `_poll_table()` вместо инкрементального запроса делает
   `_fetch_all()` + `_dispatch_replace()` → `store.replace_records()` — таблица
   перезаписывается целиком (структура и типы сохраняются), удалённые в PG
   строки исчезают. Курсор поллинга не откатывается (новое значение только если
   больше текущего).
6. **Завершение**: при остановке gateway `sync_service.stop()` дописывает
   очередь, затем в `finally` — финальный `store.publish()` и `store.close()`.

#### Как связаны компоненты (callbacks)

Колбеки подключаются в `gateway.py::run()` (строки ~608-613) — это единственная
точка связывания `AuditSyncService` и `AuditMemoryStore`:

| Событие в AuditSyncService | Колбека | Метод store | Что делает |
|---------------------------|---------|-------------|-----------|
| новые/изменённые строки | `set_on_new_records_callback` | `upsert_records` | инкрементальный апдейт |
| полная перезагрузка таблицы | `set_on_replace_records_callback` | `replace_records` | сверка удалений |
| структура из PG | `set_on_schema_callback` | `ensure_schema` | типы, NOT NULL, комментарии |
| завершение цикла | `set_on_sync_callback` | `publish` | публикация снимка для CLI |

Чтобы изменить поведение (например, публиковать снимок реже или дополнительно
инвалидировать индексы) — правишь именно эту секцию `gateway.py`.

#### Управляющие ключи (`skills.audit_analyzer` в `project.json`)

| Ключ | Где читается | По умолч. | Эффект |
|------|-------------|-----------|--------|
| `in_memory_enabled` | `gateway.py:511` | `false` | Выключает весь кеш и синхронизацию: CLI ходит напрямую в PG. `true` — включает |
| `in_memory_cache_path` | `gateway.py:524` | `cache/audit_cache.duckdb` | Куда публикуется снимок (отн. `workspace/skills/audit_analyzer/`). Меняя — меняешь файл, который читает CLI |
| `db_schema` / `db_tables` | `gateway.py:516-518` | `oarb` / 4 таблицы | Какие таблицы синхронизировать. Список — массив строк; добавил таблицу → она появится в кеше после следующего цикла |
| `poll_interval_sec` | `gateway.py:549` | `60` | Частота инкрементального поллинга, сек. Меньше → свежее кеш, больше запросов к PG |
| `full_resync_every` | `gateway.py:552` | `10` | Полная перезагрузка таблиц каждые N циклов поллинга. `0` — отключить (удалённые строки останутся в кеше) |
| `mode_vector_db_table` | `gateway.py:517` | `oarb.audit_vectors` | Таблица векторов, включается в синхронизацию и прогревается в FAISS |

`config.py` мержит `project.json` в `SETTINGS`; после правки `project.json`
перезапуск gateway обязателен.

#### Требования к таблицам источника

- Колонка `id` (для upsert `DELETE + INSERT` по ключу; если её нет — таблица
  пересоздаётся из батча целиком).
- Колонка `updated_at` (инкрементальный поллинг). Для `audit_vectors` — `id`.
  Тип track-колонки должен быть сравнимым (`>`) — `timestamp`/`bigint`.
- Безопасность поллинга: строки, изменённые **и** удалённые между циклами,
  подхватятся полной пересинхронизацией (`full_resync_every`).

#### Практические сценарии

- **Свежее кеш, чаще опрос**: `poll_interval_sec: 15`.
- **Меньше нагрузки на PG**: `poll_interval_sec: 300`, `full_resync_every: 5`
  (реже опрос, но регулярная сверка удалений).
- **Не нужна сверка удалений / большие таблицы**: `full_resync_every: 0`.
- **Выключить кеш совсем**: `in_memory_enabled: false` (CLI → прямой PG).
- **Добавить таблицу в анализ**: вставить её имя в `db_tables` и перезапустить
  gateway.

#### Мониторинг

- `AuditMemoryStore.get_stats()`: `tables` (кол-во строк), `dirty`,
  `upserts`, `publishes`, `publish_errors`, `last_upsert_at`, `last_publish_at`,
  `last_error`, `indexes_in_memory`, `vector_sources`.
- `AuditSyncService.get_stats()`: `polls`, `full_resyncs`, `reconnects`,
  `errors`, `queue_size`, `last_sync` (метка на таблицу), `connected`.
- Внешние признаки работы: mtime файла кеша
  (`workspace/skills/audit_analyzer/cache/audit_cache.duckdb`) обновляется после
  каждого publish; лог gateway: `audit_analyzer sync started (in-memory cache +
  vectors, публикация кеша навыка: <path>)`.

---

## 🔍 Векторная индексация

> **Исчерпывающий гайд:** как устроены индексы, как их создавать, обновлять,
> добавлять новые, отлаживать, и какие таблицы/файлы задействованы.

### Архитектура

```
┌──────────────────────────────────────────────────────────────────────┐
│ 1. Конфиг (public.agent_vector_index_config)                                  │
│    - задаёт какие таблицы индексировать, какие колонки эмбеддить,    │
│      параметры чанкования, track-колонку                             │
└──────────────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────────────┐
│ 2. Источники (oarb.audits, oarb.violations, oarb.audit_reports, …)   │
│    - читаются через SELECT * + track_column для инкрементального     │
│      сравнения с уже собранными векторами                            │
└──────────────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────────────┐
│ 3. tools/build_vectors.py (сборщик)                                   │
│    - NEW/CHANGED/DELETED классификация по (source, pk_value)         │
│    - чанкование длинных текстов (lib/services/text_splitter.py)      │
│    - батчевый эмбеддинг через Ollama /api/embed                       │
│    - INSERT в oarb.audit_vectors + rebuild FAISS в public.agent_vector_index_store
└──────────────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────────────┐
│ 4. Хранилище                                                         │
│    - oarb.audit_vectors:  эмбеддинги REAL[] + метаданные             │
│    - public.agent_vector_index_store: сериализованный FAISS BYTEA (для поиска)│
└──────────────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────────────┐
│ 5. Поиск (audit_analyzer --mode vector)                              │
│    - PostgresDuckDbProvider.search_vector()                          │
│    - десериализует FAISS из public.agent_vector_index_store, ищет в памяти,  │
│      при промахе пересобирает из oarb.audit_vectors                  │
└──────────────────────────────────────────────────────────────────────┘
```

### Таблицы

| Таблица | Назначение | Кто пишет | Кто читает |
|---------|-----------|-----------|-----------|
| `public.agent_vector_index_config` | Конфиг индексов (имя, источник, колонки, чанки, track_column, enabled) | `seed_default_indexes.sql` (вручную) | `tools/build_vectors.py` |
| `oarb.audit_vectors` | Сырые эмбеддинги `REAL[]` + метаданные (chunk_index/count, content_hash, row_data JSONB, synced_at) | `tools/build_vectors.py` | `lib/services/cache_provider_impl.py:PostgresDuckDbProvider` (агент читает только через DuckDB-снимок `audit_cache.duckdb`; канон — PG) |
| `public.agent_vector_index_store` | Сериализованный FAISS `BYTEA` + метаданные (dimension, vector_count, updated_at) | `provider.rebuild_and_store_index()` | `provider._INDEX_CACHE` (in-memory после preload) |

DDL: `sql/audit_analyzer/create_public_agent_vector_index_config.sql`, `sql/audit_analyzer/create_oarb_audit_vectors.sql`.

### Дефолтные индексы

В `sql/audit_analyzer/seed_default_indexes.sql` (idempotent):

| index_name | Источник | content_cols | embedding_cols | Чанкование |
|------------|----------|--------------|----------------|------------|
| `audits_index` | `oarb.audits` | `title, audit_type, auditee_entity, status` | те же 4 колонки | нет |
| `violations_index` | `oarb.violations` | `description, recommendation, violation_code, severity` | `description` (chunked 500/80) + `violation_code` | да |
| `audit_reports_index` | `oarb.audit_reports` | `full_text, title, report_number, report_date` | `full_text` (chunked 500/80) + `title` | да |

Применение: `psql "$DATABASE_URL" -f sql/audit_analyzer/seed_default_indexes.sql`

### Шпаргалка: что делать в каком случае

| Цель | Команда | Что происходит |
|------|---------|---------------|
| **Добавить новый индекс** | 1. INSERT в `public.agent_vector_index_config`<br>2. `--index <name> --full-rebuild` | Создаётся конфиг, собираются вектора + FAISS |
| **Обновить один индекс (новые строки)** | `--index <name>` | Инкрементально: NEW/CHANGED/DELETED по `content_hash` |
| **Обновить один индекс (изменился конфиг)** | UPDATE конфига + `--index <name> --full-rebuild` | TRUNCATE индекса + все строки заново |
| **Обновить все индексы (новые строки)** | `build_vectors.py` (без флагов) | Все индексы из конфига, инкрементально |
| **Обновить все индексы (после изменений конфига)** | `--full-rebuild` | Все индексы, TRUNCATE + заново |
| **Проверить что всё актуально (без записей)** | `--check` | Сравнивает сигнатуру, обновляет только diff |
| **Обновить индекс после DDL таблицы** | `--index <name> --full-rebuild` | Схема таблицы изменилась → нужен полный пересчёт |
| **Изменилась модель эмбеддинга** | UPDATE `embedding_*` в `project.json` + `--full-rebuild` | Старые FAISS с неправильной размерностью пересоберутся |
| **Отключить индекс (без удаления данных)** | `UPDATE ... SET enabled = false` | `build_vectors` пропустит, вектора остаются |
| **Удалить индекс полностью** | DELETE из 3 таблиц (`audit_vectors`, `agent_vector_index_store`, `agent_vector_index_config`) | Полное удаление |
| **Удалить все индексы разом** | `TRUNCATE oarb.audit_vectors, public.agent_vector_index_store` + `DELETE FROM public.agent_vector_index_config` | Полная очистка |
| **Восстановить случайно удалённый индекс** | Заново INSERT + `--full-rebuild` | Полная пересборка из источника |
| **Сценарий «один и тот же текст в разных индексах»** | Два индекса с разными `index_name` на одной таблице | Поддерживается, поиск по `index_name` |
| **Embedding провайдер недоступен** | `--check` (быстрее падает), проверить Ollama | Все строки → `errors=N` |
| **Performance для 100k+ строк** | `--batch-size 8 --chunk-size 300` | Меньше памяти Ollama, дольше |

### Как добавить новый индекс

**1. Опишите индекс в `public.agent_vector_index_config`:**

```sql
INSERT INTO public.agent_vector_index_config
    (index_name, source_table, src_table, pk_column,
     content_cols, embedding_cols, track_column, enabled)
VALUES (
    'objects_index',                        -- уникальное имя (используется в CLI --index-name)
    'objects',                              -- короткое имя (идёт в column "source" таблицы audit_vectors)
    'oarb.objects',                         -- полное имя исходной таблицы
    'id',                                   -- колонка первичного ключа
    ARRAY['name', 'description']::TEXT[],   -- колонки для content (отображение в результатах поиска)
    '[
        {"column": "description", "chunk": true, "chunk_size": 500, "chunk_overlap": 80},
        "name"
    ]'::JSONB,                              -- колонки для эмбеддинга (с чанкованием или без)
    'updated_at',                           -- track_column: должен быть monotonic, тип timestamp/bigint
    true
)
ON CONFLICT (index_name) DO UPDATE SET ...;  -- для идемпотентного повторного применения
```

**2. Проверьте:**

```bash
python tools/build_vectors.py --status
# Должен появиться objects_index со счётчиком 0
```

**3. Соберите вектора:**

```bash
# Только новый индекс
python tools/build_vectors.py --index objects_index --full-rebuild

# Или все индексы из конфига
python tools/build_vectors.py --full-rebuild
```

**4. Проверьте FAISS:**

```bash
python tools/build_vectors.py --status
# objects_index: 100 векторов, размерность 1024

psql -c "SELECT source, dimension, vector_count, updated_at FROM public.agent_vector_index_store ORDER BY source"
# objects_index | 1024 | 100 | 2026-08-12 ...
```

**5. Используйте в CLI:**

```bash
audit_analyze.bat --mode vector --query "объект с нарушениями" --index-name objects_index --top-k 5
```

### Как обновить существующий индекс

#### Сценарий A: новые/изменённые/удалённые строки в источнике (типичный случай)

```bash
# Один индекс — инкрементально (быстро, классификация NEW/CHANGED/DELETED по content_hash)
python tools/build_vectors.py --index audits_index

# Все индексы — инкрементально
python tools/build_vectors.py

# Быстрая проверка: обновить только если сигнатура изменилась (для cron)
python tools/build_vectors.py --check
```

`--check` сравнивает `(count, MAX(track_column))` источника с `oarb.audit_vectors`. Если совпадает — пропускает; если различается — запускает инкрементальную сборку.

**Когда `--check` не помогает:** если меняли `embedding_cols` (сигнатура та же), или добавляли колонку в источник.

#### Сценарий B: изменился список embedding_cols или content_cols

```sql
UPDATE public.agent_vector_index_config
SET embedding_cols = '["title", "description", {"column":"body","chunk":true}]'::jsonb,
    content_cols = ARRAY['title', 'description']::text[],
    updated_at = NOW()
WHERE index_name = 'audits_index';
```

```bash
python tools/build_vectors.py --index audits_index --full-rebuild
# Контент изменился → content_hash другой → все строки пересоздаются
```

**Без `--full-rebuild`** нельзя: `content_hash` изменится для всех строк → `build_vectors` увидит CHANGED → DELETE + INSERT (это эквивалентно `--full-rebuild` для индекса, но медленнее — без TRUNCATE). Используйте `--full-rebuild` явно.

#### Сценарий C: изменилась модель эмбеддинга или размерность

`project.json → skills.audit_analyzer.embedding_*`:

```json
{
  "embedding_model": "nomic-embed-text:latest",
  "embedding_dimension": 768
}
```

**Обязательная последовательность:**

```bash
# 1. Удалить старые FAISS — у них неправильная размерность
psql -c "DELETE FROM public.agent_vector_index_store"

# 2. Удалить старые вектора — у них неправильная размерность
psql -c "TRUNCATE oarb.audit_vectors"

# 3. Пересобрать с новой моделью
python tools/build_vectors.py --full-rebuild

# 4. Проверить размерность
python tools/build_vectors.py --status
# dim должен быть 768, не 1024
```

**Альтернатива (быстрее, но менее надёжно):** оставить `audit_vectors` без изменений, но тогда `provider.search_vector()` может получить `RuntimeError: dimension mismatch` (Ollama вернёт 768, FAISS ожидает 1024). Чистая пересборка безопаснее.

#### Сценарий D: добавилась новая колонка в источнике (DDL)

**Если колонка НЕ используется в embedding_cols** — просто запустите без `--full-rebuild`:

```bash
python tools/build_vectors.py --index audits_index
```

**Если колонка добавляется в embedding_cols** — это сценарий B (UPDATE конфига + `--full-rebuild`).

**Если изменился тип колонки** (varchar→text, bigint→int) — `--full-rebuild` обязателен.

**Если колонка переименована** — старые вектора ссылаются на старое имя через `row_data` (JSONB). В поиске будут видны старые имена; новый `--full-rebuild` обновит.

#### Сценарий E: DDL-изменения в исходной таблице (DROP COLUMN, RENAME, ALTER TYPE)

```bash
# Полная перестройка индекса на этой таблице
python tools/build_vectors.py --index audits_index --full-rebuild
```

**Если `embedding_cols` ссылается на колонку, которой больше нет** — будет ошибка `column "X" does not exist`. Решение: сначала обновите конфиг (`UPDATE public.agent_vector_index_config SET embedding_cols = '[...]'::jsonb WHERE ...`), затем `--full-rebuild`.

**Если DROP COLUMN `track_column`** (`updated_at`) — все индексы на этой таблице перестанут обновляться инкрементально. Решение: добавить новую `updated_at` + обновить конфиг.

#### Сценарий F: исходная таблица пуста (TRUNCATE в источнике)

```bash
# После очистки источника вручную:
psql -c "TRUNCATE oarb.audits"

# build_vectors увидит: source rows = 0, audit_vectors > 0 → все строки DELETED
python tools/build_vectors.py --index audits_index
# Все вектора индекса будут удалены
```

Или принудительно:

```bash
python tools/build_vectors.py --index audits_index --full-rebuild
# TRUNCATE индекса + нет строк для добавления → 0 векторов в индексе
```

#### Сценарий G: добавлен новый индекс (см. «Как добавить новый индекс» выше)

#### Сценарий H: обновить ВСЕ индексы разом

```bash
# Все индексы, инкрементально (без --full-rebuild)
python tools/build_vectors.py

# Все индексы, полная перестройка
python tools/build_vectors.py --full-rebuild

# Все индексы, только проверка сигнатуры
python tools/build_vectors.py --check
```

**Порядок обработки:** `audits_index` → `violations_index` → `audit_reports_index` (по алфавиту `index_name`).

#### Сценарий I: остановить и продолжить обновление (mid-build)

`build_vectors.py` — **идемпотентен**. Если прервать (Ctrl-C) посередине `--full-rebuild`:

```bash
# Что произошло: DELETE FROM oarb.audit_vectors WHERE source = X выполнен
# INSERT выполнен частично
# Что делать:
python tools/build_vectors.py --index X --full-rebuild
# DELETE повторится (безопасно, ничего не изменит), INSERT добьёт
```

**Не нужно:** `TRUNCATE` вручную — `--full-rebuild` уже сделал DELETE.

#### Сценарий J: ошибка во время обновления

См. [Edge cases → Что делать при ошибке посреди --full-rebuild](#что-делать-при-ошибке-посреди---full-rebuild) ниже.

### Как отключить/удалить индекс

#### Отключить (без удаления собранных векторов)

```sql
UPDATE public.agent_vector_index_config SET enabled = false WHERE index_name = 'audits_index';
```

`build_vectors.py` пропустит его при следующем запуске. Вектора остаются в `oarb.audit_vectors` и `public.agent_vector_index_store`.

**Когда использовать:** временно не нужен (например, на время миграции источника), но потом восстановим.

**Что произойдёт в навыке:** `--mode vector --index-name audits_index` **продолжит работать** — провайдер читает FAISS из `agent_vector_index_store`, а не из конфига.

#### Удалить полностью (один индекс)

```sql
-- 1. Удалить собранные вектора (каскадно по source)
DELETE FROM oarb.audit_vectors WHERE source = 'audits_index';
DELETE FROM public.agent_vector_index_store WHERE source = 'audits_index';

-- 2. Удалить конфиг
DELETE FROM public.agent_vector_index_config WHERE index_name = 'audits_index';
```

После этого `audit_analyze.bat --mode vector --index-name audits_index` вернёт **пустой результат** (нет FAISS-индекса). Чтобы восстановить — INSERT конфига + `--full-rebuild`.

**Что НЕ удаляется:**
- Исходная таблица `oarb.audits` — не трогается.
- Другие индексы — не затрагиваются.

#### Удалить все индексы разом (полная очистка)

```sql
-- Все собранные вектора + FAISS + конфиги
TRUNCATE oarb.audit_vectors;
TRUNCATE public.agent_vector_index_store;
TRUNCATE public.agent_vector_index_config CASCADE;
```

После этого `audit_analyze --mode vector` **вернёт ошибку** «нет конфигурации индексов». Восстановление:

```bash
# 1. Применить seed заново
psql -f sql/audit_analyzer/seed_default_indexes.sql

# 2. Пересобрать
python tools/build_vectors.py --full-rebuild
```

#### Удалить один индекс через `TRUNCATE` (быстро, но задевает всё)

**Не рекомендуется** — `TRUNCATE oarb.audit_vectors` без `WHERE` очищает ВСЕ индексы. Если нужно очистить только один:

```sql
-- Найти pk_value, которые принадлежат этому индексу
DELETE FROM oarb.audit_vectors
WHERE source = 'audits_index';

DELETE FROM public.agent_vector_index_store
WHERE source = 'audits_index';
```

Это эквивалентно первому варианту, но с явным указанием колонки. Используйте `DELETE FROM ... WHERE source = X`, а не `TRUNCATE` — без `WHERE` очистите всё.

#### Восстановление (recovery) после случайного удаления

**Если удалили только конфиг (`DELETE FROM agent_vector_index_config`):**

```bash
# 1. Восстановить конфиг (можно взять из бэкапа или из seed_default_indexes.sql)
psql -f sql/audit_analyzer/seed_default_indexes.sql
# Отредактируйте если нужен был другой конфиг

# 2. Вектора и FAISS остались в БД — НЕ пересобирайте, просто проверить
python tools/build_vectors.py --status
# Если FAISS нет — нужно --full-rebuild
```

**Если удалили вектора (`DELETE FROM audit_vectors`):**

```bash
# Вектора потеряны, FAISS остался но невалиден
python tools/build_vectors.py --full-rebuild
# TRUNCATE индекса + полная пересборка
```

**Если `TRUNCATE` всех таблиц:**

```bash
psql -f sql/audit_analyzer/create_oarb_audit_vectors.sql
psql -f sql/audit_analyzer/create_public_agent_predefined_scripts.sql
psql -f sql/audit_analyzer/create_public_agent_vector_index_config.sql
psql -f sql/audit_analyzer/create_public_agent_vector_index_store.sql
psql -f sql/audit_analyzer/seed_default_indexes.sql
python tools/build_vectors.py --full-rebuild
```

#### Что будет если `--index-name` указывает на несуществующий индекс

```bash
audit_analyze.bat --mode vector --query "..." --index-name does_not_exist
# "Индекс 'does_not_exist' не найден или отключён"
```

Вектора в БД не затрагиваются. Ошибка показывается пользователю.

#### Что будет если удалить индекс, а в `audit_analyze.bat` ссылка

**Если индекс был в реестре предопределённых скриптов (`predefined.py`):** поиск перестанет находить `vector_source` параметры для этого индекса (ошибка `CacheProvider.search_vector` → `[]`).

**Если индекс был в `predefined.py` через `validation.vector_source`:** скрипт вернёт ошибку `vector_source not configured` или пустой результат.

**Чистый CLI:** `--mode vector --index-name X` — пустой результат без падения.

#### Удалить через `psql` cascade (осторожно)

```sql
-- Удалить только конфиг одного индекса (без удаления векторов)
DELETE FROM public.agent_vector_index_config WHERE index_name = 'audits_index';
-- Вектора остаются, но build_vectors не будет их пересобирать
-- (новые строки в источнике не подхватятся — нужен заново INSERT конфига)
```

#### Что удалять нельзя

- `public.agent_vector_index_config` целиком `TRUNCATE ... CASCADE` — удалит все индексы разом (см. выше как восстановить).
- `oarb.audit_vectors` без `WHERE` — удалит ВСЕ вектора всех индексов.
- `public.agent_vector_index_store` без `WHERE` — удалит ВСЕ FAISS-индексы.

Если удалили случайно — см. **«Восстановление после случайного удаления»** выше.

#### Автоматизация удаления в cron / CI

```bash
# Временно отключить индекс (без потери данных)
psql -c "UPDATE public.agent_vector_index_config SET enabled = false WHERE index_name = 'audit_reports_index'"

# Полностью удалить индекс + пересобрать остальные
psql -c "DELETE FROM oarb.audit_vectors WHERE source = 'audit_reports_index'"
psql -c "DELETE FROM public.agent_vector_index_store WHERE source = 'audit_reports_index'"
psql -c "DELETE FROM public.agent_vector_index_config WHERE index_name = 'audit_reports_index'"
python tools/build_vectors.py --full-rebuild  # пересоберёт оставшиеся 2
```

### Алгоритм сборки одного индекса

`tools/build_vectors.py:build_index(index_name, index_cfg, db_table, ...)`:

1. **Загрузить текущее состояние** из `oarb.audit_vectors` по `(source, pk_value)`.
2. **Прочитать все строки** из исходной таблицы через `SELECT *`.
3. **Посчитать `content_hash`** для каждой строки (MD5 от search_text).
4. **Классифицировать:**
   - **NEW** — строки, которых нет в `audit_vectors` → INSERT
   - **CHANGED** — `content_hash` изменился → DELETE + INSERT
   - **DELETED** — строки, удалённые из источника → DELETE
5. **Разбить на чанки** через `lib/services/text_splitter.py:build_chunks` (только колонки с `chunk: true`).
6. **Батчами отправить в Ollama** (`embedding_base_url` из project.json).
7. **INSERT в `oarb.audit_vectors`** (один INSERT на чанк).
8. **Пересобрать FAISS**: `provider.invalidate_cache(index)` + `provider.rebuild_and_store_index(index, db_table)`.

### Параметры конфигурации (public.agent_vector_index_config)

| Поле | Тип | Назначение |
|------|-----|-----------|
| `index_name` | TEXT PK | Уникальное имя индекса (audits_index, violations_index, …). Используется в CLI `--index-name`. |
| `source_table` | TEXT | Короткое имя для `column "source"` в `audit_vectors` (например `audits`, `violations`). |
| `src_table` | TEXT | Полное имя исходной таблицы (`schema.table`). |
| `pk_column` | TEXT | Колонка первичного ключа (по умолч. `id`). |
| `content_cols` | TEXT[] | Колонки для `content` (полный текст для отображения в результатах поиска). |
| `embedding_cols` | JSONB | Колонки для эмбеддинга. Формат: `["col"]` или `[{"column":"col","chunk":true,"chunk_size":500,"chunk_overlap":80}]`. |
| `track_column` | TEXT | Колонка для инкрементальной выборки. Должна быть сравнимой (`>`): `timestamp`, `bigint`. |
| `enabled` | BOOLEAN | Активен ли индекс при следующем запуске `build_vectors.py`. |

### Формат `embedding_cols`

Два варианта в одном массиве (можно смешивать):

```jsonc
// Только имена колонок — простой случай
["title", "audit_type", "status"]

// С чанкованием для длинных текстов
[
  {"column": "description", "chunk": true, "chunk_size": 500, "chunk_overlap": 80},
  "violation_code"
]

// Микс — некоторые с чанкованием, некоторые без
[
  {"column": "full_text", "chunk": true, "chunk_size": 800, "chunk_overlap": 150},
  {"column": "summary", "chunk": false},
  "title"
]
```

**Когда использовать чанкование:**
- Текст >1000 символов → да (по умолчанию chunk_size=500).
- Структурированные поля (код, статус, тип) → нет.
- Короткие тексты (title, summary до 200 символов) → нет (чанки будут по 1).

### Требования к исходной таблице

| Требование | Зачем | Как проверить |
|-----------|-------|---------------|
| Колонка `pk_column` существует | Для `DELETE + INSERT` (upsert) | `SELECT pk FROM table LIMIT 1` |
| Колонка `track_column` монотонна | Для инкрементального опроса | Должна быть `TIMESTAMP` или `BIGINT`, обновляться при UPDATE |
| Все `content_cols` и `embedding_cols` существуют | Для чтения | `SELECT col1, col2 FROM table LIMIT 1` |
| Доступ на `SELECT` | Сборщик должен читать | `GRANT SELECT ON table TO <user>` |
| Доступ на `INSERT`/`DELETE` в `oarb.audit_vectors` | Запись результатов | `GRANT INSERT, DELETE ON oarb.audit_vectors` |
| Доступ на `INSERT`/`UPDATE`/`DELETE` в `public.agent_vector_index_store` | FAISS-сериализация | `GRANT ... ON public.agent_vector_index_store` |

### Алгоритм чанкования

`lib/services/text_splitter.py:build_chunks(row, embedding_cols, chunk_size, chunk_overlap)`:

1. Если **все** колонки короче `chunk_size` → один чанк.
2. Иначе → самая длинная колонка дробится рекурсивно через `split_text()`:
   - Разделители (по приоритету): `\n\n` → `\n` → `.!?` → `,;` → пробел → символ.
   - Чанки склеиваются с перекрытием `chunk_overlap`.
3. В `search_text` каждого чанка добавляется метка `[N/M]`.
4. В `content` (для отображения) добавляется суффикс ` [ч. N/M]`.

**Поведение при поиске:** если несколько чанков одного документа попали в top-K, возвращается только один с наивысшим score, остальные доступны через `matched_chunks`.

### Мониторинг

**Статусы через CLI:**

```bash
python tools/build_vectors.py --status
# index_name: vector_count, dimension, src_rows, last_sync
```

**SQL-запросы:**

```sql
-- Сколько векторов в каждом индексе
SELECT source, COUNT(*) AS cnt, MAX(synced_at) AS last
FROM oarb.audit_vectors
GROUP BY source
ORDER BY source;

-- Состояние FAISS-индексов
SELECT source, dimension, vector_count, updated_at
FROM public.agent_vector_index_store
ORDER BY source;

-- Сколько чанков у одного документа (для отладки)
SELECT pk_value, COUNT(*) AS chunks
FROM oarb.audit_vectors
WHERE source = 'violations_index'
GROUP BY pk_value
ORDER BY chunks DESC
LIMIT 10;

-- Вектора без FAISS-индекса (несоответствие)
SELECT v.source, COUNT(*) AS orphan_vectors
FROM oarb.audit_vectors v
LEFT JOIN public.agent_vector_index_store s ON s.source = v.source
WHERE s.source IS NULL
GROUP BY v.source;
```

### Типичные проблемы

#### Ошибки при запуске

| Симптом | Причина | Что делать |
|---------|---------|-----------|
| `ModuleNotFoundError: No module named 'faiss'` | Не установлен `faiss-cpu` | `pip install faiss-cpu numpy` |
| `ModuleNotFoundError: No module named 'numpy'` | Не установлен `numpy` | `pip install numpy` |
| `FAISS-индекс не собран` warning | `faiss` или `numpy` отсутствуют | Установить, перезапустить `--full-rebuild` |
| `ImportError: No module named 'utils'` или `No module named 'lib'` | Скрипт не из корня проекта | Запускать из корня: `cd /path/to/nanobot && python tools/build_vectors.py` |
| `psycopg2.OperationalError: connection refused` | Неверный DSN или PostgreSQL не запущен | Проверьте `channels.postgres.{host,port,user,dbname}` в `project.json` + `DB_PASSWORD` в `.secrets.env`; `pg_isready` |
| `psql: command not found` | Нет `psql` в PATH (только для seed/DDL) | Установите PostgreSQL client или используйте `python -c "from workspace.utils.db import execute; execute(open('sql/...').read())"` |
| `permission denied for table public.agent_vector_index_store` | Не хватает GRANT | `GRANT INSERT, UPDATE, DELETE ON public.agent_vector_index_store TO <user>` |
| `--status` показывает 0 индексов | `public.agent_vector_index_config` пуст | Применить `sql/audit_analyzer/seed_default_indexes.sql` |
| `ERROR: таблица oarb.audit_vectors не создана` | DDL не применён | `psql -f sql/audit_analyzer/create_oarb_audit_vectors.sql` |

#### Ошибки при сборке

| Симптом | Причина | Что делать |
|---------|---------|-----------|
| `TypeError: cannot use 'dict' as a dict key` | `embedding_cols` содержит dict-объекты без нормализации | Уже исправлено в `_normalize_cols()` (`tools/build_vectors.py`); если повторилось — обновите код |
| `column "description" does not exist` | Колонка указана в `embedding_cols`, но отсутствует в источнике | Проверьте `\d oarb.violations`; удалите колонку из конфига или ALTER TABLE |
| `column "updated_at" does not exist` (при `_filter_unchanged`) | `track_column` отсутствует в источнике | Укажите существующую колонку или добавьте `updated_at` через `ALTER TABLE ... ADD COLUMN updated_at TIMESTAMPTZ DEFAULT NOW()` |
| `psycopg2.errors.StringDataRightTruncation` при INSERT | Длина `search_text` > ограничения TEXT | Это не должно происходить (TEXT без лимита); если происходит — `ALTER TABLE oarb.audit_vectors ALTER COLUMN search_text TYPE TEXT` |
| `duplicate key value violates unique constraint` | Параллельный запуск `build_vectors.py` | Запускайте только один экземпляр; для cron используйте flock |
| `httpx.ConnectError: [Errno 111] Connection refused` | Ollama не запущена | `systemctl start ollama` (или запустить вручную); проверить `curl http://localhost:11434` |
| `httpx.HTTPStatusError: 404 Not Found` от Ollama | Модель не загружена | `ollama pull mxbai-embed-large:latest` |
| `httpx.HTTPStatusError: 500 Internal Server Error` | Ollama не справилась с запросом (длинный текст, OOM) | Уменьшите `--batch-size`, разбейте длинные тексты чанками меньшего размера |
| Все строки в `errors`, 0 вставлено | Ollama возвращает ошибку на каждый запрос | Проверьте `ollama logs`; возможно, текст содержит невалидные символы или модель не загружена |
| `Все индексы актуальны, синхронизация не требуется` (а должна быть) | Сигнатура совпадает: `COUNT + MAX(track_column)` одинаковые | Проверьте: `SELECT COUNT(*), MAX(updated_at) FROM oarb.<table>` — если `MAX` старее последнего изменения, добавьте триггер `BEFORE UPDATE` на обновление `updated_at` |
| Бесконечный `Retry N/3 через Nс` | Ollama недоступна, retry безуспешны | Проверьте Ollama; `--check` лучше `--full-rebuild` для cron |
| `Ошибка удаления pk=X` при инкрементальной сборке | Строки были удалены из источника и из `audit_vectors`, но транзакция прервалась | Запустите снова: идемпотентно, дойдёт до консистентного состояния |
| `psycopg2.errors.InvalidTextRepresentation` | Невалидный UTF-8 в строке источника | Очистите данные в источнике: `UPDATE oarb.<table> SET col = regexp_replace(col, '[\\x00-\\x08\\x0B-\\x1F]', '', 'g')` |

#### Проблемы с FAISS-поиском

| Симптом | Причина | Что делать |
|---------|---------|-----------|
| `RuntimeError: Error in faiss::IndexFlat::search: index has 0 vectors` | FAISS-индекс пуст | `python tools/build_vectors.py --status` — если `vector_count=0`, пересоберите `--full-rebuild` |
| `RuntimeError: Error in faiss::IndexFlat::add: dimension mismatch` | Размерность FAISS ≠ размерности эмбеддинга запроса | Модель Ollama изменилась, а конфиг/project.json — нет. Обновите `embedding_dimension` и `--full-rebuild` |
| Все результаты с `score=0.000` | FAISS устарел (новые вектора в `audit_vectors` не пересобраны в FAISS) | `python tools/build_vectors.py --full-rebuild` |
| Все результаты возвращают `row_data=None` | Поле `row_data` не пишется в INSERT | Проверьте `INSERT` в `tools/build_vectors.py:392-405`; у вас должна быть колонка `row_data JSONB` |
| Поиск возвращает результаты из другой таблицы | `embedding_cols` конфликтуют между индексами (один и тот же текст в разных таблицах) | Используйте разные `index_name` и проверьте через `SELECT DISTINCT source FROM oarb.audit_vectors` |
| Поиск по `violations_index` возвращает нарушения из всех проверок сразу | Индекс не фильтрует по `audit_id` | По умолчанию семантический поиск не фильтрует; для фильтрации нужен префикс в `--query` (например, `audit_id:5 ...`) — **это расширение, не реализовано** |
| Поиск очень медленный (>1 сек на запрос) | FAISS не в памяти, пересобирается из БД каждый раз | `provider._INDEX_CACHE` пуст; gateway должен делать `preload_indexes()` при старте |

#### Проблемы с конфигурацией индексов

| Симптом | Причина | Что делать |
|---------|---------|-----------|
| `embedding_cols` содержит `[]` (пустой массив) | Все строки молча игнорируются (нет search_text) | Заполните конфиг: `UPDATE ... SET embedding_cols = '["title"]'::jsonb` |
| `embedding_cols` содержит колонку с NULL для всех строк | `_build_search_text` возвращает `""` → строка пропускается | Проверьте `SELECT col, COUNT(*) FROM table GROUP BY col`; используйте только заполненные колонки |
| `content_cols` пуст | INSERT упадёт или `content` будет NULL | Заполните `content_cols` хотя бы одной колонкой |
| `pk_column` — UUID или TEXT | `pk_value INTEGER` в `oarb.audit_vectors` не вместит | Сейчас поддерживается только INTEGER; для UUID нужен ALTER: `ALTER TABLE oarb.audit_vectors ALTER COLUMN pk_value TYPE TEXT USING pk_value::TEXT` |
| `track_column = NULL` для всех строк | `_filter_unchanged` пропускает индекс | Используйте другую track_column или добавьте заполнение: `UPDATE table SET updated_at = NOW() WHERE updated_at IS NULL` |
| В конфиге 2 индекса на одну таблицу с разными `embedding_cols` | Поддерживается, но FAISS общий | Создайте два индекса с разными `index_name`, проверьте через `provider.search_vector(index_name=...)` |
| DROP COLUMN в источнике | `embedding_cols` ссылается на несущую колонку → ошибка чтения | Обновите конфиг: `UPDATE ... SET embedding_cols = '[...]'::jsonb WHERE index_name = '...'` |
| `updated_at` не обновляется при UPDATE | Нет триггера `BEFORE UPDATE` | `CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS TRIGGER AS $$ BEGIN NEW.updated_at = NOW(); RETURN NEW; END $$ LANGUAGE plpgsql; CREATE TRIGGER ... BEFORE UPDATE ON oarb.<table> FOR EACH ROW EXECUTE FUNCTION touch_updated_at();` |

#### Размерность и совместимость моделей

| Модель Ollama | Размерность | По умолчанию в конфиге |
|---------------|-------------|------------------------|
| `mxbai-embed-large:latest` | 1024 | да (дефолт) |
| `nomic-embed-text:latest` | 768 | нет |
| `all-minilm:latest` | 384 | нет |
| `snowflake-arctic-embed:latest` | 1024 | нет |
| `bge-m3` | 1024 | нет |

**Если меняете модель:**

```jsonc
// project.json
"embedding_model":     "nomic-embed-text:latest",  // было mxbai-embed-large:latest
"embedding_dimension": 768,                          // было 1024
```

После смены **обязательно**:
```bash
# 1. Удалить старые FAISS (они имеют старую размерность)
psql -c "DELETE FROM public.agent_vector_index_store"
psql -c "TRUNCATE oarb.audit_vectors"

# 2. Пересобрать с новой моделью
python tools/build_vectors.py --full-rebuild

# 3. Проверить что размерности совпадают
python tools/build_vectors.py --status
# dim должен быть 768
```

**Если размерность в конфиге не совпадает с реальной Ollama** — FAISS будет собран с правильной размерностью, но `--status` покажет неправильную. Проверяйте вручную:

```bash
curl -X POST http://localhost:11434/api/embed \
     -d '{"model":"<model>","input":["test"]}' | jq '.embeddings[0] | length'
```

### Расширенные сценарии

**Добавить колонку для индексации без пересоздания:**

```sql
UPDATE public.agent_vector_index_config
SET embedding_cols = embedding_cols || '["new_column"]'::jsonb
WHERE index_name = 'audits_index';
```

Затем **обязательно** `--full-rebuild` (т.к. изменился `content_hash` → все строки CHANGED).

**Полная очистка и пересоздание:**

```sql
TRUNCATE oarb.audit_vectors;
TRUNCATE public.agent_vector_index_store;
```

```bash
python tools/build_vectors.py --full-rebuild
```

**Массовое обновление embedding_cols:**

```sql
-- Увеличить размер чанка для всех индексов с чанкованием
UPDATE public.agent_vector_index_config
SET embedding_cols = jsonb_set(
    embedding_cols,
    '{0,chunk_size}',
    '800',
    false
)
WHERE jsonb_typeof(embedding_cols->0) = 'object'
  AND embedding_cols->0->>'column' IN ('description', 'full_text');
```

После — `--full-rebuild` для затронутых индексов.

### Edge cases и редкие сценарии

#### Несколько индексов на одну таблицу

Поддерживается. Например, `audits_summary_index` (только title) и `audits_full_index` (description чанковано):

```sql
INSERT INTO public.agent_vector_index_config (..., index_name, embedding_cols, enabled) VALUES
('audits_summary_index', 'audits', 'oarb.audits', 'id',
 ARRAY['title']::text[],
 '["title"]'::jsonb, 'updated_at', true),
('audits_full_index', 'audits', 'oarb.audits', 'id',
 ARRAY['title','description']::text[],
 '[{"column":"description","chunk":true,"chunk_size":500,"chunk_overlap":80},"title"]'::jsonb,
 'updated_at', true);
```

Поиск: `audit_analyze.bat --mode vector --query "..." --index-name audits_full_index --top-k 5`.

#### Инкрементальная сборка vs `--check` — разница

| Сценарий | Команда | Что делает |
|----------|---------|-----------|
| Быстрая проверка без записей | `python tools/build_vectors.py --check` | Сравнивает сигнатуру `(count, MAX(track))` → запускает инкрементальную сборку только если diff |
| Полная проверка и сборка | `python tools/build_vectors.py` (без флагов) | Загружает все строки, классифицирует NEW/CHANGED/DELETED, собирает |
| Принудительная полная перестройка | `python tools/build_vectors.py --full-rebuild` | TRUNCATE индекса + все строки заново |

**`--check` НЕ помогает, если изменения в embedding_cols** (сигнатура источника не меняется). Используйте `--full-rebuild` или `--index <name> --full-rebuild` после изменения конфига.

**`--check` пропускает индекс если `track_column` NULL** (MAX возвращает NULL → сравнение `0|` с `0|` = совпадение). Используйте непустую track_column.

#### Параллельный запуск (concurrency)

`build_vectors.py` использует `DELETE + INSERT` без блокировок. Параллельный запуск на одном индексе приведёт к:

- `psycopg2.errors.UniqueViolation` на `id SERIAL`
- Потерянным изменениям (один из процессов перезатрёт другого)

**Решения:**

```bash
# Через flock (cron-friendly)
flock -n /var/lock/build_vectors.lock python tools/build_vectors.py --full-rebuild

# Через .pid файл
[ -f /tmp/build_vectors.pid ] && kill -0 $(cat /tmp/build_vectors.pid) 2>/dev/null && exit 1
echo $$ > /tmp/build_vectors.pid
python tools/build_vectors.py --full-rebuild
rm /tmp/build_vectors.pid
```

Параллельная сборка **разных** индексов безопасна (разные `source`).

#### Миграция со старой версии (v1.x → v2.0.0)

Если у вас остались FAISS-индексы в файлах `.faiss` (не в БД) — мигрируйте:

```bash
# 1. Применить новые DDL (если ещё не)
psql -f sql/audit_analyzer/create_oarb_audit_vectors.sql
psql -f sql/audit_analyzer/create_public_agent_vector_index_config.sql
psql -f sql/audit_analyzer/create_public_agent_vector_index_store.sql

# 2. Зарегистрировать индексы в public.agent_vector_index_config
psql -f sql/audit_analyzer/seed_default_indexes.sql

# 3. Пересобрать (старые файлы .faiss будут проигнорированы)
python tools/build_vectors.py --full-rebuild

# 4. Удалить старые файлы
rm -rf ~/.nanobot/workspace/skills/audit_analyzer/cache/*.faiss
```

#### Что делать при ошибке посреди `--full-rebuild`

`--full-rebuild` сначала делает `DELETE FROM oarb.audit_vectors WHERE source = X` (строка 308-316), затем собирает. Если сборка упадёт посередине (например, Ollama недоступна) — индекс окажется в неполном состоянии.

**Решение:**

```bash
# Просто перезапустить — операция идемпотентна:
python tools/build_vectors.py --index audits_index --full-rebuild
# Сначала TRUNCATE, потом заново INSERT
```

**Не нужно:** `TRUNCATE oarb.audit_vectors` — `--full-rebuild` уже делает DELETE перед сборкой.

#### Один и тот же текст в нескольких индексах

Если `oarb.violations.description` индексируется и в `violations_index`, и в `audit_full_index` — FAISS-поиск может вернуть один и тот же документ дважды. **Дедупликация по `pk_value + source`** — ответственность вызывающего кода.

#### Эмбеддинг для разных моделей

Каждый индекс эмбеддится **одной моделью** (из `project.json → skills.audit_analyzer.embedding_*`). Разные модели для разных индексов **не поддерживаются** через конфиг — только глобально.

Если нужна разная размерность для разных индексов — нужен рефакторинг `cache_provider_impl.py:PostgresDuckDbProvider` (per-index `embedding_base_url/model`).

#### Обновление без пересборки (in-place)

Если хотите обновить FAISS в памяти после изменения `audit_vectors` без полного пересбора:

```python
from workspace.skills.audit_analyzer.scripts.skill_config import build_cache_provider
provider = build_cache_provider()

# Сбросить in-memory FAISS для одного индекса (перечитает из public.agent_vector_index_store)
provider.invalidate_cache('audits_index')

# Принудительно пересобрать (заново прочитает audit_vectors и сериализует)
provider.rebuild_and_store_index('audits_index', 'oarb.audit_vectors')
```

#### Graceful degradation в навыке

`audit_analyzer` (CLI `--mode vector`) при сбое эмбеддинга возвращает `[]` без падения:

```python
embedding = get_embedding(query, url, model)
if embedding is None:
    return []   # ← здесь
```

Если в логах навыка видите `Ошибка эмбеддинга после 3 попыток` — ищите проблему в Ollama, а не в навыке.

#### Большие источники и память Ollama

| Размер источника | `--batch-size` | Время Ollama | Память |
|-----------------|----------------|-------------|--------|
| <1000 строк | 16 (дефолт 10) | минуты | <2 GB |
| 1k–10k строк | 16 | десятки минут | 2–4 GB |
| 10k–100k строк | 8 + `--chunk-size 300` | часы | 4–8 GB |
| >100k строк | 4 + `--chunk-size 200` | дни | 8+ GB |

**Мониторинг во время сборки:**

```bash
# Размер загруженных моделей Ollama
ollama ps

# Логи Ollama (в реальном времени)
journalctl -u ollama -f
```

#### Что если Ollama медленная?

`-chunk-size 200 -batch-size 4 -overlap 50` — снижает нагрузку.

#### Параллельные запуски в gateway

`AuditSyncService` и `build_vectors.py` могут работать одновременно. Они **не конфликтуют** (разные таблицы: `oarb.audit_vectors` и доменные таблицы аудита), но:

- Если источник (`oarb.audits`) сильно меняется во время `--full-rebuild` — могут появиться пропущенные строки (сигнатура уже посчитана).
- Решение: запускать `build_vectors.py` в период минимальной нагрузки (ночью).

#### Когда `--full-rebuild` медленный

- **Ollama медленная** → уменьшите batch-size.
- **Источник огромный** → запускайте по одному индексу: `--index <name> --full-rebuild`.
- **Сеть до PostgreSQL медленная** → проверьте DSN, используйте локальную БД.
- **Disk I/O на запись в `audit_vectors`** → 100k+ строк = много INSERT; используйте `--batch-size` побольше (32-64) для меньшего числа батчей (но больше памяти Ollama).

#### Когда НЕ нужен `--full-rebuild`

Если добавилась **одна колонка** в источник и она в `embedding_cols` — без `--full-rebuild` строки не пересоберутся (content_hash изменится, но `tools/build_vectors.py` сравнивает по `(source, pk_value)` и content_hash — он увидит diff и обработает). **Проверьте:** добавьте колонку, запустите без `--full-rebuild`, проверьте `audit_vectors.content_hash`.

#### Особые случаи с Ollama моделями

| Проблема | Решение |
|----------|---------|
| Модель `mxbai-embed-large` не поддерживает батчи >32 | `--batch-size 16` (уже дефолт) |
| Модель требует префикс `query:` или `passage:` (ColBERT-style) | Добавьте префикс в `_build_search_text()` перед отправкой |
| Модель возвращает разные размерности для разных текстов | Не поддерживается; проверьте `len(data["embeddings"][0])` — должно быть константой |
| Ollama отвечает `embedding: null` (модель не загружена) | `ollama pull mxbai-embed-large:latest` |
| Ollama требует больше памяти (большие чанки) | Уменьшите `--chunk-size` или `--batch-size` |

#### Совместимость с PostgreSQL 13+

`oarb.audit_vectors.embedding REAL[]` — нативный PostgreSQL массив. Работает на 9.4+. На Greenplum 6.25 — поддерживается.

Если мигрируете на старый PG 9.4 — может потребоваться замена `REAL[]` на `numeric[]` или `double precision[]` (см. DDL).

#### JSONB в Greenplum 6.25

`embedding_cols JSONB` и `row_data JSONB` работают на GP 6+. Если на старом GP (5.x) — нужна миграция на `TEXT`.

#### Безопасность и секреты

`build_vectors.py` использует `DATABASE_URL` через `utils.db.resolve_dsn()` — никаких секретов в коде или логах.

Логи `build_vectors.py` могут содержать **содержимое строк** (превью `content[:60]`) — если источник содержит PII (персональные данные), это утечка. Решение — закомментируйте превью в `_get_embeddings` или обфусцируйте.

#### Когда все сломалось — пересоздание с нуля

```bash
# 1. Удалить конфиг индексов
psql -c "TRUNCATE public.agent_vector_index_config CASCADE"

# 2. Удалить собранные вектора
psql -c "TRUNCATE oarb.audit_vectors"
psql -c "TRUNCATE public.agent_vector_index_store"

# 3. Заново применить seed
psql -f sql/audit_analyzer/seed_default_indexes.sql

# 4. Пересобрать
python tools/build_vectors.py --full-rebuild
```

---

## 🛠 tools/ — инфраструктурные утилиты

В корне `tools/` живут CLI-утилиты, **отдельные от навыков** — инфраструктура, не аналитика.

### `tools/build_vectors.py`

Перестроение векторных индексов из PostgreSQL-данных. **Полная документация — в [Векторная индексация](#векторная-индексация)**, включая:

- как добавить/обновить/удалить индекс,
- формат `embedding_cols` (с чанкованием и без),
- алгоритм сборки и классификации NEW/CHANGED/DELETED,
- типичные проблемы и их решения,
- мониторинг через SQL-запросы.

Краткая шпаргалка по флагам:

```bash
# Статус без изменений
python tools/build_vectors.py --status

# Полная перестройка всех индексов (осторожно: долго + нагрузка на Ollama)
python tools/build_vectors.py --full-rebuild

# Только проверка сигнатуры (COUNT + MAX track_column) — для cron
python tools/build_vectors.py --check

# Один индекс
python tools/build_vectors.py --index audits_index

# Dry-run без записи в БД
python tools/build_vectors.py --dry-run

# Параметры эмбеддинга (пауза между запросами + ожидание перед повтором при ошибке)
python tools/build_vectors.py --batch-size 32 --chunk-size 500 --chunk-overlap 80
python tools/build_vectors.py --pause-sec 3 --embedding-retry-wait 5

# Другая таблица векторов
python tools/build_vectors.py --db-table my_app.vectors

# Подробный лог (DEBUG): конфиг, каждый чанк/строка — для диагностики
python tools/build_vectors.py --verbose
```

| Флаг | Дефолт | Описание |
|------|--------|----------|
| *(без флагов)* | — | Инкрементальная синхронизация (NEW / CHANGED / DELETED) |
| `--full-rebuild` | — | Полная перестройка (TRUNCATE индекса + все строки) |
| `--check` | — | Сравнить сигнатуру (count distinct pk + max track); синхронизировать только при diff |
| `--status` | — | Сводное состояние индексов без синхронизации |
| `--dry-run` | — | План без записей в БД |
| `--index <name>` | все | Собрать только индекс `name` |
| `--db-table` | `oarb.audit_vectors` | Таблица сырых векторов |
| `--batch-size` | 10 | Батч эмбеддинга |
| `--chunk-size` | 500 | Размер чанка в символах |
| `--chunk-overlap` | 80 | Перекрытие чанков |
| `--embedding-retry-wait` | 5 | При ошибке получения эмбеддинга: ждать это время (сек) и повторить один раз |
| `--verbose` | — | Подробный лог каждого чанка/строки (уровень DEBUG) |

**Логирование.** Все сообщения идут через `loguru` в stderr (без ANSI-цветов,
удобно при `>> build.log 2>&1`) и разбиты по этапам: конфиг → состояние
БД/источника → классификация (новые/изменённые/удалённые) → удаление →
чанки → эмбеддинг с прогрессом → пересборка FAISS → итог. Ошибка любого
этапа печатается с traceback, поэтому падение без причины маловероятно;
сбой отдельного индекса не роняет весь прогон (фиксируется в сводке `ИТОГО`).

**Гарантии инкрементальной сборки:**
- `pk_value` сравнивается как строка (`TEXT` в БД vs числовой PK в источнике
  нормализуются через `_norm_pk`) — детект CHANGED/DELETED работает, а не
  переписывает индекс на каждом запуске.
- CHANGED-строки: сначала вставляются новые чанки, старые удаляются
  **после** успешной вставки (`DELETE ... content_hash <> <new>`). Если
  эмбеддинг упал — старый вектор сохраняется (без потери данных).
- Быстрая проверка `--check` использует `COUNT(DISTINCT pk_value)`, поэтому
  чанкование (несколько чанков на строку) не заставляет `--check` всегда
  запускать синхронизацию.

**Важно:** при первом запуске проверить, что установлены зависимости FAISS:
`pip install faiss-cpu numpy`. Без них вектора вставляются в `audit_vectors`,
но `public.agent_vector_index_store` остаётся пустой, и `--mode vector` поиск
через `lib/services/cache_provider_impl.py` не работает.

**Типичные сценарии:**
- **После изменений в DDL таблиц** — `--full-rebuild`.
- **Проверка готовности системы** (cron / healthcheck) — `--check`.
- **Мониторинг без записи** — `--status`.
- **Большой источник + экономия памяти Ollama** — `--batch-size 8` + `--chunk-size 300`.

## 🗃 SQL-скрипты: создание таблиц

Все DDL собраны в корневом каталоге [`sql/`](sql/). Каталог, порядок применения, совместимость — в [`sql/README.md`](sql/README.md). Здесь только краткая сводка по новой структуре v2.

### Совместимость с Greenplum 6.5+

В v2.0.0 таблицы `oarb.audit_vectors` и `oarb.vector_index_*` переработаны для полной совместимости с **Greenplum 6.5** (PostgreSQL 9.4 ядро).

| Изменение | Было (v1.5) | Стало (v2.0) | Зачем |
|-----------|--------------|---------------|-------|
| `id` тип | `SERIAL` (int4 + sequence) | `BIGINT GENERATED BY DEFAULT AS IDENTITY` | Нет переполнения на 2.1B записей |
| `pk_value` тип | `INTEGER` | `TEXT` | Поддержка UUID, BIGINT и других PK |
| Распределение GP | нет (рандом по 1-й колонке) | явные `DISTRIBUTED BY (source)` / `REPLICATED` | Контролируемая сегментация, локальные JOIN |
| Индексы `audit_vectors` | 2 (source, source+pk) | 3 (+source+synced, +source+hash) | Быстрые запросы «последние sync», дедупликация |

**Использование:**

| СУБД | Файлы |
|------|-------|
| Все (PG/GP) | `sql/audit_analyzer/create_<schema>_<table>.sql` — один файл на таблицу, Greenplum 6.5 (`DISTRIBUTED BY`) |

**Миграция со старой версии:** скрипты миграции векторов удалены (v2.2.0).
Примените актуальные DDL из `sql/audit_analyzer/` и пересоберите индексы:
`python tools/build_vectors.py --full-rebuild`.

⚠️ Миграция удаляет данные в `audit_vectors` и `agent_vector_index_store`. После ОБЯЗАТЕЛЬНО:

```bash
python tools/build_vectors.py --full-rebuild
```

### Структура v2.0

```sql
-- PG 13+ вариант
public.agent_vector_index_config (
    index_name      TEXT PRIMARY KEY,         -- + 8 полей
    embedding_cols  JSONB NOT NULL,
    ...
);

public.agent_vector_index_store (
    source       TEXT PRIMARY KEY,
    index_binary BYTEA NOT NULL,
    ...
);

oarb.audit_vectors (
    id             BIGINT IDENTITY PRIMARY KEY,
    pk_value       TEXT,                     -- было INTEGER
    embedding      REAL[] NOT NULL,
    source         TEXT NOT NULL,
    ... + 3 индекса
);
```

```sql
-- GP 6.5 вариант — добавляется распределение:
DISTRIBUTED BY (index_name);     -- agent_vector_index_config
DISTRIBUTED REPLICATED;          -- agent_vector_index_store (полная копия на каждом сегменте)
DISTRIBUTED BY (source);         -- audit_vectors
```

### Ограничения GP 6.5

- **BYTEA**: до ~1GB на сегмент. Для индексов >1M векторов × 1024 dim = ~400MB OK.
  Для >2.5M × 1024 dim — нужен партиционирование (выходит за рамки).
- **REAL[]**: до ~1GB на массив. Для 1024-dim = ~1M векторов на сегмент OK.
- **`bigserial`**: не поддерживается, используйте `BIGINT IDENTITY`.

### Известные несовместимости с PG

- `DISTRIBUTED BY` — только GP. PG падает с `syntax error at or near "DISTRIBUTED"`.
- `DISTRIBUTED REPLICATED` — только GP. PG не поддерживает.
- `GENERATED BY DEFAULT AS IDENTITY` — PG 10+. Старые PG 9.4-9.6 нуждаются в `SERIAL`.

---

## 🔗 Полная таблица связей между файлами (v2.0.0)

### Точки входа (тонкие оркестраторы)

| Файл | Строк | Что делает | Настраивается через |
|------|------:|-----------|-------------------|
| `gateway.py` | 232 | Сервер: каналы, Streamlit, FAISS preload, restart-loop. Токены LLM-итераций — опционально через `gateway.print_llm_calls` (`project.json`, `false` по умолчанию) → `ApplicationContext.create(print_llm_calls=...)`. Активность пула воркеров — через `gateway.print_worker_activity` → `ChannelFactory(print_worker_activity=...)` → `PostgresChannel` | `project.json` (`channels.*`, `gateway`, `logging.db`) |
| `cli_agent.py` | 165 | REPL: ввод → `MessageBus` → `AgentLoop`. Запускает `ApplicationContext.create(print_llm_calls=True)` — в терминале выводятся токены LLM-итераций (`→ LLM: отправлен промпт (X токенов)` / `← LLM: получен ответ (Y токенов)`) | CLI-аргументы, `project.json` (`cli`) |
| `streamlit_app.py` | 502 | Тонкий web-клиент (НЕ через ApplicationContext) | `project.json` → `channels.postgres`, `streamlit` |

### Bootstrap и сервисный слой

| Файл | Что делает |
|------|-----------|
| `lib/core/application_context.py` |  Единый bootstrap всех общих сервисов |
| `lib/core/agent_factory.py` |  Создание AgentLoop: `ToolAuditHook` в `hooks=`, `DatabaseLoggingHook` — как фабрика оборота в `hook_factories=` |
| `lib/core/bus_factory.py` |  MessageBus + обёртки publish_inbound/outbound |
| `lib/services/config_service.py` |  Загрузка конфига, SETTINGS-аксессор, pre-resolve env, таймауты |
| `lib/services/session_storage.py` |  Выбор PGSessionManager / SessionManager |
| `lib/services/runtime_patcher.py` |  Все monkey-patch'и (ContextGovernor + _assemble_outbound) |
| `lib/services/channel_factory.py` |  ChannelManager + Redis/Postgres |
| `lib/services/transcription_service.py` |  openai/groq key/URL/language |
| `lib/services/subprocess_manager.py` |  Streamlit spawn + terminate/kill |
| `lib/services/preload_service.py` |  FAISS preload + audit_cache refresh |
| `lib/services/db_logging_service.py` |  Worker-поток, batch INSERT через общий пул `utils.db`, без JSONL-fallback. Событие `llm_call` (`log_llm_call`) пишет полный промпт + ответ LLM в `payload` |
| `lib/services/db_logging_bus.py` |  Обёртки publish_inbound/outbound для логгера |
| `lib/cli/console_loop.py` |  REPL/typewriter/consume_outbound (вынесено из cli_agent.py) |
| `lib/cli/display_config.py` |  DisplayConfig |
| `lib/cli/hook_loader.py` |  Сканирование workspace/hooks/*.py (плагины) |
| `lib/hooks/base_tool_tracking_hook.py` |  Общий каркас для tool-хуков |
| `lib/hooks/tool_audit_hook.py` |  Хук аудита вызовов инструментов |
| `lib/hooks/database_logging_hook.py` |  AgentHook для tool-событий + run_finished + `llm_call` (полный промпт/ответ на итерацию через `before_iteration`/`after_iteration`); при `print_llm_calls=True` выводит в терминал токены итерации (включается в CLI через `ApplicationContext.create(print_llm_calls=True)`); per-turn инстансы через `make_db_logging_hook_factory` (конкурентно-безопасно) |
| `lib/lifecycle/gateway_runner.py` |  Цикл с exponential backoff |
| `lib/lifecycle/shutdown_coordinator.py` |  LIFO graceful shutdown |
| `workspace/hooks/session_file_redirect_hook.py` |  AgentHook: перенаправляет `write`/`edit`/`create_file`/`write_file` и `media` тула `message` в `data_store/cache/sessions/<session_key>/` (политика хранения в `workspace/AGENTS.md`) |
| `workspace/hooks/recent_files_hook.py` |  Сбор созданных файлов для auto-attach в `OutboundMessage.media` |

### Pre-existing (не тронуты рефакторингом)

| Файл | Что делает |
|------|-----------|
| `lib/session/pg_session_manager.py` | Хранение сессий в PostgreSQL (без JSONL-fallback) |
| `lib/channels/postgres_channel.py` | Канал через таблицу agent_conversation_messages |
| `lib/channels/redis_channel.py` | Канал через Redis-очереди (BRPOP/LPUSH) |
| `lib/services/audit_sync_service.py` | Синхронизация audit-таблиц из PG в in-memory DuckDB (SQL через общий пул `utils.db`) |
| `lib/services/audit_memory_store.py` | DuckDB-кеш + FAISS-индексы + publish-snapshot |
| `lib/services/cache_provider.py` | Интерфейс CacheProvider + SearchResult |
| `lib/services/cache_provider_impl.py` | Реализация кеша (PostgresDuckDbProvider) |
| `lib/services/text_splitter.py` | Чанкование текстов |
| `workspace/utils/db.py` | Общий пул соединений PG: одна очередь + воркеры (1..N), sync/async API, транзакции-аренда (`lease`); неподключённые воркеры уступают очередь подключённым |
| `workspace/skills/audit_analyzer/` | Навык: тонкий CLI поверх `lib/services` |

### Где что править

| Компонент | Что нужно сделать | Файл | Если сломалось — где смотреть |
|-----------|-----------------|------|------------------------------|
| **Конфиг** | Сменить модель/провайдера | `config.json` → `agents.defaults.model` | `ValueError: LLM_API_KEY` → `.secrets.env` (секция `providers: llm`); gateway не находит ключ → `lib/services/config_service.py:_pre_resolve_env_refs` |
| **Конфиг** | Настроить таймауты | `project.json` → секции `gateway`, `cli` или `streamlit` | LLM-запросы висят → `cli.llm_timeout` / `gateway.llm_timeout`; exec-команды обрываются на 60с → `tools.exec.timeout` (`config.json`) |
| **Каналы / БД** | Подключение к БД | `project.json` → `channels.postgres` (`host`/`port`/`dbname`/`user` + опц. `dsn`) | `psycopg2.OperationalError` / `connection refused` → `DB_PASSWORD` в `.secrets.env` (DSN собирается в `utils.db.resolve_dsn()`); `gssencmode` ошибка на GP 6.25 → `lib/services/config_service.py` (kwargs `connect()`); `too many connections` → общий пул в `workspace/utils/db.py` (`channels.postgres.pool` → `min_conn`/`max_conn`/`pool_timeout`); проверить лимит честно можно через не-суперюзерную роль (`ALTER ROLE <role> CONNECTION LIMIT N`) — на superuser роли лимит PostgreSQL игнорирует |
| **Каналы** | Включить Redis-канал | `project.json` → `channels.redis.enabled` | `Connection refused` → `host`/`port`/`password`; не приходят сообщения → `lib/channels/redis_channel.py` + `allow_from` |
| **Навыки** | Настроить навык | `project.json` → `skills.<имя>` | Навык не подхватывается → `agents.defaults.disabledSkills` (`config.json`); навык стартует со старыми параметрами → `lib/services/runtime_patcher.py` (см. `RuntimePatcher.apply_all`) |
| **Секреты** | Добавить API-ключ | `.secrets.env` (провайдер-скоупинг формат) | `nanobot._load_runtime_config` падает с `ValueError` → `lib/services/config_service.py:_pre_resolve_env_refs` (должен подставить `${VAR}` в `os.environ` ДО nanobot) |
| **Логирование** | БД-логирование | `project.json` → `logging.db` (`enabled`, `flush_interval_sec`, `batch_size`, `min_level`) | В таблице `gateway_logs` пусто → `lib/services/db_logging_service.py:get_stats()` (`queue_size`, `connected`, `last_error`); при недоступности БД события дропаются (счётчик `failed`). Полный промпт и ответ LLM на каждую итерацию — событие `llm_call` (payload: `prompt`/`response`, metadata: `iteration`/`model`/`finish_reason`/`usage`) |
| **Сервисный слой** | Сервисный слой | `lib/services/<service>.py` (например, `db_logging_service.py`) | `ctx.start()` падает → сервис в `None` (graceful degradation, см. `lib/core/application_context.py:create`); race-condition `нет данных в кэше` → callbacks на `AuditSyncService` ДО `ctx.start()` (см. `gateway.py:main`) |
| **Bootstrap** | Bootstrap | `lib/core/application_context.py` | Контекст не создаётся → `lib/core/application_context.py:create` + флаги `enable_db_logging`/`enable_audit`; double-init воркеров → `lib/lifecycle/shutdown_coordinator.py` |
| **Lifecycle** | Lifecycle (backoff/shutdown) | `lib/lifecycle/gateway_runner.py` / `shutdown_coordinator.py` | Gateway зацикливается на рестартах → `GatewayRunner.run_forever` (exponential backoff 1с→30с); процесс не умирает по Ctrl-C → `ShutdownCoordinator` (LIFO) |
| **Каналы** | Канал связи | Написать класс унаследовав `BaseChannel`, подключить через `lib/services/channel_factory.py` | Сообщения не доходят → `allow_from` в `project.json`; reasoning не пишется → `PostgresChannel._flush_reasoning` (период `flush_interval`) |
| **Хуки** | Хук агента | Создать файл в `workspace/hooks/` с подклассом `AgentHook` | Хук не вызывается → `lib/services/agent_factory.py:AgentFactory.create` (lazy-import); `ImportError` из хука → `try/except` в `AgentFactory` (хук/фабрика просто не подключится) |
| **Хуки** | Перенаправление файлов сессии | `workspace/hooks/session_file_redirect_hook.py` (подключается автоматически через `lib/core/application_context.py:ApplicationContext.create` → `lib.cli.hook_loader.scan_and_register`; плагины передаются в `AgentFactory.create(project_hooks=...)`, который один раз вызывает `AgentLoop.from_config(hooks=merged, hook_factories=...)`) | Файлы уходят в корень workspace → проверить, что хук инстанцировался: `ApplicationContext.create` печатает один раз `Hooks connected: RecentFilesHook, SessionFileRedirectHook, ToolAuditHook` (полный список подключённых хуков — плагины + фреймворковые + per-turn factories; сканер успех молчит); whitelist пропускает `AGENTS.md`/`lib/`/`data_store/`/`*.py` — добавить в `_ALLOWED_PREFIXES` если нужно; не работает на `exec`-redirects (`>`, `>>`) — это вне `write`/`edit`; для тула `message` хук перенаправляет и `media`: ищет файл в текущей session-папке по относительному пути и по basename (включая `attachments/`, `results/`) и подставляет реальный — закрывает `Media file not found, keeping path` при attach (агент приложил относительный путь или «абсолютный» путь чужого workspace); URL/`data:`/уже существующие пути не трогаются; если добавляете новый хук в `workspace/hooks/` — он должен быть самодостаточным плагином (контракт `cls(workspace_dir=...)`); больше ничего делать не нужно, он подхватится на следующем старте |
| **Хуки** | Auto-attach созданных файлов в `OutboundMessage.media` | `workspace/hooks/recent_files_hook.py` (тот же auto-scan; `RuntimePatcher._wrap` дренажит `recent_files_hook.drain(session_key)` после `tool_audit_hook.drain`) | Агент создал файл через `write_file`, но забыл приложить в `message()` → auto-attach добавляет; агент приложил несуществующий путь (после SSRF-блокировки `pip install`) → отбрасывается через `Path.is_file()`; агент приложил путь ДО `SessionFileRedirectHook` (basename совпадает, но указанный путь не существует — файл уехал в `data_store/cache/sessions/<key>/`) → auto-attach ЗАМЕНЯЕТ устаревший путь реальным; порядок хуков: `RecentFilesHook` ДО `SessionFileRedirectHook` (тогда `params["path"]` уже финальный к моменту `after_execute_tool`); отключить — передать `recent_files_hook=None` в `RuntimePatcher.apply_all()` |
| **Файл-инструменты** | Контроль `write`/`edit` | `workspace/hooks/session_file_redirect_hook.py` | Без хука работает `data_store/cache/...` по правилу в `workspace/AGENTS.md`, но модель может его забыть; хук закрывает дыру независимо от подсказок в промпте |
| **Бенчмарки** | Тест бенчмарка | YAML-файл в `benchmarks/items/` | Тест падает по `keyword` → перечитать `expect.keywords_include`; `multi_step` не переходит к следующему шагу → `new_session: true` (или `false` для общей истории) |
| **Web UI** | Streamlit UI | `streamlit_app.py` | Чат не отвечает → `streamlit.max_wait` (дефолт 600с) и `poll_interval`; `st.rerun` лимит → блокирующий поллинг в `streamlit_app.py` (без `st.rerun`) |
| **Агент** | Личность агента | `workspace/SOUL.md` | — |
| **Агент** | Инструкции агенту | `workspace/AGENTS.md` | Инструкции не подхватываются → путь `agents.defaults.workspace` (`config.json`); конфликт с глобальным `AGENTS.md` → файлы мерджатся в порядке: `~/.nanobot/AGENTS.md` < `workspace/AGENTS.md` |
| **Навык audit_analyzer** | Навык `audit_analyzer` (общее) | `workspace/skills/audit_analyzer/scripts/` + `lib/services/audit_*` (кеш) | `FileNotFoundError: audit_cache.duckdb` → `python gateway.py` (владелец кеша); LLM 429 → `cli_max_retries` (`project.json`) |
| **Навык audit_analyzer** | Схема таблиц | `workspace/skills/audit_analyzer/scripts/database.py:_fetch_schema` (строки 188-237) | Таблица не видна → `db_tables` в `project.json` + `db_schema`; нет комментариев колонок → `pg_catalog.pg_description.objsubid`; тип `varchar(N)` без длины → `character_maximum_length` в `_fetch_schema` |
| **Навык audit_analyzer** | DuckDB-кеш аудита | `lib/services/audit_memory_store.py` (in-memory) + `lib/services/audit_sync_service.py` (поллинг PG) | `нет данных в кэше` несмотря на строки в PG → callbacks ДО `ctx.start()` (см. `gateway.py:main`); удалённые в PG строки остаются в кеше → `full_resync_every: 0` отключает сверку; файл кеша не обновляется → `publish_path` пуст или `_dirty=False` |
| **Навык audit_analyzer** | Векторный поиск | `lib/services/cache_provider_impl.py` (провайдер) + `tools/build_vectors.py` (индексатор) | `--mode vector` пустой результат → `python tools/build_vectors.py --status` + пересборка `--full-rebuild`; эмбеддинг не строится → Ollama на `embedding_base_url` (дефолт `http://localhost:11434/api/embed`); индекс пересобирается при каждом запросе → `invalidate_cache` не вызван, FAISS не в памяти |

---

## 🧪 Тестирование

```bash
# Юнит-тесты v2.0.0 сервисного слоя (не требуют БД)
python -m pytest tests/test_config_service.py tests/test_session_storage.py \
                    tests/test_runtime_patcher.py tests/test_transcription_service.py \
                    tests/test_channel_factory.py tests/test_subprocess_manager.py \
                    tests/test_preload_service.py tests/test_db_logging_service.py \
                    tests/test_hooks_database_logging.py tests/test_bus_factory.py \
                    tests/test_agent_factory.py tests/test_gateway_runner.py \
                    tests/test_shutdown_coordinator.py tests/test_console_loop.py \
                    tests/test_application_context.py -q

# Пул соединений (mock psycopg2, БД не нужна)
python -m pytest tests/test_utils_db.py -q

# Тесты воркеров (некоторые требуют БД)
python -m pytest tests/test_pg_session_manager.py -q

# Юнит-тесты audit/кэша (sync+memory)
python -m pytest tests/test_audit_memory_store.py tests/test_audit_sync_service.py -q

# Полный набор (без БД; 906 passed)
python -m pytest tests -q

# Сквозной тест навыка (требует живого PostgreSQL)
python workspace/skills/audit_analyzer/tests/e2e_test.py

# Live e2e media-фикса (реальный gateway + живая БД + живой LLM)
# Опт-ин: без NANOBOT_LIVE_E2E=1 тест пропускается. Пишет в изолированную
# таблицу public.agent_conversation_messages_e2e (боевая очередь не трогается).
$env:NANOBOT_LIVE_E2E="1"; python -m pytest tests/test_gateway_live_media_e2e.py -q
```

E2E проверяет все режимы: predefined (реальный SQL по шаблонам), sql
(LLM → EXPLAIN → выполнение), vector (FAISS + Ollama embedding), а также
резолв параметров через семантический поиск.

**Новые test-файлы v2.0.0** (полный список в `README.md`):
- `test_application_context.py` — bootstrap и lifecycle
- `test_config_service.py` — pre-resolve env, таймауты, SETTINGS-аксессор
- `test_session_storage.py` — выбор PG/File/auto
- `test_runtime_patcher.py` — оба monkey-patch'а с fallback
- `test_db_logging_service.py` — worker, batch, без JSONL-fallback
- `test_bus_factory.py` — обёртки publish_inbound/outbound
- `test_console_loop.py` — REPL/typewriter/print_tool_events
- `test_gateway_runner.py` — exponential backoff
- `test_shutdown_coordinator.py` — LIFO graceful shutdown
- `test_subprocess_manager.py` — Streamlit spawn/terminate
- ... и т.д.

> **Стандарт качества тестов (QA-чистка 2026-08-18).** Набор проревизован —
> each test должен давать реальную проверку, а не «галочку». Не оставляем:
> smoke-тесты без `assert` (одно «не должно упасть»), тесты, пересказывающие
> дефолты датаклассов/конструкторов, и тесты, мокающие саму тестируемую
> функцию. Удалено 42 таких теста, исправлен `assert ... if False else True`.
> `test_db_loader.py` намеренно использует `pytest.skip` без DuckDB-кэша —
> это портабельный guard интеграционных тестов, не заглушка.

---

## ➕ Добавление новой настройки

Если вы вводите новый параметр, который раньше был литералом в коде, следуйте правилу:

1. **Объявите ключ в `project.json`** (JSONC, с дефолтом и комментарием) — в подходящей секции (`channels.*`, `skills.*`, `cli`, `gateway`, `logging.db` и т.п.).
2. **Для обязательных настроек навыка `audit_analyzer` используйте
   `lib/services/audit_settings.py` (`require_setting` → `ConfigurationError`)**
   — это единый источник правды без литералов в коде. Для необязательных —
   `config.get_setting(*keys, default=...)`. **Не хардкодьте литерал.**
3. **Добавьте ключ в `REQUIRED_KEYS` в `tests/test_config_keys.py`** — иначе CI не поймает случайное удаление/переименование.
4. **Перезапустите gateway / CLI** после правки `project.json`.

Пример (вынос `max_stuck_retries`):

```json
// project.json
"channels": {
  "postgres": {
    "max_stuck_retries": 3   // Лимит retry зависшего сообщения
  }
}
```

```python
# lib/channels/postgres_channel.py
from config import get_setting
max_retries = get_setting("channels", "postgres", "max_stuck_retries", default=3)
```

```python
# tests/test_config_keys.py → REQUIRED_KEYS
("channels.postgres.max_stuck_retries", 3),
```

Используйте `get_setting()` для вложенных ключей с дефолтом; `SETTINGS.x.y.z` — для горячего чтения без дефолта (если ключ гарантированно есть). Избегайте `cfg.get("key", "default")` без явного пути — это маскирует orphan-ключи.

## 📝 Изменения и миграции

Краткий таймлайн релизов — в [CHANGELOG.md](CHANGELOG.md). Этот раздел — только то, что **требует ручных действий при миграции**.

### Миграция 1.5.0 → 2.0.0

**Конфигурация:**

| Изменение | Действие |
|-----------|----------|
| `.env` → `project.json` + `.secrets.env` | Скопировать секции `channels.*`, `skills.*`, `cli`, `benchmark`, `streamlit`, `gateway` в `project.json` (JSONC). Секреты — в `.secrets.env` с провайдер-скоупинг форматом |
| Провайдерские ключи больше не через `export` | Секция `# providers: llm` с `api_key=...` в `.secrets.env`. `ConfigService._pre_resolve_env_refs` подставит в `os.environ` автоматически (env-переменная — каноническая `LLM_API_KEY`) |
| `vector_indexes` / `mode_vector_index_path` в `config.json` | Удалить; теперь в `public.agent_vector_index_config` (см. [DEVELOPMENT.md → Векторная индексация](#векторная-индексация)) |
| DuckDB-кеш audit_analyzer | CLI запускал загрузку | gateway-only — CLI читает готовый снимок |
| `data-analyzer`, `html_presentation_generator` | Удалены. Убрать из импортов и `config.json` |
| `pg_agent_worker.py` | Удалён. Использовать `streamlit_app.py` + `PostgresChannel` |

**Код (если вы форкали):**

| Что | Изменение |
|-----|-----------|
| `gateway.py` | Было 696 строк, стало 132. Вся инициализация — в `lib/core/ApplicationContext`. Свой код инициализации → переносить в `ApplicationContext.create()` или в новый сервис в `lib/services/` |
| `cli_agent.py` | Было 865 строк, стало 165. То же самое |
| `RuntimePatcher` | Оба monkey-patch'а (`ContextGovernor.normalize_tool_result`, `agent._assemble_outbound`) теперь в `lib/services/runtime_patcher.py` с fallback при изменении API nanobot |
| `DbLoggingService` | Новый. Если раньше логировали вызовы иначе — мигрировать на `lib/services/db_logging_service.py` + `lib/hooks/database_logging_hook.py` |
| Хуки | `lib/hooks/database_logging_hook.py` встроен в `AgentLoop` через `AgentFactory`: общий инстанс заменён на per-turn фабрику `hook_factories=` (см. `database_logging_hook.py:make_db_logging_hook_factory`) |

**Данные:**

- **Сессии** (`public.agent_session_meta`, `public.agent_session_messages`) —
  схема та же. DDL: `sql/session/create_public_agent_session_meta.sql`,
  `sql/session/create_public_agent_session_messages.sql`.
- **Канал** (`public.agent_conversation_messages`) — без миграции (имя уже актуально).
  DDL: `sql/channels/create_public_agent_conversation_messages.sql`.
- **`audit_cache.duckdb`** — gateway пересоздаст автоматически (in-memory → новый snapshot).
- **Векторные индексы** (`oarb.audit_vectors`, `public.agent_vector_index_store`,
  `public.agent_vector_index_config`) — без миграции (1.5.0 уже хранил их в БД);
  DDL в `sql/audit_analyzer/`.
- **Бенчмарки** (`public.agent_benchmark_runs`, `public.agent_benchmark_results`) — без миграции;
  DDL в `sql/benchmarks/`.
- **`agent_gateway_logs` / `agent_question_runs`** — новые таблицы:
  `sql/logs/create_public_agent_gateway_logs.sql`,
  `sql/logs/create_public_agent_question_runs.sql`.

**Что НЕ изменилось:**

- API точек входа: `python gateway.py`, `python cli_agent.py -P`.
- Имена таблиц БД.
- `benchmarks/items/*.yaml` — формат совместим.
- `audit_analyzer` режимы `predefined` / `sql` / `vector`.
- Параметры CLI `audit_analyzer` (`--top-k`, `--threshold`, `--index-name`).

### Краткий таймлайн

| Дата | Версия | Что |
|------|--------|-----|
| 2026-05-25 | 0.9.0 | nanobot-шлюз с `PostgresChannel`, инструментами, конфигурацией workspace |
| 2026-05-27 | 1.0.0 | Навыки `db_analyzer` + `html_presentation_generator`, E2E-тесты |
| 2026-05-27 | 1.1.0 | Модель `gpt-oss:20b-cloud`, кеш схемы в `db_analyzer` |
| 2026-05-29 | 1.2.0 | Streamlit-чат, единотабличная архитектура `agent_conversation_messages` |
| 2026-06-10 | 1.3.0 | Self-review система, `ToolAuditHook`, бенчмарк-фреймворк, Redis-канал |
| 2026-06-16 | 1.4.0 | Переход asyncpg → psycopg2, переименование `db_analyzer` → `audit_analyzer` |
| 2026-07-22 | 1.5.0 | Векторные индексы в PostgreSQL, DuckDB-кеш, файловые → БД-секреты |
| 2026-08-12 | 2.0.0 | `ApplicationContext` + сервисный слой, gateway — владелец кеша, JSONC, удаление навыков |
| 2026-08-14 | 2.0.1 | Fix: gateway DuckDB-snapshot, build_vectors NameError, PostgresChannel ↔ nanobot 0.3.0; SQL: один файл = одна таблица (GP 6.5 only) |
| 2026-08-17 | v2.2.0 | Единый пул PG: DbLoggingService/AuditSyncService/cache_provider на `utils.db`, неподключённые воркеры уступают очередь подключённым; `SessionFileRedirectHook`; schema-meta в кэше; dict-media; удалена write-функциональность `AuditSyncService`; 856 тестов (см. `[2.2.0]` в CHANGELOG.md) |

Подробный changelog — в [CHANGELOG.md](CHANGELOG.md).
