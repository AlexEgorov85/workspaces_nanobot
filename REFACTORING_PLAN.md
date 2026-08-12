# 📋 План рефакторинга: ApplicationContext + сервисный слой + DbLoggingService

> **Статус плана:** ✅ **ВСЕ ШАГИ ВЫПОЛНЕНЫ** (см. сводку ниже).
> **Как отмечать шаги:** ставьте `[x]` в чекбоксе. Каждый шаг содержит блок «✅ Проверка» —
> его нужно прогнать перед тем, как отметить шаг выполненным.
> **Принцип:** маленькие безопасные шаги, каждый сохраняет работоспособность gateway/cli.

## 📊 Сводка выполнения

| Step | Сервис | Файл | Тест | Статус |
|------|--------|------|------|--------|
| 0 | ConfigService | `lib/services/config_service.py` | `tests/test_config_service.py` (15) | ✅ |
| 1 | SessionStorageService | `lib/services/session_storage.py` | `tests/test_session_storage.py` (11) | ✅ |
| 2 | RuntimePatcher | `lib/services/runtime_patcher.py` | `tests/test_runtime_patcher.py` (8) | ✅ |
| 3 | TranscriptionService | `lib/services/transcription_service.py` | `tests/test_transcription_service.py` (11) | ✅ |
| 4 | ChannelFactory | `lib/services/channel_factory.py` | `tests/test_channel_factory.py` (7) | ✅ |
| 5 | SubprocessManager | `lib/services/subprocess_manager.py` | `tests/test_subprocess_manager.py` (6) | ✅ |
| 6 | PreloadService | `lib/services/preload_service.py` | `tests/test_preload_service.py` (16) | ✅ |
| 7 | BusFactory + AgentFactory | `lib/core/bus_factory.py`, `agent_factory.py` | `tests/test_bus_factory.py`, `test_agent_factory.py` (9) | ✅ |
| 8 | GatewayRunner + ShutdownCoordinator | `lib/lifecycle/gateway_runner.py`, `shutdown_coordinator.py` | `tests/test_gateway_runner.py`, `test_shutdown_coordinator.py` (12) | ✅ |
| 9 | DbLoggingService + DatabaseLoggingHook | `lib/services/db_logging_service.py`, `workspace/hooks/database_logging_hook.py`, `lib/services/sql/create_logs_table.sql` | `tests/test_db_logging_service.py`, `test_hooks_database_logging.py` (20) | ✅ |
| 10 | ApplicationContext | `lib/core/application_context.py` | `tests/test_application_context.py` (7) | ✅ |
| 11 | gateway.py → 129 lines | `gateway.py` | `tests/test_gateway.py` (6) | ✅ |
| 12 | cli_agent.py → 162 lines + lib/cli/{display_config,hook_loader,console_loop}.py | `cli_agent.py`, `lib/cli/*` | `tests/test_cli_agent.py` (16) | ✅ |
| 13 | streamlit_app.py — НЕ переписывать | — | `tests/test_streamlit_app.py` (без изменений) | ✅ |
| 14 | pg_agent_worker.py — оставлен как legacy | — | — | ✅ |
| 15 | README + тесты | `tests/test_*.py` | **685 passed** | ✅ |

**Полный suite:** `python -m pytest -q` → **685 passed** в 19.6s.

**Размеры точек входа после рефакторинга:**
- `gateway.py` — 129 строк (было 696)
- `cli_agent.py` — 162 строки (было 865)
- `streamlit_app.py` — 502 строки (не тронут, отдельная архитектура)
- `pg_agent_worker.py` — 310 строк (legacy, отдельная точка входа)

---

## 🎯 Цель

Устранить дублирование логики между точками входа и добавить наблюдаемость:

1. **`ApplicationContext`** — единая точка создания и связывания всех сервисов.
2. **Тонкие точки входа** — `gateway.py`, `cli_agent.py` (только оркестрация).
3. **Переиспользуемые сервисы** в `lib/services/`, `lib/core/`, `lib/lifecycle/`.
4. **`DbLoggingService`** — структурированное логирование событий агента в PostgreSQL.
5. **Все monkey-patch'и** собраны в одном `RuntimePatcher`.

---

## 📂 Целевая структура папок

