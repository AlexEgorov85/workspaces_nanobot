# Changelog

Все значимые изменения в проекте **nanobot — Personal AI Agent** будут задокументированы в этом файле.

Формат основан на [Keep a Changelog](https://keepachangelog.com/ru/1.0.0/), проект придерживается [Semantic Versioning](https://semver.org/lang/ru/).

Релизные ветки именуются как `release/vX.Y`, теги патч-релизов — `vX.Y.Z`.

## [Unreleased]

### Added
- Механизм подстановки секретов `${VAR}` из окружения при чтении конфигурации.
- `project.json` — JSONC-файл с комментариями (дополнение к `config.json`), где задаются проектные секции: `channels.*` (PostgreSQL/Redis), `skills.*`, `cli`, `benchmark`, `streamlit`, `gateway`.
- Шаблон `.secrets.env.example` со списком переменных окружения, ожидаемых при старте.

### Changed
- Конфигурация мигрирована из `.env` в `project.json` + `config.json`, секреты вынесены в `.secrets.env`; порядок мержа: `project.json → config.json → .secrets.env` (поздний перекрывает ранний). Оставлен защитный fallback: если `.env` появится, он прочитается первым.
- Удалены навыки `data-analyzer` и `html_presentation_generator`: вычищены их зависимости из `requirements.txt`, блоки из `project.json`/`config.json`.

### Removed
- Артефакты аудита упомянутых навыков, конфигурация `data-analyzer` и `html_presentation_generator`.

### Fixed
- README приведены в соответствие реальности: убраны упоминания удалённых навыков, исправлена заметка о GP-схеме, счётчик seed-записей и ссылки на бенчмарки.
- Прогресс-события рантайма больше не затирают media сообщений-инструментов в `PostgresChannel.send`.

---

## [1.5.0] — 2026-07-22

### Added
- Векторные поисковые индексы перенесены из файлов в PostgreSQL/Greenplum:
  - `oarb.audit_vectors` — сырые эмбеддинги `REAL[]` с метаданными (строит `build_vectors.py`);
  - `oarb.vector_index_store` — сериализованный FAISS-индекс `BYTEA` (ищет `vector_mode.py`);
  - `oarb.vector_index_config` — конфигурация индексов (таблицы/колонки), чанкование, автосинхронизация.
  - Параметры `--top-k` / `--threshold` задаются аргументами CLI (`--index-name`), а не конфигом.
- DuckDB-кеш для `audit_analyzer` с фоновым обновлением (`in_memory_enabled`, `cache/audit_cache.duckdb`); `init`-режим загрузки кеша из PostgreSQL.
- Передача файлов между агентами через БД как base64 `data URL` вместо файловых ссылок.
- 75 unit-тестов по runner/gateway/streamlit (`tests/`), исправлены найденные баги.
- `requirements.txt` со всеми зависимостями.
- Инъекция провайдерных API-ключей из `.secrets.env` в конфиг на старте (совместимость с nanobot 0.2.2).
- Инструкция разработчика по векторным индексам (docs).

### Changed
- Конфигурация мигрирована из кода в `.env` (+ исправлена коллизия имени `scripts/config.py`); из `.env` в `.secrets.env` вынесены API-ключи.
- Реорганизована структура: модули `lib/channels` и `lib/session`, добавлены README для них; итоговое расположение `lib/session/sql/`, `lib/channels/sql/`, `scripts/`, `logs/`.
- README исправлен (неточности), добавлены README для `lib/channels` и `lib/session`.

### Fixed
- Совместимость с Greenplum 6.25: ручной UPSERT вместо `ON CONFLICT` в `PGSessionManager`.
- Хранение файлов сессии в `data_store/cache/sessions/{session_key}`.
- Убран `ThreadedConnectionPool` — вызывал double free на Windows с asyncio.
- DSN берётся из `gateway_settings.py`; retry LLM при 429; вывод реальной ошибки БД в fallback-сообщениях.
- gssencmode=disable для GP 6.25 / PG 9.4 (и URI, и key=value DSN; через kwargs `connect()`, а не модификацией строки).
- Отдельные счётчики retry в `_connect()`: 50 попыток для «too many connections», 15 для остальных.
- Исправлен индекс `parents` (3 вместо 2 — работает на всех версиях Python); при retry удаляется assistant-placeholder вместо установки `status='failed'`.

### Security
- API-ключи вынесены из кода и конфигурации в `.secrets.env` (файл в `.gitignore`).

---

## [1.4.0] — 2026-06-16

### Added
- Русские docstrings во всех `.py`.
- Хук `_run_sync` fallback для случая, когда нет event loop (Streamlit, CLI) — использует временный пул.

### Changed
- Стек БД переведён с `asyncpg` → `psycopg2`, API переведён с async на sync.
- Убран общий пул: каждый запрос создаёт и закрывает собственное подключение; удалён модуль `db_api`; импорты переведены на функции модульного уровня.
- `DB_RETRYABLE_ERRORS` экспортирован из `db.py` (убрана дубликация в `pg_session_manager`).
- Навык `db_analyzer` переименован в `audit_analyzer`; `config.json` грузится из папки `gateway.py`.

### Fixed
- Совместимость с PG 9.4 и GP 6.25: `DISTRIBUTED BY`, pgcrypto, schema-introspection; удалены все DDL (`ensure_tables`) — таблицы должны существовать заранее.
- Раздельные счётчики retry: `TooManyConnectionsError` — 50×, остальные ошибки — 10×.
- Таймаут 30с на `pool.acquire()` (канал больше не зависает); предотвращена утечка соединения в `_get_conn` при ошибке `_init_jsonb`.
- `ON CONFLICT` → `UPDATE+INSERT` для GP6; `IF NOT EXISTS` → проверки через `pg_catalog`; убраны касты `::jsonb` из DML; синтаксис `session ON CONFLICT` исправлен, `msg_timestamp` дедуплицирован.
- `pool_max_conn` снижен до 1 против «too many connections» на Greenplum.
- Streamlit ожидает ответ агента без `st.rerun` (обход лимита `maxReruns`).

### Removed
- Пул соединений (включая шаринг одного пула между async/sync через `run_coroutine_threadsafe`) — перевыделение ресурсов на каждый запрос.
- Все DDL и `::jsonb`-касты.

---

## [1.3.0] — 2026-06-10

### Added
- Единый слой БД `SharedDB` (один psycopg2-коннекшн с блокировкой) + конфигурируемый асинхронный пул (`min_size`/`max_size`); sync-методы используют отдельные подключения.
- HTTP **DB API Server** — доступ к PostgreSQL из любых процессов; автоочистка БД; поддержка DSN для subprocess-процессов.
- **Self-review** система: `ReviewAgentLoop`, `RepeatGuardHook`, навык response-verification; метаданные `_review` (quality, attempts, issues, tool_repeat_stopped).
  - Ревьюер разбит на 8 независимых проверок с русскими промптами; fast-path по приветствию; фиксы multi-turn контекста.
  - **Fresh Data Rule** — агент обязан делать свежие tool-вызовы, а не переиспользовать историю.
  - Check 1 (Tool Usage) — детект обхода инструментов и ответа «из памяти»; Check 3 (Error Honesty) — детект «нет данных» вместо реальных ошибок инструментов.
  - `on max_iterations` — подстановка ответа «could not get data» вместо галлюцинированного контента.
- `ToolAuditHook` — запись всех tool-вызовов (статус/ошибки/аргументы) в `metadata._tool_audit`; `ToolParamsHook` влит в него.
- **Benchmark-фреймворк**: русские YAML-элементы, хук-фиксы, поддержка `qwen3-coder`; `fix bechmark` на точке реза ветки.
- Нативный инструмент `db_analyzer` для gateway (с валидацией параметров predefined-скриптов и защитой от необработанных исключений; позже откатан в ветку).
- Streamlit запускается как subprocess вместе со всеми каналами; тонкий клиент через `conversation_messages` + единый `AgentLoop` в gateway.
- UI: file-based история по умолчанию (`--storage` для DB), сворачиваемое reasoning, отображение tool events, загрузка хуков.
- Redis-канал `redis_channel.py`; блок `session_manager` в конфиге (читается из сырого JSON в обход валидации Pydantic) + совместимость с PG 9.4.20.

### Changed
- `psycopg2`/`asyncpg` → единый `asyncpg SharedDB` для каналов, сессий, навыка и CLI (`:param` → `%s`).
- Единый DSN в `gateway_settings.py` (убраны дубликаты из навыка); унифицирована конфигурация gateway.
- `conversation_id` → `chat_id` для блокировок по чатам; per-chat locking.
- Убран `INDEX.json` — каждый результат сохраняется отдельным файлом; ограничение `MAX_OUTPUT` у ExecTool до 10M; `processing_timeout` 600 → 120 с.
- `_tool_events` → `_tool_audit` без дублирования; слияние `reasoning` и `_reasoning` в ключ `metadata.reasoning`.

### Fixed
- Двойное кодирование JSONB в postgres_channel (хелпер `_decode_jsonb`, backward compat для старых записей); JSONB-декодер в SharedDB (asyncpg возвращает `dict`).
- Путь workspace в data-analyzer и захардкоженный путь в e2e-тесте.
- Обработка переполнения диска в `_normalize_with_persist`; gateway обёрнут в автоперезапуск при краше; limit роста INDEX.json (preview убран).
- Транзакционный `_mark_failed`; гонка UPSERT в `PGSessionManager` (`ON CONFLICT`); соответствие `seed_messages.sql` DDL; исправлена двойная JSONB-кодировка в `pg_agent_worker`.
- `%s`-плейсхолдеры для asyncpg; `to` (/quote) очистка в навыке; не переконфигурировать SharedDB.
- postgres channel: поллинг, `allow_from`, `timezone.UTC`, создание каталогов.

### Removed
- WebSocket-канал (конфиг + примеры `gateway_settings`), `webui-dist/` (SPA) и код `_patch_webui_dist`, `patches/` (reviewer, review_agent_loop) — мёртвый код из benchmark-dev.
- Мёртвые файлы: `temp_loop.py`, `create_table.sql`, `test_file_*.py`, регенерированные артефакты workspace, `_tmp_checks.py`, `fibonacci.py`; `connection_string` из docstring.
- `ResponseReviewHook`, `INDEX.json`, `DbAnalyzerTool` (revert).

---

## [1.2.0] — 2026-05-29

### Added
- **Streamlit-чат** с live-отображением рассуждений агента (`streamlit_app.py`).
- CLI: стриминг reasoning и ответа в реальном времени (вывод tool-выводов скрыт).

### Changed
- PostgresChannel переведён на **единотабличную** архитектуру (`conversation_messages`) с батчингом reasoning и контролем конкурентности (макс. параллельных сообщений).
- Вывод CLI переписан: typewriter-эффект, хуки, константы конфигурации.

---

## [1.1.0] — 2026-05-27

### Changed
- Модель конфигурации обновлена до `gpt-oss:20b-cloud`; исправлены стрелочные символы в presentation-инструменте.
- `db_analyzer`: класс `Database`, кеш схемы, фильтр таблиц, прямой DSN; улучшенный формат схемы для LLM (`NOT NULL`, `varchar(N)`, `schema.table`).
- `cli_agent`: добавлены константы `_CONFIG_PATH` и `_WORKSPACE_DIR`; скан `workspace/skills/` на предмет `tool.py`.

### Fixed
- Отображение рассуждений в `cli_agent` — по-дельтам, без накопления, с Rich markup; устранено дублирование ответа; откат пере-скана навыков (два дублирующих коммита).
- Показ результатов tool-вызовов (`show_tool_results`).
- Исправлен остаток merge-конфликта в `config.json`; трекинг `config.json` (секреты санитизированы).

---

## [1.0.0] — 2026-05-27

### Added
- Навыки `db_analyzer` и `html_presentation_generator` (полный код, E2E-тесты, исправленный `.gitignore`); разрешение `vector_source`, JSON-safe вывод.
- CLI-режим vector: параметры `--top-k` и `--threshold` (примеры для Linux в SKILL.md).

### Changed
- CLI: `--params` поддерживает формат `key=value` (фикс кавычек для Windows); примеры в SKILL.md.


---

## [0.9.0] — 2026-05-25

### Added
- Начальная версия проекта: nanobot-шлюз с `PostgresChannel`, инструментами и конфигурацией workspace.