# nanobot — Personal AI Agent (Deployment)

Локальная инсталляция фреймворка **[nanobot-ai](https://github.com/HKUDS/nanobot)**
(PyPI: `nanobot-ai`) с кастомными доработками: канал PostgreSQL, бенчмарки и один
навык — `audit_analyzer` (домены `legal_summarizer` и `office_files` уехали в
платформу `mcp-platform`).

> **Агент:** Aura (🐈) · **Модель:** OpenAI-compatible · **ОС:** Windows · **Язык:** RU/EN

## 🚀 Быстрый старт

```bash
python -m venv .venv && .venv\Scripts\activate
pip install nanobot-ai && pip install -r requirements.txt
copy .secrets.env.example .secrets.env   # cp на Linux
# Отредактируйте .secrets.env: DB_PASSWORD=... и # providers: llm / api_key=...
python tools/migrate.py --apply         # применить миграции схемы
# --profile обязателен для gateway (prod | test), иначе ConfigurationError + exit 2:
python gateway.py --profile=prod        # AgentLoop + канал PostgreSQL
# или (CLI — фиксированный профиль test, флаг --profile не принимается):
python cli_agent.py -P -s dev           # REPL в patched-режиме (PostgreSQL)
```

Минимальный набор таблиц (если нет `migrate.py`):

```bash
psql -d nanobot -f sql/session/create_public_agent_session_meta.sql
psql -d nanobot -f sql/session/create_public_agent_session_messages.sql
psql -d nanobot -f sql/channels/create_public_agent_conversation_messages.sql
```

Полный список DDL — в [`sql/README.md`](sql/README.md).

## 🛠 Команды

```bash
python gateway.py --profile=prod                              # долгоживущий сервер
python cli_agent.py                                           # REPL vanilla (JSONL), профиль test
python cli_agent.py -P -s my-session                          # REPL patched (PGSessionManager + хуки)
python tools/migrate.py --apply                               # миграции схемы
```

> **Индексы и снимок обслуживает платформа, не агент.** Сборка векторов
> (`tools/build_vectors.py`) и диагностика (`tools/check_indexes.py`) удалены
> из агента 2026-10-01 вместе с кластером снимка. Операторские команды —
> из каталога `mcp-platform/`:
>
> ```bash
> cd mcp-platform
> python -m servers.enterprise.build_index --dry-run           # что и сколько пересчитать
> python -m servers.enterprise.build_index --index audits_index --full-rebuild
> python -m servers.enterprise.server --health                 # состояние capability
> ```
>
> Сборка — **писатель PostgreSQL**, снимок она не открывает: после сборки
> снимок надо перезагрузить, иначе поиск продолжит читать прежние вектора.
> Состояние индексов видно операцией `index_stats` capability `vectors`.

> **Навык `audit_analyzer`** не имеет собственного CLI и не ходит в данные
> напрямую: модель вызывает операции capability `audit` платформы
> enterprise-mcp как `mcp_enterprise_*` — процесс поднимает штатный
> MCP-клиент нанобота, объявленный в `config.json → tools.mcpServers`.
> Операции: `mcp_enterprise_list_scripts` (каталог готовых скриптов),
> `mcp_enterprise_run_script`, `mcp_enterprise_generate_sql` (NL→SQL, запрос
> строит и проверяет платформа), `mcp_enterprise_vector_search`.
> Модель не пишет SQL: белый список таблиц и потолок строк проверяются до
> выполнения. Личность вызова подставляет `McpIdentityHook` — самой её
> передавать не нужно. Внешний контракт — `SKILL.md` и общий контракт вызовов
> в `workspace/skills/enterprise_mcp/SKILL.md`.

Подробности по каждой команде — в [docs/INTERNAL_API.md](docs/INTERNAL_API.md) и
[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

## 🏗 Архитектура

```mermaid
flowchart LR
    WEB["gateway (HTTP API)"] --> ORCH["Оркестрация<br/>ApplicationContext"]
    TERM["cli_agent (терминал)"] --> ORCH
    UI["внешний веб-клиент"] --> ORCH
    ORCH --> AGENT["Агент<br/>рассуждение + инструменты"]
    ORCH --> BUS["Шина сообщений"]
    AGENT --> CACHE[("Снимок DuckDB<br/>(владеет платформа)")]
    AGENT --> TOOLS["Инструменты<br/>SQL / векторный поиск"]
    CACHE --> VEC["Индексы FAISS<br/>(владеет платформа)"]
    TOOLS --> DB[("База данных (PostgreSQL)")]
    VEC --> EMB["Эмбеддинги<br/>(capability llm платформы)"]
    classDef entry fill:#d1ecf1,stroke:#0c5460,stroke-width:2px
    classDef core fill:#fff3cd,stroke:#d39e00,stroke-width:2px
    classDef infra fill:#d4edda,stroke:#1b7a3d,stroke-width:2px
    class WEB,TERM,UI entry
    class ORCH,AGENT,BUS,TOOLS core
    class CACHE,VEC,DB,EMB infra
```

**Поток:** `config.json` → `config.py: SETTINGS` → `ApplicationContext.create()` →
`MessageBus` → `AgentLoop` → `gateway.py`/`cli_agent.py` запускают каналы + lifecycle.
Данные, снимок и индексы агент не держит: он ходит в них операциями capability
`enterprise-mcp`.
Полная таблица связей — в [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## 📁 Структура проекта

```
nanobot/
├── README.md  CHANGELOG.md  AGENTS.md
├── config.json  config.py                # конфиг + загрузчик (project.json удалён)
├── gateway.py  cli_agent.py                  # точки входа
├── lib/                          # сервисный слой: core, services, cli, hooks,
│                                 #   lifecycle, channels, session, utils, commands
├── workspace/                    # runtime, hooks-плагины, skills, memory
├── tests/  tools/  sql/  docs/  requirements.txt
```

Подробное дерево — в [docs/ARCHITECTURE.md → Структура проекта](docs/ARCHITECTURE.md#структура-проекта).
Навигация по `docs/` — в [docs/README.md](docs/README.md).

## 🗃 База данных

DDL в `sql/<domain>/create_<schema>_<table>.sql` (один файл = одна таблица).
Миграции — `python tools/migrate.py --apply`. Слои:

- **Сессии:** `public.agent_session_meta`, `public.agent_session_messages`
- **Канал:** `public.agent_conversation_messages`
- **Журнал:** `public.agent_gateway_logs`, `public.agent_question_runs` (UUID + JSONB)
- **Домен audit_analyzer:** `oarb.audits/violations/audit_reports/report_items` (REFERENCE)
- **Векторы:** `oarb.audit_vectors` (эмбеддинги; FAISS и DuckDB-снапшот обслуживает платформа, состав индексов объявляет `mcp-platform/platform.json → vectors.indexes`); `public.agent_vector_index_config` и `public.agent_vector_index_store` — legacy SQL-артефакты, кодом не читаются
- **Predefined scripts:** `public.agent_predefined_scripts`

> Имена таблиц/индексов выше — значения текущей инсталляции (REFERENCE). Они
> настраиваются в `config.json` (`channels.postgres.*`, `logging.db.*`) и в
> `mcp-platform/platform.json` (`audit.tables`, `vectors.indexes`) — и в других
> развёртываниях могут отличаться. Файла `project.json` больше нет: его секции
> переехали в `config.json` (`gateway.agent.<name>`) либо в конфиг платформы.

Состав таблиц снимка и векторных индексов объявляет платформа:
`mcp-platform/platform.json` (`audit.tables`, `vectors.indexes`). Прежний
[docs/table-registry.md](docs/table-registry.md) описывает снятый реестр
ресурсов и как инструкция не годится.

## 🧪 Тестирование

**3603 теста собираются** (`python -m pytest tests/ -q --collect-only`); интеграционные
падают без живого PostgreSQL/LLM. Зелёный прогон требует доступного PostgreSQL
(test-профиль) и терминала с UTF-8.

```bash
pytest tests/ -q
pytest tests/ --cov=lib --cov-report=term-missing
```

Группы: `test_application_context.py` + `test_*_factory.py` · `test_runtime_patcher.py`
+ `test_utils_db.py` · `test_*_service.py` (db_logging, audit, transcription) ·
`test_pg_session_manager.py` + `test_*_channel.py` · `test_hooks_*.py` +
`test_recent_files_hook.py` + `test_office_files.py` ·
`test_gateway*.py` + `test_cli_agent.py`.

## ⏰ Heartbeat и cron

`nanobot gateway` запускает встроенный heartbeat-cron, который периодически проверяет
`HEARTBEAT.md` (`gateway.heartbeat.enabled=true`, `intervalS: 1800`). Не дублируйте его.

- Периодическая проверка → правьте `HEARTBEAT.md`.
- Одноразовое напоминание → встроенный `cron` tool opencode.
- Политика storage и cron для агента — в [`workspace/AGENTS.md`](workspace/AGENTS.md).

> [!WARNING]
> Не пишите напоминания только в `MEMORY.md` — это не вызывает уведомлений.

## 📚 Документация

| Документ | Назначение |
|---|---|
| **[docs/README.md](docs/README.md)** | Навигационный индекс каталога документации (разделы, нормативная архитектура, конвенции) |
| **[docs/TARGET_ARCHITECTURE.md](docs/TARGET_ARCHITECTURE.md)** | Нормативный архитектурный контракт (принципы, invariant'ы, anti-patterns, decision-чеклист) |
| **[CHANGELOG.md](CHANGELOG.md)** | История релизов (Keep a Changelog / SemVer) |
| **[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)** | Диагностический runbook |
| **[docs/MIGRATION.md](docs/MIGRATION.md)** | Сводка изменений между релизами + breaking changes |
| **[docs/table-registry.md](docs/table-registry.md)** | ~~Реестр таблиц~~ — снят, надгробие с картой «куда что уехало» |
| **[docs/skill-tool-architecture.md](docs/skill-tool-architecture.md)** | Контракт Skill ↔ Tool |
| **[docs/architecture/](docs/architecture/)** | Инвентаризация зависимостей и monkey-patch'ей |
| **[lib/channels/README.md](lib/channels/README.md)** | Каналы (Postgres/Redis): DDL, поток, конфиг |
| **[lib/session/README.md](lib/session/README.md)** | `PGSessionManager`: схема, graceful degradation |
| **workspace/skills/*/SKILL.md** | Документация навыков |
| **workspace/AGENTS.md** | Инструкции для агента |

## 🆕 Что нового в [Unreleased]

**MAJOR.** Session hot-path переведён на upstream `SessionManager` (JSONL) —
единственный source of truth; PostgreSQL остаётся cold-storage mirror
(`SessionColdSyncService`, per-transaction advisory lock, leader-election).
`PGSessionManager` в hot path только делегирует `super()`; прямых SQL-операций
в `agent_session_meta` / `agent_session_messages` нет. Исторические PG-сессии
требуют разовой миграции в JSONL **до** деплоя (см. `docs/architecture/storage-layers.md`,
`docs/MIGRATION.md` § «Storage hybridization»).

**MAJOR.** Persisted FAISS-кеш удалён: единственный источник векторных данных —
DuckDB-снапшот `gateway.vector.index.storage_table`, FAISS собирается в памяти
при старте gateway. Таблица-сигнатура и настройка `signature_table` удалены
(миграция `V003__drop_vector_index_store.sql`), `--list-indexes` и
`tools/check_indexes.py` читают runtime из того же снапшота.

**MAJOR.** Профиль конфигурации задаётся только CLI-флагом `--profile`
(whitelist: `prod` / `test`); env-передача профиля больше не работает,
`gateway.py` / `cli_agent.py` без флага падают с
`ConfigurationError` и `exit 2` (см. `docs/PROFILES.md`).

**SECURITY.** `history_search(session_scope="all")` изолирован по `user_id`
(новая колонка `agent_gateway_logs.user_id`, миграция `V004`): больше нет
cross-user выдачи; без identity-store возвращается `missing_user_identity` /
`missing_session_identity` без обращения к БД.

**Changed.** Единый logging pipeline: `workspace/utils/event_log.py` удалён,
`DbLoggingService` — единственный writer в `agent_gateway_logs` /
`agent_question_runs`; `/compact` живёт в upstream-обработчике
`nanobot.command.builtin.cmd_compact`, а факт сжатия пишет
`ContextCompactionService.notify_session_compacted()` (через
`CompactionEventSubscriber` по событию `ContextCompactionEvent`).

Полный changelog — в [CHANGELOG.md → Unreleased](CHANGELOG.md#unreleased).

## 🆕 Что нового в v2.5.2

**PATCH поверх v2.5.1, 2026-09-14.** Две группы доработок:

**NFS / DuckDB cache.** Раньше gateway, развёрнутый на NFS-шаре, цикл
sync-а падал с `IO Error: Could not set lock on file cache.duckdb.tmp:
Conflicting lock is held in PID 0` (DuckDB `ATTACH` берёт эксклюзивный
`flock`, который NFS `lockd` не отдаёт). Теперь:
- **единый механизм** `resolve_cache_path()` — вызывается и из
  gateway, и из CLI/skill/vector_index_service; путь записи и путь
  чтения **всегда совпадают** (`b1d2e21`, fix от расхождения после
  коммита `85cad2a`);
- safe default — `~/.cache/nanobot/duckdb/cache.duckdb` (POSIX `fcntl`
  работает там штатно), без escape hatch и без совместимости с NFS
  (`85cad2a`);
- единственная опция override — `gateway.cache.local_path` (`c522b55`);
  legacy `<workspace>/data_store/duckdb/` больше не выбирается
  через `gateway.cache.use_workspace_path` — опция удалена;
- startup WARNING при попадании снимка на NFS (`/proc/mounts` check);
- defensive publish-слой: уникальный `.tmp.<pid>.<ms>.tmp`, retry с
  backoff на `ATTACH`, понятный `sync_publish_failed` вместо
  `except OSError: pass` (`605660b`, `652b09d`).

**Observability sync-путей.** Единый конвейер `DbLoggingService.try_log_event`
вместо ad-hoc `logger.warning` (`a1811c5`); ошибки
`preload` векторов и `channel` lease-loop теперь попадают в долговечный
`agent_gateway_logs` (`9fb88c4`, `48575e9`); `PG→DuckDB` sync-цикл
(`initial_load` / `poll_cycle` / `claim` / `release` / `reconnect`)
полностью пишется в `agent_gateway_logs` (`f58c957`, `d4558f9`).

**Tests:** добавлены `TestResolvePublishPath` (6 кейсов),
`TestSingleMechanism` (1 кейс — инвариантна согласованности gateway ↔
CLI/skill) и `TestWarnIfPublishPathOnNfs` (2 кейса); все ранее
падавшие тесты (включая `preload_service::test_error_returns_none`)
зелёные.

**Audit-analyzer three-mode contract (`a396c27`).** `audit_analyzer`
свёрнут в три равноправных режима — `predefined`, `vector`,
`generated_sql` — **без fallback между ними**. Удалён
`scripts/column_hints.py` и прежний registry: схема передаётся в LLM
через `CacheProvider.get_schema()` +
`lib.utils.sql_safety.format_schema`, few-shot — через
`predefined.db_loader.load_all`. Если выбранный режим неприменим,
агент получает явный `RuntimeError` с диагностикой, а не молчаливый
переход на соседний режим.

**Vector discovery: declared vs runtime.**
`audit_analyzer/scripts/cli.py::_list_indexes()` читает фактическое
состояние индексов из DuckDB-снапшота таблицы-хранилища
(`gateway.vector.index.storage_table`), а не декларативный JSON. Для сверки
с декларацией (`config.json::gateway.vector.index.indexes.*`) добавлен
`tools/check_indexes.py`: MISSING / ORPHAN / STALE / INVALID-signature,
exit 0/1/2, `--json` для CI. См. `docs/VECTOR_INDEXES.md`.

**Preload health summary на старте gateway (`78a57f4`).** После
`preload_vector_indexes()` gateway печатает в **stderr** многострочный
summary (`declared/loaded/missing/orphan/stale` + счётчики vectors) и
пишет одно событие `vector_index_preload_health` в `agent_gateway_logs`
через `DbLoggingService.try_log_event`: `level="WARN"` при divergence, иначе `INFO`.
Конструктор `PreloadService(settings, db_logging_service)` —
сервис логирования пробрасывается явно.

**Tests (полный набор):** добавлены `TestResolvePublishPath` (6),
`TestSingleMechanism` (1 — инвариантна gateway ↔ CLI/skill),
`TestWarnIfPublishPathOnNfs` (2), `test_check_indexes` (17 — declared vs
runtime), `test_preload_service` (+18 health summary, всего 22),
`test_audit_analyzer_mode_selection` (переписан под three-mode),
`test_audit_analyzer_generated_sql` (обновлён под `MAX_ATTEMPTS`).

Полный changelog — в [CHANGELOG.md → 2.5.2](CHANGELOG.md#252--2026-09-14).

## 🆕 Что нового в v2.5.1

**PATCH поверх v2.5.0, 2026-09-13.** Регрессии и доработки после MINOR-релиза — закрытие
lifecycle-deadlock `postgres_channel` при `stream_end` с пустым delta (`71cfcde`),
удаление agent-tools `duckdb_query` и `vector_search` (`12bf182`), перенос конфига vector-индексов из PG-реестра
`public.agent_vector_index_config` в `project.json::gateway.vector.index.indexes.*`
+ хардкод эмбеддинга (`bf59b5a`), DB-first `scripts/predefined` в `audit_analyzer`
+ удаление `tools/generate_predefined_scripts_sql.py` (`79e0e63`),
`tools/build_vectors.py --validate-only` + ETA прогресса (`8b70383`), стабилизация
порядка таблиц в `lib/utils/duckdb_query.build_schema` (`a8e03e8`), перенос тестов
`audit_analyzer` в `workspace/skills/audit_analyzer/tests/` (`10771cc`),
синхронизация архитектурной документации и README «Что нового».

Изменения конфигурации: `config.json` — провайдер LLM `qwen3.6-35b-a3b` через
`https://api.neuraldeep.ru/v1/`, `contextWindowTokens: 40000` (см. `e06b2b0`).

Полный changelog — в [CHANGELOG.md → 2.5.1](CHANGELOG.md#251--2026-09-13).

## 🆕 Что нового в v2.5.0

**MINOR поверх v2.4.0, 2026-09-11.** Рефакторинг `legal_summarizer` (layered package,
document-level cache, brief как ровно один Chunk, structural
packing, вопрос-режим через document cache, e2e 3-mode CLI), переработка
конфигурационного контракта skills ↔ runtime infrastructure (`TableRegistry.register_infra`,
`gateway.vector.{embedding,index}.*`, `EmbeddingSettings`, hard validation legacy-ключей),
generic infrastructure tools (`duckdb_query`, `vector_search`, `nl_sql_generate`,
`column_descriptions`, `history_search`, `compact_context`), SQL AST-security-guard,
миграции схемы, сервисы времени жизни (`ContextCompactionService`,
`RuntimeHealth`/`RuntimeReadiness`, `ConsolidatorLocale`), перенос утилит
`lib/utils/*` (media/jsonb/outbound) → `workspace/utils/*`, vector-storage как
инфраструктурный ресурс, ремедиация compatibility-shim долга, history_search FTS-baseline.
Подробный эпиграф с breaking changes — в начале блока v2.5.0.

Полный changelog — в [CHANGELOG.md → 2.5.0](CHANGELOG.md#250--2026-09-11).
Сводка breaking changes — в [docs/MIGRATION.md](docs/MIGRATION.md).

## 🛡 Зависимости и лицензия

Рантайм агента: `nanobot-ai`, `loguru`, `psycopg2-binary`, `httpx`, `PyYAML`,
`mcp` (клиент stdio-сессии к `enterprise-mcp`) и пакеты разбора офисных
документов (`python-docx`, `openpyxl`, `xlrd`, `pypdf`, `pdfplumber`,
`python-pptx`, `Pillow`, `chardet`) — точные версии в `requirements.txt`.
Пакеты данных и индексов (`duckdb`, `faiss-cpu`, `numpy`, `pyarrow`) в
требования агента **не входят**: снимком, FAISS-индексами и эмбеддингами владеет
платформа, они объявлены в её манифестах (`mcp-platform/requirements.txt`,
`mcp-platform/pyproject.toml`).

> **Требует решения владельца:** `requirements.txt` агента всё ещё объявляет
> `redis==8.0.0`, хотя Redis-канал снят и ни один модуль агента пакет не
> импортирует (сторож — `tests/test_channel_factory.py::TestRedisIsGone`).
> Файл `requirements.txt` в эту правку не входил.

**Лицензия:** MIT.