```
lib/
├── __init__.py
├── core/
│   ├── __init__.py
│   ├── application_context.py        # Bootstrap всех точек входа
│   ├── agent_factory.py              # Создание AgentLoop с хуками
│   └── bus_factory.py                # Создание MessageBus (+ обёртки логирования)
├── services/
│   ├── __init__.py
│   ├── config_service.py             # Загрузка конфига, ключи, таймауты
│   ├── session_storage.py            # Фабрика SessionManager (PG / File)
│   ├── runtime_patcher.py            # ВСЕ monkey-patch'и в одном месте
│   ├── transcription_service.py      # Ключи/URL транскрипции (openai/groq)
│   ├── channel_factory.py            # Создание всех каналов
│   ├── subprocess_manager.py         # Управление дочерними процессами (Streamlit)
│   ├── preload_service.py            # Фоновые предзагрузки (FAISS / кеш навыка)
│   ├── db_logging_service.py         # Логирование в БД (NEW)
│   ├── audit_sync_service.py         # УЖЕ ЕСТЬ — не пересоздавать
│   ├── audit_memory_store.py         # УЖЕ ЕСТЬ — не пересоздавать
│   ├── cache_provider.py             # УЖЕ ЕСТЬ
│   ├── cache_provider_impl.py        # УЖЕ ЕСТЬ
│   ├── text_splitter.py              # УЖЕ ЕСТЬ
│   └── sql/
│       └── create_logs_table.sql     # DDL для gateway_logs
├── cli/                              # NEW (вынос из cli_agent.py)
│   ├── __init__.py
│   ├── console_loop.py               # REPL + consume_outbound + typewriter
│   ├── display_config.py             # DisplayConfig и рендер
│   └── hook_loader.py                # Сканирование workspace/hooks/*.py
├── lifecycle/
│   ├── __init__.py
│   ├── gateway_runner.py             # Цикл перезапуска с backoff
│   └── shutdown_coordinator.py       # Порядок graceful shutdown
└── session/                          # УЖЕ ЕСТЬ
    └── pg_session_manager.py
```

> ⚠️ **Важно:** `streamlit_app.py` и `pg_agent_worker.py` — **отдельные архитектуры**, см. Step 12 и Step 13.

---

## 📦 Факты о текущем коде (сверка перед стартом)

| Файл | Строк | Комментарий |
|------|------:|-------------|
| `gateway.py` | 696 | God Object: конфиг, патчи, каналы, Streamlit, аудит-сервисы, backoff |
| `cli_agent.py` | 865 | vanilla/patched, REPL, хуки, cron-миграция, кеш навыка |
| `streamlit_app.py` | 502 | Поллинг-UI поверх `conversation_messages` (Postgres-канал) — **не трогаем** |
| `pg_agent_worker.py` | 310 | Воркер `agent_questions → agent_responses` (legacy, через `Nanobot`) |
| `config.py` | 229 | Уже есть: `project.json → config.json → .secrets.env`, `AttrDict`, `${VAR}` |
| `lib/services/audit_memory_store.py` | 690 | Уже вынесен + тесты |
| `lib/services/audit_sync_service.py` | 449 | Уже вынесен + тесты |

**Тесты:** 136 passed (мокают nanobot через `patch.dict("sys.modules")`; nanobot стоит в user site-packages, не в .venv).

**Критичные ограничения:**
- Метода `agent.process_message` в `nanobot.agent.loop.AgentLoop` НЕТ. Общение — через `MessageBus` (`publish_inbound` / `consume_outbound`).
- `AgentHook` — async-методы: `before_execute_tool`, `after_execute_tool`, `on_execute_tool_error`, `after_run` и т.д.
- `DatabaseLoggingHook` должен быть `AgentHook` (tool-события) + обёртки `bus.publish_inbound/outbound` (content), а НЕ классом с sync-колбэками из старого плана.
- Monkey-patch'и уже дублируются: `ContextGovernor.normalize_tool_result` (только gateway) и `_assemble_outbound` (gateway + cli — почти идентичный код).

---

# ✅ Шаги миграции

## Step 0 — `ConfigService`

**Файл:** `lib/services/config_service.py`

**Что сделать:**
- Обернуть существующий `config.py`: загрузка `SETTINGS` (project.json → config.json → .secrets.env), `AttrDict`, резолв `${VAR}`.
- Обернуть `_load_runtime_config(config.json)` из nanobot + `sync_workspace_templates`.
- Инъекция API-ключей провайдеров: `SETTINGS.providers.*.api_key → config.providers.*.api_key`.
- Применение таймаутов: `NANOBOT_LLM_TIMEOUT_S`, `config.tools.exec.timeout`, `config.agents.defaults.max_tool_iterations`.
- Перенести `_settings_section(name)` (нормализация dict/attr доступа к SETTINGS).

**Публичный API:**
```python
class ConfigService:
    def load(self, script_dir: Path, workspace_dir: Path) -> RuntimeConfig
        # загружает SETTINGS + runtime config, инъекция ключей, таймауты, sync_workspace_templates
    def settings_section(self, name: str, default: dict | None = None) -> dict
    def apply_provider_keys(self, config) -> None
    def apply_timeouts(self, config, *, llm_timeout, exec_timeout, max_iterations) -> None
```

**Проверка шага:**
- [x] Выполнен сам шаг (шаг отмечен после прохождения проверки ниже)
- [ ] `python -c "from lib.services.config_service import ConfigService; print(ConfigService)"` — импортируется без nanobot
- [ ] `ConfigService().load()` возвращает конфиг с api_key (при наличии `.secrets.env`)
- [ ] `os.environ["NANOBOT_LLM_TIMEOUT_S"]` устанавливается при `llm_timeout >= 0`
- [ ] `sync_workspace_templates` вызывается ровно один раз при загрузке
- [ ] `settings_section("channels")` работает и для dict, и для object-подобных SETTINGS
- [ ] `python gateway.py` — стартует и работает как раньше (поведение не изменилось)
- [ ] Юнит-тесты `tests/test_config_service.py` (без nanobot): манипулируем `config.SETTINGS` через mock, проверяем `settings_section`, инъекцию ключей, таймауты

---

## Step 1 — `SessionStorageService`

**Файл:** `lib/services/session_storage.py`

**Что сделать:**
- Единый выбор хранилища из источников (по приоритету): аргументы CLI (`--storage`/`-S`) → `session_manager.json` → `SETTINGS.gateway.storage` → авто (PG если есть DSN).
- Создание `PGSessionManager` (из `lib/session/pg_session_manager.py`) или `SessionManager` (JSONL).
- Флаг `configure_db=True`: вызов `utils.db.configure(dsn)` + `os.environ["DATABASE_URL"] = dsn` (важно для `tools.exec.allowedEnvKeys`).
- При `storage=postgres` без DSN — понятная ошибка + `sys.exit(1)`.

**Публичный API:**
```python
class SessionStorageService:
    def create(self, config, *, storage_override: str | None = None,
               configure_db: bool = True, workspace_dir: Path | None = None):
        # storage: "auto" | "postgres" | "file"
```

**Проверка шага:**
- [ ] `storage=postgres` + DSN → создаётся `PGSessionManager`
- [ ] `storage=file` → создаётся `SessionManager` (даже если DSN есть)
- [ ] `storage=auto` + DSN → `PGSessionManager`; без DSN → `SessionManager`
- [ ] `storage=postgres` без DSN → понятное сообщение и `sys.exit(1)`
- [ ] `configure_db=True` → `utils.db.configure` вызван и `os.environ["DATABASE_URL"]` установлен
- [ ] Согласовано с `session_manager.json` (если файл появится — его значения побеждают конфиг)
- [ ] Юнит-тесты `tests/test_session_storage.py`: мокаем `PGSessionManager`/`SessionManager`, проверяем все комбинации storage

---

## Step 2 — `RuntimePatcher`

**Файл:** `lib/services/runtime_patcher.py`

**Что сделать:**
- Собрать ВСЕ monkey-patch'и в одном классе:
  1. `patch_context_governor(settings, workspace_dir, config)` — выгрузка больших результатов в `data_store/` (из gateway). Исключение `read_file`, `persist_threshold`, `persist_max_files`, `persist_max_age_hours`.
  2. `patch_assemble_outbound(agent, tool_audit_hook)` — внедрение `_tool_audit` в `metadata` (общий код из gateway и cli — убрать дублирование).
- Каждый патч в try/except с fallback: если API nanobot изменился — предупреждение, но не падение.
- Проверка версии/наличия атрибутов перед патчем (например `hasattr(ContextGovernor, "normalize_tool_result")`).
- Вернуть отчёт `PatchReport` (что применено / что пропущено).

**Публичный API:**
```python
class RuntimePatcher:
    def patch_context_governor(self, settings, workspace_dir: Path, config) -> bool
    def patch_assemble_outbound(self, agent, tool_audit_hook) -> bool
    def apply_all(self, config, settings, workspace_dir: Path, agent, tool_audit_hook) -> PatchReport
```

**Проверка шага:**
- [ ] В `gateway.py` и `cli_agent.py` НЕ осталось inline-monkey-patch'ей (проверить `grep -n "_assemble_outbound\|normalize_tool_result" gateway.py cli_agent.py` — только импорты/вызовы `RuntimePatcher`)
- [ ] `patch_context_governor` при `persist_threshold=0` — no-op
- [ ] `patch_context_governor` при `persist_threshold>0`: результат > порога сохраняется в `data_store/`, в контекст подставляется ссылка `[Result saved to data_store/...]`
- [ ] `patch_assemble_outbound`: после хода агента `result.metadata["_tool_audit"]` заполнен (при наличии вызовов инструментов)
- [ ] При изменённом API nanobot патч не роняет процесс (предупреждение + `PatchReport`)
- [ ] Юнит-тесты `tests/test_runtime_patcher.py`: применяем патч к mock-объектам, проверяем `PatchReport` и fallback

---

## Step 3 — `TranscriptionService`

**Файл:** `lib/services/transcription_service.py`

**Что сделать:**
- Перенести `_resolve_transcription_key(config)` и `_resolve_transcription_base(config)` из `gateway.py` (openai/groq, при ошибке — пустая строка).
- Перенести `transcription_language` (из `config.channels.transcription_language`).

**Публичный API:**
```python
class TranscriptionService:
    def __init__(self, config): ...
    def get_api_key(self) -> str
    def get_base_url(self) -> str
    def get_language(self) -> str | None
```

**Проверка шага:**
- [ ] `TranscriptionService(config).get_api_key()` для `openai`/`groq` возвращает корректный ключ
- [ ] Неизвестный провайдер → пустая строка (не исключение)
- [ ] Отсутствующий атрибут в конфиге → пустая строка (не исключение)
- [ ] `get_base_url()` для groq возвращает `https://api.groq.com/openai/v1` (или значение `api_base`)
- [ ] Юнит-тесты: перенести существующие тесты `TestResolveTranscriptionKey`/`TestResolveTranscriptionBase` из `test_gateway.py` без изменения логики
- [ ] `python gateway.py` — Postgres-канал получает ключ транскрипции как раньше

---

## Step 4 — `ChannelFactory`

**Файл:** `lib/services/channel_factory.py`

**Что сделать:**
- Создание `ChannelManager(config, bus, session_manager=...)` (стандартные каналы nanobot).
- Создание Redis-канала по `SETTINGS.channels.redis` (проброс `send_progress`, `send_tool_hints`, `show_reasoning`).
- Создание Postgres-канала по `SETTINGS.channels.postgres` (DSN, транскрипция через `TranscriptionService`).
- Сообщения `✓ Redis channel enabled / PostgreSQL channel enabled` — сохранить.

**Публичный API:**
```python
class ChannelFactory:
    def __init__(self, transcription: TranscriptionService): ...
    def create_all(self, config, settings, bus, session_manager) -> ChannelManager
```

**Проверка шага:**
- [ ] Redis `enabled=false` → канал НЕ создан, выводится `Redis channel disabled`
- [ ] Redis `enabled=true` → `channels.channels["redis"]` существует
- [ ] Postgres `enabled=true` без DSN → сообщение об ошибке, канал не создан
- [ ] Postgres `enabled=true` с DSN → канал создан, транскрипция настроена (`transcription_api_key` заполнен)
- [ ] Юнит-тесты `tests/test_channel_factory.py`: мокаем `ChannelManager`, `RedisChannel`, `PostgresChannel`
- [ ] `python gateway.py` — все каналы регистрируются как раньше (`Channels enabled: ...`)

---

## Step 5 — `SubprocessManager`

**Файл:** `lib/services/subprocess_manager.py`

**Что сделать:**
- Запуск Streamlit: `streamlit run streamlit_app.py --server.headless true --server.port 8501`, лог в `logs/streamlit.log` (append).
- Если `streamlit_app.py` отсутствует — no-op.
- Корректная остановка: `terminate()` → `wait(timeout=5)` → `kill()` при зависании.
- Кросс-платформенность (Windows/Unix) — через `subprocess.Popen` + стандартные сигналы.

**Публичный API:**
```python
class SubprocessManager:
    def spawn_streamlit(self, script_path: Path, port: int = 8501) -> bool
    def terminate_all(self, timeout_sec: float = 5.0) -> None
```

**Проверка шага:**
- [ ] При старте gateway `Streamlit UI started on :8501`, процесс живёт (`ps`/`Get-Process`)
- [ ] При остановке gateway Streamlit завершается (порт 8501 освобождается)
- [ ] При падении Streamlit gateway продолжает работу (только предупреждение)
- [ ] Юнит-тесты `tests/test_subprocess_manager.py`: мокаем `subprocess.Popen`, проверяем спавн и terminate/kill-последовательность

---

## Step 6 — `PreloadService`

**Файл:** `lib/services/preload_service.py`

**Что сделать:**
- Разделить ДВА разных механизма (в текущем коде они несвязаны):
  1. **Gateway:** прогрев FAISS-индексов — `_preload_vector_indexes(store)` (через `store.preload_indexes()`).
  2. **CLI:** подгрузка файла кеша навыка — `_preload_audit_cache(config)` + `_background_audit_cache_refresh(config)` через `cache_provider_impl`.
- Фоновые задачи стартуют/останавливаются через `asyncio` (`asyncio.create_task` / отмена).

**Публичный API:**
```python
class PreloadService:
    def preload_vector_indexes(self, store) -> None            # async-обёртка
    def start_audit_cache_tasks(self, config) -> list[Task]    # async
    def stop_tasks(self, tasks: list[Task]) -> None
```

**Проверка шага:**
- [ ] В gateway при `in_memory_enabled=true` + DSN: векторные индексы прогреваются при старте (логи `vector index ... built in memory`)
- [ ] В cli: при свежем кеше (< 1 часа) повторная загрузка НЕ происходит
- [ ] В cli: при устаревшем кеше — `load_cache_from_postgres` вызывается
- [ ] Фоновая задача `_background_audit_cache_refresh` отменяется при выходе без исключений
- [ ] Юнит-тесты: перенести `TestPreloadAuditCache`/`TestBackgroundAuditCacheRefresh` из `test_cli_agent.py`; добавить тест на `preload_vector_indexes` с mock-store

---

## Step 7 — `AgentFactory` + `BusFactory`

**Файлы:** `lib/core/agent_factory.py`, `lib/core/bus_factory.py`

**Что сделать:**
- `BusFactory`: создание `MessageBus` + (опционально) обёртки `publish_inbound`/`publish_outbound` для логирования (см. Step 9).
- `AgentFactory.create(...)`: `AgentLoop.from_config(config, bus, session_manager=..., cron_service=..., hooks=[...])` + применение `RuntimePatcher.patch_assemble_outbound`.
- Подключение хуков: `ToolAuditHook` (обязательно) + `DatabaseLoggingHook` (если `enable_db_logging`).
- Сохранить различие: gateway НЕ передаёт `cron_service`, cli — передаёт.

**Публичный API:**
```python
class AgentFactory:
    def create(self, config, bus, session_manager, settings, *,
               cron_service=None,
               db_logging_service=None,
               audit_memory_store=None,
               audit_sync_service=None) -> AgentLoop
```

**Проверка шага:**
- [ ] Агент создаётся с `hooks` (ToolAuditHook присутствует)
- [ ] `_assemble_outbound` патчится через `RuntimePatcher` (не inline)
- [ ] При `enable_db_logging` — DatabaseLoggingHook подключён
- [ ] При создании с `cron_service` — CronService в агентах присутствует (cli)
- [ ] Юнит-тесты `tests/test_agent_factory.py`: мокаем `AgentLoop.from_config`, проверяем состав `hooks` и патч

---

## Step 8 — `GatewayRunner` + `ShutdownCoordinator`

**Файлы:** `lib/lifecycle/gateway_runner.py`, `lib/lifecycle/shutdown_coordinator.py`

**Что сделать:**
- `GatewayRunner.run_forever()`: цикл с exponential backoff (`1с → 2с → 4с → 8с → 16с → 30с`), clean shutdown выходит из цикла.
- Порядок остановки (перенести из `finally` в gateway):
  1. `AuditSyncService.stop(timeout_sec=10.0)`
  2. `AuditMemoryStore.publish()` → `store.close()`
  3. отмена `channels_task`, `channels.stop_all()`
  4. `SubprocessManager.terminate_all()`
  5. `agent.close_mcp()` → `agent.stop()` → `sessions.flush_all()`
- Обработка `KeyboardInterrupt`/`asyncio.CancelledError` с сообщением `Shutting down...`.

**Проверка шага:**
- [ ] При исключении в `run()` gateway перезапускается, пауза удваивается до 30с
- [ ] При чистом завершении цикл выходит (нет перезапуска)
- [ ] Ctrl+C → все сервисы остановлены в правильном порядке (лог порядка в shutdown_coordinator)
- [ ] Все несохранённые сессии сброшены на диск (`Flushed N session(s) to disk`)
- [ ] Юнит-тесты `tests/test_gateway_runner.py`: мокаем сервисы, проверяем backoff-последовательность и порядок stop

---

## Step 9 — `DbLoggingService` + `DatabaseLoggingHook`

**Файлы:** `lib/services/db_logging_service.py`, `lib/services/sql/create_logs_table.sql`, `workspace/hooks/database_logging_hook.py`

**Что сделать:**
- **`DbLoggingService`** (импортируется БЕЗ nanobot; только stdlib + psycopg2):
  - worker-поток с единственным psycopg2-подключением, очередь команд (неблокирующая запись), batch insert, flush по интервалу.
  - fallback: при недоступности БД логировать в JSONL-файл (`fallback_path`).
  - `get_stats()`: размер очереди, записано/ошибки, состояние подключения.
- **DDL** `gateway_logs` (см. SQL-блок ниже), индексы по `timestamp`, `session_id`, `event_type`, `level`.
- **`DatabaseLoggingHook(AgentHook)`** — переработать под реальный API nanobot:
  - tool-события: `before_execute_tool` (лог tool_call + start), `after_execute_tool` (лог tool_result + latency), `on_execute_tool_error` (лог error).
  - run-level: `after_run` (final content, usage → outbound summary).
- **Bus-обёртки** для inbound/outbound content (делать в `BusFactory`):
  - `publish_inbound` → `log_inbound(session_key, channel, content)`
  - `publish_outbound` → `log_outbound(channel, content, metadata)`; фильтровать служебные `_reasoning_delta/_stream_delta/_progress/_tool_hint/_turn_end` (логировать их как `outbound_delta`, финал — `outbound_final`).
- Конфиг в `project.json`: `logging.db.*` (enabled, table_name, schema, flush_interval_sec, batch_size, queue_maxsize, min_level).
- Интеграция в `ApplicationContext`: инициализировать ПЕРВЫМ; `start()` запускает worker; `stop(timeout_sec=15)` дозаписывает очередь.

**SQL-блок (для проверки):**
```sql
-- Последние 10 событий
SELECT timestamp, level, event_type, session_id, summary
FROM gateway_logs ORDER BY timestamp DESC LIMIT 10;

-- Статистика по типам событий за час
SELECT event_type, COUNT(*) FROM gateway_logs
WHERE timestamp > NOW() - INTERVAL '1 hour'
GROUP BY event_type ORDER BY count DESC;

-- Самые медленные инструменты за сутки
SELECT payload->>'tool' AS tool,
       AVG((metadata->>'latency_ms')::float) AS avg_ms,
       COUNT(*) AS calls
FROM gateway_logs
WHERE event_type = 'tool_result' AND timestamp > NOW() - INTERVAL '24 hours'
GROUP BY payload->>'tool' ORDER BY avg_ms DESC;
```

**Проверка шага:**
- [ ] В логах gateway при старте видно `DbLoggingService started`
- [ ] Таблица `gateway_logs` создана в БД (проверить `\d gateway_logs`)
- [ ] После сообщения агенту в таблице есть записи `inbound`/`outbound`/`tool_call`/`tool_result`
- [ ] `log_*` методы НЕ блокируют вызывающий поток (очередь, а не прямой INSERT)
- [ ] При остановке gateway все буферизованные логи записаны (очередь пуста, `get_stats()["queue_size"] == 0`)
- [ ] При недоступной БД: логи пишутся в fallback-JSONL, gateway работает дальше
- [ ] Импорт `from lib.services.db_logging_service import DbLoggingService` — без nanobot
- [ ] Юнит-тесты `tests/test_db_logging_service.py`: запись с мок-подключением, batch-flush, fallback, stop с дозаписью
- [ ] Юнит-тесты `tests/test_hooks_database_logging.py`: `AgentHook`-методы вызывают `log_*` с корректными аргументами

---

## Step 10 — `ApplicationContext`

**Файл:** `lib/core/application_context.py`

**Что сделать:**
- Единый bootstrap: создать и связать ВСЕ сервисы из шагов 0–9.
- Публикует: `config, settings, bus, session_manager, agent, db_logging_service, audit_sync_service, audit_memory_store, runtime_patcher, transcription_service, subprocess_manager, preload_service`.
- Управление жизненным циклом: `create()`, `start()`, `stop()` (через `ShutdownCoordinator`).
- Graceful degradation: если сервис не инициализировался (нет БД и т.п.) — работаем без него, но с предупреждением.
- Флаги: `enable_db_logging`, `enable_audit`, `enable_cron`, `storage_override`, `session_override`.

**Публичный API:**
```python
@dataclass
class ApplicationContext:
    script_dir: Path
    workspace_dir: Path
    config: any
    settings: any
    bus: any
    session_manager: any
    agent: any
    db_logging_service: Optional[DbLoggingService] = None
    audit_sync_service: Optional[AuditSyncService] = None
    audit_memory_store: Optional[AuditMemoryStore] = None
    runtime_patcher: Optional[RuntimePatcher] = None
    transcription_service: Optional[TranscriptionService] = None
    subprocess_manager: Optional[SubprocessManager] = None
    preload_service: Optional[PreloadService] = None

    @classmethod
    def create(cls, script_dir, workspace_dir, *, enable_db_logging=True,
               enable_audit=True, enable_cron=False, storage_override=None) -> "ApplicationContext"
    def start(self) -> None
    def stop(self) -> None
```

**Проверка шага:**
- [ ] `ApplicationContext.create()` создаёт все сервисы БЕЗ реального подключения к БД/агента (mock-friendly)
- [ ] Все сервисы доступны через атрибуты `ctx.*`
- [ ] `ctx.start()` запускает фоновые сервисы (audit-sync, db-logging)
- [ ] `ctx.stop()` останавливает всё в правильном порядке и без исключений
- [ ] При недоступной БД: контекст создаётся, `audit_sync_service`/`db_logging_service` могут быть `None`, но агент работает
- [ ] Юнит-тесты `tests/test_application_context.py`: мокаем все сервисы, проверяем create/start/stop и graceful degradation

---

## Step 11 — Рефакторинг `gateway.py` (тонкий оркестратор)

**Что сделать:**
- Заменить логику инициализации на `ApplicationContext.create()`.
- Оставить gateway-специфичное: `ChannelFactory`, `SubprocessManager.spawn_streamlit`, `GatewayRunner`, аудит-сервисы (переезжают в контекст).
- Удалить inline-патчи, загрузку конфига, выбор хранилища, таймауты (всё в сервисах).
- Целевой размер: **~120–150 строк** (реалистично, без потери поведений).

**Проверка шага:**
- [ ] `gateway.py` < 200 строк (target ~150)
- [ ] `grep -n "normalize_tool_result\|_assemble_outbound" gateway.py` — только импорт/вызов `RuntimePatcher`
- [ ] `python gateway.py` — все каналы, Streamlit, аудит-сервисы работают как раньше
- [ ] Ctrl+C → корректное завершение всех сервисов (порядок из Step 8)
- [ ] Тесты `tests/test_gateway.py` обновлены: вместо `main()`/`_resolve_*` тестируем новый оркестратор и сервисы

---

## Step 12 — Рефакторинг `cli_agent.py` (тонкий оркестратор)

**Что сделать:**
- Вынести в `lib/cli/`: `console_loop.py` (REPL + consume_outbound + typewriter), `display_config.py`, `hook_loader.py` (сканирование `workspace/hooks/*.py`).
- Сохранить ВСЕ фичи: `vanilla/patched`, `--storage/-S`, `--session/-s`, `_migrate_cron_store`, `CronService`, `_get_audit_cache_config`/`_preload_audit_cache` (через `PreloadService`), сканирование хуков, `DisplayConfig`.
- Целевой размер: **~150 строк**.

**Проверка шага:**
- [ ] `cli_agent.py` < 200 строк (target ~150)
- [ ] `python cli_agent.py` — vanilla-режим работает (REPL, typewriter, exit/Ctrl+C)
- [ ] `python cli_agent.py --patched --storage postgres` — работает (PGSessionManager)
- [ ] `python cli_agent.py -s my-session` — именованные сессии работают
- [ ] Хуки из `workspace/hooks/` загружаются (`✓ HookName loaded`)
- [ ] Все тесты из `test_cli_agent.py` переехали в `lib/cli/`-тесты и проходят
- [ ] При выходе сервисы останавливаются, сессии сбрасываются на диск

---

## Step 13 — `streamlit_app.py` — НЕ переписывать

**Что сделать:**
- Это поллинг-UI поверх `conversation_messages` (Postgres-канал), архитектурно отдельный клиент.
- НЕ создавать второй `ApplicationContext`/агента внутри Streamlit — это даст двойную обработку.
- Опционально: вынести SQL/JSONB-хелперы (`_decode_jsonb`, `_load_chat_history`, `_check_response`) в `lib/webui/` — поведение не менять.

**Проверка шага:**
- [ ] `python -m streamlit run streamlit_app.py --server.port 8501` — UI открывается
- [ ] Отправка сообщения → появляется строка в `conversation_messages` со `status='pending'`
- [ ] Gateway обрабатывает сообщение → UI отображает ответ ассистента
- [ ] Загрузка/скачивание файлов работает
- [ ] Существующие тесты `test_streamlit_app.py` проходят без изменений

---

## Step 14 — `pg_agent_worker.py` (опционально)

**Что сделать:**
- Задокументировать как legacy (работает через `Nanobot` напрямую, таблицы `agent_questions`/`agent_responses`).
- Опционально: перевести на `ApplicationContext`/`AgentFactory` (если воркер ещё используется).

**Проверка шага:**
- [ ] Зафиксировано в README: воркер работает независимо, рефакторинг точек входа его не ломает
- [ ] (если мигрировали) `python pg_agent_worker.py --help` работает; обработка вопроса → ответ в `agent_responses`
- [ ] (если мигрировали) тесты `test_pg_agent_worker.py` обновлены и проходят

---

## Step 15 — Миграция тестов, чистка, README

**Что сделать:**
- Обновить все тесты, которые ссылаются на приватные функции точек входа:
  - `test_gateway.py`: `_resolve_transcription_key/_base` → `TranscriptionService`; `main()` → новый оркестратор.
  - `test_cli_agent.py`: `_patch_agent_tool_audit` → `RuntimePatcher.patch_assemble_outbound`; REPL/display → `lib/cli/`.
- Добавить тесты на новые сервисы (см. «Проверка шага» в каждом шаге).
- Удалить мёртвый код из `gateway.py`/`cli_agent.py`.
- Обновить README: новая структура, диаграмма, таблица «что переиспользуется».
- Проверить покрытие (`pytest --cov`): цель > 70% по `lib/`.

**Проверка шага:**
- [ ] `python -m pytest -q` — все тесты проходят (включая ранее существовавшие 136)
- [ ] `python -m pytest --cov=lib --cov-report=term-missing -q` — покрытие по `lib/` ≥ 70%
- [ ] В `gateway.py`/`cli_agent.py` нет импортов неиспользуемых модулей
- [ ] README обновлён: структура, точки входа, DbLoggingService
- [ ] Нет дублирования: `grep -rn "from config import SETTINGS" gateway.py cli_agent.py lib/` — только в обёртках

---

# 🏆 Критерии приёмки всего рефакторинга

- [ ] `gateway.py` — только оркестрация (~150 строк), поведение не потеряно
- [ ] `cli_agent.py` — только оркестрация (~150 строк), все флаги и режимы работают
- [ ] `streamlit_app.py` — не затронут (архитектурно отдельный клиент)
- [ ] `ApplicationContext` создаёт и связывает все общие сервисы
- [ ] Оба monkey-patch'а — только в `RuntimePatcher`
- [ ] `DbLoggingService` пишет все события агента в БД, fallback JSONL, `get_stats()`
- [ ] Отдельные подключения для логов (DbLoggingService) и бизнес-операций (AuditSyncService)
- [ ] Graceful shutdown корректен на Windows и Linux
- [ ] Все тесты проходят, покрытие по `lib/` > 70%
- [ ] README обновлён
- [ ] Нет дублирования между точками входа

---

## 📊 Что переиспользуется между точками входа (итог)

| Сервис | gateway | cli | streamlit | pg_agent_worker |
|--------|:-------:|:---:|:--------:|:---------------:|
| `ConfigService` | ✅ | ✅ | через `config.SETTINGS` | через `config.SETTINGS` |
| `SessionStorageService` | ✅ | ✅ | ❌ | ❌ |
| `RuntimePatcher` | ✅ | ✅ | ❌ | ❌ |
| `TranscriptionService` | ✅ | ❌ | ❌ | ❌ |
| `ChannelFactory` | ✅ | ❌ | ❌ | ❌ |
| `SubprocessManager` | ✅ | ❌ | ❌ | ❌ |
| `PreloadService` | ✅ | ✅ | ❌ | ❌ |
| `AgentFactory` | ✅ | ✅ | ❌ | ⚠️ опционально |
| `DbLoggingService` | ✅ | ✅ | ❌ | ❌ |
| `AuditSyncService` | ✅ | ❌ | ❌ | ❌ |
| `AuditMemoryStore` | ✅ | ❌ | ❌ | ❌ |
