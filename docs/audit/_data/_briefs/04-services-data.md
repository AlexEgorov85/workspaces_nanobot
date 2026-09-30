# Work brief `04-services-data`

Product files: **12**, LOC: **3331**

## `lib/services/db_logging_service.py` — 1142 LOC (code 978)
- module: `lib.services.db_logging_service`
- docstring: DbLoggingService — структурированное логирование событий агента в PostgreSQL. Импортируется БЕЗ nanobot (psycopg2 импортируется лениво — модуль годен для тестов с мок-подключением и для сред без psycopg2). Архитектура: *
- static importers (17): `lib/channels/postgres_channel.py`, `lib/core/application_context.py`, `lib/hooks/database_logging_hook.py`, `lib/services/cache_load_service.py`, `lib/services/context_compaction.py`, `lib/services/duckdb_cache_store.py`, `lib/services/preload_service.py`, `lib/services/runtime_events_subscriber.py`, `lib/services/runtime_patcher.py`, `lib/services/session_cold_sync_service.py`, `tests/contract/test_session_manager_contract.py`, `tests/test_context_compaction_user_id.py`, `tests/test_db_logging_service.py`, `tests/test_hooks_database_logging.py`
- string/dynamic refs: 3
- test files touching it: — none —
- classes: 4, module functions: 3

### class `LogEvent` — lines 129-157 (29 LOC), 0 methods
- bases: object
- decorators: dataclass
- docstring: Одно событие для записи в БД (стройная таблица agent_gateway_logs). Контекст вопроса (user_id/agent_id/is_subagent/parent_*) живёт в отдельной таблице agent_question_runs (см. upsert_question_run) и здесь не дублируется 
- name referenced in 18 file(s); tests: 7

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `_FlushSentinel` — lines 161-162 (2 LOC), 0 methods
- bases: object
- decorators: dataclass
- docstring: — NONE —
- name referenced in 1 file(s); tests: 0

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `_QuestionRunRecord` — lines 166-183 (18 LOC), 0 methods
- bases: object
- decorators: dataclass
- docstring: Контекст вопроса для upsert в agent_question_runs (не в agent_gateway_logs).
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `DbLoggingService` — lines 186-1126 (941 LOC), 32 methods
- bases: object
- decorators: —
- docstring: Фоновый writer событий агента в PostgreSQL (без fallback-JSONL).
- name referenced in 6 file(s); tests: 4

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 189-259 | `(self, dsn: str | None=None, *, table_name: str, question_runs_table: str, schema: str='pu` | 3 | 0 | 6 | — |
| `start` | 265-276 | `(self) -> None` | 4 | 0 | 21 | Запустить worker-поток. |
| `stop` | 278-288 | `(self, timeout_sec: float=15.0) -> None` | 4 | 0 | 16 | Остановить worker, дождавшись опустошения очереди. |
| `is_running` | 290-291 | `(self) -> bool` | 3 | 1 | 4 | — |
| `log_event` | 297-300 | `(self, event: LogEvent) -> bool` | 2 | 7 | 9 | — |
| `register_request` | 315-366 | `(self, session_key: str | None, request_id: str | None, *, user_id: str | None=None, chat_` | 4 | 0 | 4 | Зарегистрировать контекст вопроса (upsert в agent_question_runs). Также сохраняет session_key → {request_id, u |
| `get_request_id` | 368-377 | `(self, session_key: str | None) -> str | None` | 7 | 0 | 6 | Получить request_id текущего вопроса для сессии. |
| `clear_request` | 379-384 | `(self, session_key: str | None) -> None` | 3 | 0 | 2 | Снять привязку вопроса по завершении прогона. |
| `finish_request` | 386-405 | `(self, request_id: str | None, *, status: str='finished', summary: str | None=None, respon` | 2 | 0 | 2 | Обновить статус/summary/response вопроса (upsert в agent_question_runs). |
| `log_inbound` | 407-439 | `(self, session_id: str, channel: str, content: str, *, message_id: str | None=None, sender` | 8 | 0 | 3 | — |
| `log_outbound` | 441-468 | `(self, session_id: str, channel: str, content: str, *, latency_ms: float | None=None, toke` | 3 | 0 | 3 | — |
| `log_tool_call` | 470-489 | `(self, session_id: str, tool_name: str, args: dict | None=None, *, tool_call_id: str | Non` | 2 | 0 | 4 | — |
| `log_tool_result` | 491-522 | `(self, session_id: str, tool_name: str, result: Any, latency_ms: float, *, tool_call_id: s` | 4 | 0 | 4 | — |
| `log_llm_call` | 524-562 | `(self, session_id: str, prompt: Any, response: Any, *, iteration: int | None=None, model: ` | 4 | 0 | 3 | Записать полный запрос и ответ LLM за одну итерацию. Полный ``messages`` (промпт) передаётся в ``payload["prom |
| `log_error` | 564-581 | `(self, error: str, *, session_id: str | None=None, context: dict | None=None, request_id: ` | 2 | 0 | 1 | — |
| `log_sync_event` | 583-611 | `(self, event_type: str, summary: str, payload: dict | None=None, *, name: str | None=None,` | 4 | 0 | 0 | Записать событие из PG→DuckDB sync-пути. Используется из worker-потока ``PgDuckDbSyncService`` (и аналогичных) |
| `get_stats` | 613-621 | `(self) -> dict[str, Any]` | 2 | 0 | 7 | — |
| `_compute_oldest_queued_age_sec` | 623-645 | `(self) -> float | None` | 5 | 1 | 1 | Возраст самого старого ``LogEvent`` в очереди (секунды). Учитываются ТОЛЬКО объекты ``LogEvent`` с непустым `` |
| `_should_log` | 651-658 | `(self, level: str) -> bool` | 3 | 1 | 1 | Проверить, что ``level`` не ниже ``self._min_level``. Сравнение по числовой шкале (``DEBUG=0``, ``INFO=1``, `` |
| `_enqueue` | 660-699 | `(self, event: LogEvent) -> bool` | 4 | 3 | 3 | Неблокирующе положить событие в очередь. Перед постановкой в очередь резолвит ``event.user_id`` по трём ветвям |
| `_resolve_event_user_id` | 701-723 | `(self, event: LogEvent) -> None` | 11 | 1 | 1 | Резолв ``event.user_id`` из индекса (security boundary path). Вызывается из :py:meth:`_enqueue` перед постанов |
| `_worker` | 725-796 | `(self) -> None` | 15 | 0 | 3 | Главный цикл worker-потока: drain очереди → батч → flush. Алгоритм: 1. Получить из очереди элемент с таймаутом |
| `_db_run` | 802-808 | `(self, fn)` | 2 | 4 | 2 | Выполнить ``fn(conn)`` на свободном соединении общего пула ``utils.db``. |
| `_ensure_schema` | 810-845 | `(self, conn: Any) -> None` | 5 | 2 | 2 | Проверить существование таблиц логов/контекста вопросов. Сервис НЕ провижинит схему: таблицы ``agent_question_ |
| `_flush_batch` | 847-890 | `(self, batch: list[LogEvent]) -> None` | 9 | 4 | 1 | Вставить батч через общий пул ``utils.db`` (без своего соединения). Использует ``psycopg2.extras.execute_batch |
| `_insert_batch` | 892-917 | `(self, conn: Any, batch: list[LogEvent]) -> None` | 5 | 1 | 2 | Выполнить ``execute_batch`` INSERT на данном соединении. |
| `_handle_question_run` | 919-948 | `(self, rec: _QuestionRunRecord) -> None` | 7 | 1 | 1 | Обработать контекст вопроса: upsert в agent_question_runs. Через общий пул ``utils.db``. При неудаче запись вы |
| `_upsert_question_run` | 950-1021 | `(self, conn: Any, rec: _QuestionRunRecord) -> None` | 4 | 1 | 2 | Upsert контекста вопроса в agent_question_runs (без ON CONFLICT). Greenplum 6.x (база PostgreSQL 9.4) НЕ подде |
| `_drop_batch` | 1023-1031 | `(self, batch: list[LogEvent]) -> None` | 2 | 1 | 1 | Выбросить батч, когда БД недоступна (без записи в JSONL-файл). Увеличивает ``stats["failed"]`` и фиксирует ``l |
| `purge_empty_outbound` | 1037-1072 | `(self) -> int` | 7 | 1 | 2 | Удалить пустые outbound-события (stream-чанки / синтетические финалы). Удаляются ``outbound_final``/``outbound |
| `purge_old` | 1074-1116 | `(self, retention_days: int | None=None) -> tuple[int, int]` | 9 | 1 | 2 | Удалить события и question_runs старее ``retention_days``. Возвращает ``(удалено_событий, удалено_question_run |
| `_purge_old` | 1118-1126 | `(self) -> None` | 2 | 1 | 1 | Один шаг периодической очистки из worker-цикла. Пустой outbound-мусор чистим всегда (независимо от retention); |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `try_log_event` | 34-101 | `(svc: Any | None, log_event: LogEvent, *, producer: str, event_type: str) -> bool` | 5 | 10 | Defensive helper для producer'ов: попробовать записать событие. Используется из sync-путей (``PgDuckDbSyncServ |
| `_json_safe` | 104-125 | `(value: Any) -> Any` | 9 | 1 | Рекурсивно привести значение к JSON-серизуемому виду. Промпт/ответ могут содержать несеризуемые объекты (datac |
| `_count_by_type` | 1129-1142 | `(batch: list[LogEvent]) -> dict[str, int]` | 4 | 1 | Подсчитать число событий каждого event_type в батче. Возвращает dict с ключами = event_type (только непустые з |

## `lib/services/session_cold_sync_service.py` — 669 LOC (code 580)
- module: `lib.services.session_cold_sync_service`
- docstring: Background mirror для upstream SessionManager (JSONL) → PostgreSQL. См. спеку ``openspec/specs/storage/session-hybridization/spec.md`` и правила пула в ``openspec/changes/storage-hybridization/design.md`` § «Connection p
- static importers (3): `lib/core/application_context.py`, `tests/test_session_cold_sync_service.py`, `tests/test_storage_hybridization_lifecycle.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 1

### class `SessionColdSyncService` — lines 62-661 (600 LOC), 28 methods
- bases: object
- decorators: —
- docstring: Зеркалирует upstream JSONL → PG в фоне. Получает пул через DI: в ``ApplicationContext`` создаётся ``utils.db`` (через ``utils.db.configure(dsn)`` — синглтон), ``SessionColdSyncService`` использует ``utils.db.transaction(
- name referenced in 3 file(s); tests: 2

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 74-140 | `(self, session_manager: 'SessionManager', pg_dsn: str, schema: str='public', meta_table: s` | 8 | 0 | 6 | — |
| `enabled` | 143-144 | `(self) -> bool` | 1 | 0 | 9 | — |
| `start` | 146-158 | `(self) -> None` | 4 | 0 | 21 | Запустить фоновый sync-поток. No-op если ``enabled=False``. |
| `stop` | 160-184 | `(self, timeout_sec: float=30.0) -> None` | 5 | 0 | 16 | Корректно остановить поток. Устанавливает ``self._running = False`` и ждёт завершения текущего цикла в предела |
| `_worker` | 186-197 | `(self) -> None` | 6 | 0 | 3 | — |
| `_compute_delay` | 199-206 | `(self) -> float` | 5 | 1 | 1 | — |
| `_do_sync_batch` | 208-252 | `(self) -> None` | 7 | 2 | 2 | Один цикл sync: leader-election → read upstream → D23/D11 sync. Реализует: - leader-election через ``pg_try_ad |
| `_sync_cycle` | 254-260 | `(self) -> None` | 1 | 1 | 2 | Backward-compat alias: финальный flush при ``stop()``. Реализация идентична ``_do_sync_batch`` (вызывается при |
| `_sync_session_with_detection` | 262-310 | `(self, key: str) -> None` | 11 | 1 | 1 | Один ключ: D23 stale-check + reverse-lag + LWW sync. Структура (согласно tasks.md 3.4): 1. ``existing is None` |
| `_do_lww_sync` | 312-317 | `(self, key: str, snapshot: Any, jsonl_updated_at: datetime) -> None` | 2 | 2 | 1 | LWW-sync: записать meta + messages в PG. |
| `_is_stale_logged_recently` | 319-328 | `(self, key: str) -> bool` | 4 | 1 | 1 | True, если для ``key`` уже логировали stale-detected за последние ``_STALE_LOG_DEDUP_TTL`` секунд. |
| `_read_upstream` | 330-331 | `(self) -> list[dict[str, Any]]` | 2 | 1 | 1 | — |
| `_upsert_meta` | 333-365 | `(self, key: str, snapshot: Any, updated_at: datetime) -> None` | 5 | 1 | 1 | — |
| `_replace_messages` | 367-396 | `(self, key: str, snapshot: Any) -> None` | 9 | 1 | 1 | — |
| `_cleanup_missing` | 398-433 | `(self, upstream_keys: set[str]) -> None` | 8 | 1 | 1 | Удалить из PG сессии, которых больше нет в upstream JSONL. Upstream JSONL — единственный source of truth. Люба |
| `_read_pg_updated_at` | 435-446 | `(self, key: str) -> datetime | None` | 3 | 1 | 1 | — |
| `_count_pg_sessions` | 448-453 | `(self) -> int` | 2 | 1 | 1 | — |
| `_select_all_pg_keys` | 455-462 | `(self) -> list[str]` | 3 | 1 | 1 | — |
| `_try_advisory_xact_lock` | 464-484 | `(self) -> bool` | 4 | 1 | 1 | Per-transaction advisory lock. ``pg_try_advisory_xact_lock`` держит lock до конца транзакции (COMMIT/ROLLBACK) |
| `_run_in_tx` | 486-496 | `(self, fn)` | 2 | 7 | 1 | Выполнить ``fn(conn)`` в короткой транзакции через utils.db. ``utils.db.transaction()`` сам управляет COMMIT/R |
| `_log_failure` | 498-513 | `(self, exc: Exception) -> None` | 2 | 1 | 1 | — |
| `_log_deleted` | 515-529 | `(self, key: str) -> None` | 2 | 1 | 1 | — |
| `_log_stale` | 531-561 | `(self, key: str, jsonl_updated_at: datetime, pg_updated_at: datetime) -> None` | 2 | 1 | 1 | D23: PG свежее JSONL + tolerance → логируем ``session_stale_detected``. Используется in-memory dedup ``_stale_ |
| `_log_lag_exceeded` | 563-589 | `(self, key: str, jsonl_updated_at: datetime, pg_updated_at: datetime) -> None` | 2 | 1 | 1 | Reverse-lag: JSONL свежее PG + threshold → логируем ``sync_lag_exceeded``. |
| `get_stats` | 591-627 | `(self) -> dict[str, Any]` | 3 | 0 | 7 | Метрики для health-check. D-Pool.6: ``pool_size`` / ``pool_available`` / ``pool_wait_seconds`` публикуются в s |
| `_read_pool_size` | 629-649 | `(self) -> tuple[int | None, int | None]` | 7 | 1 | 1 | Читает pool_size / pool_available из utils.db.get_stats(). Если utils.db.get_stats недоступен (например, в тес |
| `_validate_ident` | 652-654 | `(part: str) -> None` | 3 | 1 | 3 | — |
| `_quote` | 657-661 | `(cls, ident: str) -> str` | 4 | 2 | 3 | — |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `resolve_default_sqlite_path` | 664-669 | `() -> Path` | 1 | 0 | Дефолтный путь к SQLite-файлу ``LLMUsageStore`` из design D4. Используется в ``lib.services.llm_usage_store_fa |

## `lib/services/config_service.py` — 284 LOC (code 223)
- module: `lib.services.config_service`
- docstring: ConfigService — единая точка загрузки конфигурации проекта. Отвечает за: * доступ к глобальным ``SETTINGS`` (собираются в ``config.py``: project.json → config.json → .secrets.env, резолв ``${VAR}``); * загрузку runtime-к
- static importers (3): `gateway.py`, `lib/core/application_context.py`, `tests/test_config_service.py`
- string/dynamic refs: 1
- test files touching it: — none —
- classes: 1, module functions: 0

### class `ConfigService` — lines 26-284 (259 LOC), 9 methods
- bases: object
- decorators: —
- docstring: Загрузка и нормализация конфигурации проекта. Всегда возвращает профильно-разрешённый ``SETTINGS`` (один источник — ``config.SETTINGS``, опубликованный ``_initialize_settings(profile)`` из application entrypoint до любог
- name referenced in 3 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 39-50 | `(self, script_dir: Path | None=None, workspace_dir: Path | None=None, *, settings_override` | 3 | 0 | 6 | — |
| `settings` | 57-70 | `(self) -> Any` | 2 | 0 | 9 | SETTINGS — глобальный (``_LazySettings`` proxy, материализованный ``_initialize_settings(profile)``) или ``set |
| `settings_section` | 72-82 | `(self, name: str, default: dict | None=None) -> dict` | 1 | 0 | 1 | Вернуть top-level секцию SETTINGS как dict (пусто, если нет). SETTINGS может быть как dict-ом, так и объектом  |
| `get_int` | 84-95 | `(self, *path: str, default: int=0) -> int` | 3 | 0 | 0 | Достать int из вложенного dict/AttrDict по цепочке ``path``. Если на любом уровне атрибут отсутствует или не п |
| `get_str` | 97-104 | `(self, *path: str, default: str='') -> str` | 2 | 0 | 0 | Достать str из вложенного dict/AttrDict по цепочке ``path``. Пустая строка и ``None`` считаются отсутствием →  |
| `load` | 110-157 | `(self, script_dir: Path | None=None, workspace_dir: Path | None=None, *, sync_templates: b` | 6 | 0 | 10 | Загрузить и собрать финальный runtime-конфиг nanobot. Args: script_dir: корень проекта (где лежит config.json) |
| `_pre_resolve_env_refs` | 159-240 | `(self, script_dir: Path | None) -> None` | 33 | 1 | 2 | Pre-resolve ``${VAR}`` placeholders in config.json from SETTINGS. nanobot's ``_load_runtime_config`` resolves  |
| `apply_provider_keys` | 246-257 | `(self, config: Any) -> None` | 7 | 1 | 2 | Подставить api_key провайдеров из SETTINGS.providers в runtime-конфиг. |
| `apply_timeouts` | 259-284 | `(self, config: Any, *, llm_timeout: int | None=-1, exec_timeout: int | None=-1, max_iterat` | 9 | 0 | 1 | Применить таймауты к конфигу и переменным окружения. ``llm_timeout >= 0`` → ``NANOBOT_LLM_TIMEOUT_S`` в os.env |

## `lib/services/text_splitter.py` — 217 LOC (code 168)
- module: `lib.services.text_splitter`
- docstring: Разбиение длинных текстов на перекрывающиеся чанки (универсальный слой). Модуль изолирован от остальной системы — может быть расширен или заменён без изменения индексаторов (tools/build_vectors.py и др.). Публичный API: 
- static importers (2): `tests/test_text_splitter.py`, `tools/build_vectors.py`
- string/dynamic refs: 2
- test files touching it: — none —
- classes: 0, module functions: 6

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `split_text` | 17-41 | `(text: str, chunk_size: int=500, chunk_overlap: int=80) -> list[str]` | 4 | 2 | Рекурсивное разбиение текста на чанки с перекрытием. Стратегия разделителей (от приоритетных к запасным): 1. Д |
| `_recursive_split` | 44-60 | `(text: str, chunk_size: int, chunk_overlap: int) -> list[str]` | 3 | 1 | Рекурсивное дробление с перебором разделителей. |
| `_split_by_separator` | 63-80 | `(text: str, pattern: str, chunk_size: int, chunk_overlap: int) -> list[str] | None` | 6 | 1 | Попробовать разбить по разделителю pattern. |
| `_merge_into_chunks` | 86-130 | `(parts: list[str], chunk_size: int, chunk_overlap: int) -> list[str]` | 13 | 1 | Склеить сегменты в чанки нужного размера с перекрытием (жадно, вперёд). В отличие от старой реализации никогда |
| `_split_by_chars` | 133-153 | `(text: str, chunk_size: int, chunk_overlap: int) -> list[str]` | 6 | 1 | Посимвольное разбиение — последняя надежда. Гарантирует завершение: как только достигнут конец текста, остаток |
| `build_chunks` | 156-217 | `(row: dict, embedding_cols: list[str], chunk_size: int=500, chunk_overlap: int=80) -> list` | 19 | 2 | Построить список чанков для одной строки таблицы. Если все колонки короче chunk_size — возвращается один чанк. |

## `lib/services/llm_client.py` — 199 LOC (code 165)
- module: `lib.services.llm_client`
- docstring: Единый HTTP-клиент к LLM (OpenAI-compatible /chat/completions). Консолидация: раньше каждый потребитель писал собственный httpx-POST с ретраями — навык ``audit_analyzer`` (``scripts/llm.py``) и бенчмарк (``benchmarks/eva
- static importers (3): `benchmarks/evaluator.py`, `workspace/skills/audit_analyzer/scripts/llm.py`, `workspace/skills/legal_summarizer/scripts/llm/client.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 5

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_resolve_cfg` | 31-41 | `(cfg: dict[str, Any] | None) -> dict[str, Any]` | 2 | 1 | Вернуть переданный конфиг или резолвнуть агентский (без переопределений). ``resolve_llm_config`` импортируется |
| `call_llm` | 44-97 | `(messages: list[dict[str, Any]], *, cfg: dict[str, Any] | None=None, context: list[dict[st` | 15 | 3 | Вызвать LLM и вернуть текстовый ответ (только ``content``). Args: messages: Сообщения (system / user / assista |
| `call_llm_json` | 100-140 | `(messages: list[dict[str, Any]], *, cfg: dict[str, Any] | None=None, context: list[dict[st` | 2 | 1 | Вызвать LLM и распарсить ответ как JSON-объект. При любом сбое (сеть, невалидный JSON, не dict) возвращает ``N |
| `_post_json` | 144-176 | `(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float, max_retries: ` | 4 | 1 | httpx POST + retry-цикл с exponential backoff через ``retry_on_exception``. Ретраит только 429 / TimeoutExcept |
| `_parse_json_object` | 179-199 | `(text: str) -> dict[str, Any] | None` | 7 | 1 | Распарсить ответ LLM в JSON-объект (с чисткой markdown-обёрток). |

## `lib/services/db_logging_bus.py` — 166 LOC (code 138)
- module: `lib.services.db_logging_bus`
- docstring: Утилиты: связать DbLoggingService с MessageBus через обёртки. Проблема: ``nanobot.bus.queue.MessageBus`` — простая асинхронная очередь, у неё нет встроенных хуков на ``publish_inbound``/``publish_outbound``. Чтобы логиро
- static importers (2): `lib/core/application_context.py`, `tests/test_hooks_database_logging.py`
- string/dynamic refs: 1
- test files touching it: — none —
- classes: 0, module functions: 2

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `make_inbound_logger` | 49-107 | `(service: Any, agent_id: str | None=None) -> Callable[[Any], Awaitable[None]]` | 16 | 2 | Создать async-логгер для ``MessageBus.publish_inbound``. Получает ``InboundMessage`` (см. ``nanobot.bus.events |
| `make_outbound_logger` | 110-166 | `(service: Any, agent_id: str | None=None) -> Callable[[Any], Awaitable[None]]` | 22 | 2 | Создать async-логгер для ``MessageBus.publish_outbound``. Получает ``OutboundMessage`` (см. ``nanobot.bus.even |

## `lib/services/subprocess_manager.py` — 149 LOC (code 116)
- module: `lib.services.subprocess_manager`
- docstring: SubprocessManager — запуск и корректное завершение дочерних процессов. Перенесено из gateway.py (запуск Streamlit UI на :8501 и его остановка). Сейчас в нём только Streamlit; добавление новых фоновых процессов — через до
- static importers (2): `gateway.py`, `tests/test_subprocess_manager.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 0

### class `SubprocessManager` — lines 33-149 (117 LOC), 5 methods
- bases: object
- decorators: —
- docstring: Управление фоновыми процессами (Streamlit UI; добавляются через ``spawn_*``). Attributes: _log_dir: директория для логов (создаётся при первом ``spawn_*``). _processes: список ``(Popen, file_handle)`` — для terminate в `
- name referenced in 3 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 41-50 | `(self, log_dir: Path | None=None) -> None` | 2 | 0 | 6 | — |
| `spawn_streamlit` | 56-111 | `(self, script_path: Path, port: int | None=None) -> bool` | 6 | 0 | 2 | Запустить Streamlit UI как subprocess. Args: script_path: путь к ``streamlit_app.py``. port: порт (по умолчани |
| `__enter__` | 113-114 | `(self) -> SubprocessManager` | 1 | 0 | 4 | — |
| `__exit__` | 116-117 | `(self, *exc) -> None` | 1 | 0 | 1 | — |
| `terminate_all` | 123-149 | `(self, timeout_sec: float | None=None) -> None` | 7 | 1 | 3 | Корректно завершить все subprocess'ы. Алгоритм для каждого процесса: 1. ``proc.terminate()`` — послать SIGTERM |

## `lib/services/session_storage.py` — 140 LOC (code 115)
- module: `lib.services.session_storage`
- docstring: SessionStorageService — единый выбор и создание хранилища сессий. Объединяет логику выбора PG/File/auto из gateway.py и cli_agent.py: * источник конфигурации — параметр ``pg`` (уже разрешённая секция channels.postgres от
- static importers (2): `lib/core/application_context.py`, `tests/test_session_storage.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 2, module functions: 0

### class `SessionStorageError` — lines 26-27 (2 LOC), 0 methods
- bases: Exception
- decorators: —
- docstring: Хранилище сессий настроено некорректно (например, postgres без DSN).
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|

### class `SessionStorageService` — lines 30-140 (111 LOC), 1 methods
- bases: object
- decorators: —
- docstring: Фабрика SessionManager / PGSessionManager на основе конфигурации. Замечание: до этого плана этот класс сам читал ``session_manager.json`` через ``_load_override()`` и применял его к ``pg_cfg`` **после** ``SETTINGS`` — эт
- name referenced in 2 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `create` | 42-140 | `(self, config: Any, *, storage: str='auto', pg: dict | None=None, configure_db: bool=True,` | 26 | 0 | 15 | Создать SessionManager подходящего типа. Алгоритм: 1. Берём уже разрешённый ``pg`` от ConfigurationResolver; 2 |

## `lib/services/llm_observer.py` — 110 LOC (code 90)
- module: `lib.services.llm_observer`
- docstring: Подключение upstream ``LLMUsageStore`` через observer-pipeline. См. спеку ``openspec/specs/storage/usage-store/spec.md`` и design D3. Единственный стабильный путь подключения ``LLMUsageStore`` как LLM-call observer в наш
- static importers (2): `lib/core/agent_factory.py`, `tests/test_storage_hybridization_lifecycle.py`
- string/dynamic refs: 2
- test files touching it: — none —
- classes: 0, module functions: 3

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `attach_llm_observer` | 31-51 | `(provider: LLMProvider, store: Any | None) -> bool` | 3 | 1 | Подключить ``store.record`` как LLM-call observer. Returns: ``True`` если observer успешно подключён, ``False` |
| `attach_fallback_model_observer` | 54-80 | `(provider: LLMProvider, bus_or_handler: Any | None) -> bool` | 5 | 1 | Подключить ``set_fallback_model_observer`` для FallbackProvider. Это отдельный observer для семантических собы |
| `wrap_provider_snapshot_loader` | 83-110 | `(base_loader: Callable[..., Any], store: Any | None, bus: Any | None=None) -> Callable[...` | 3 | 3 | Обернуть ``provider_snapshot_loader`` для подключения observer. На каждом вызове ``base_loader(...)`` обёртка: |

## `lib/services/llm_config.py` — 88 LOC (code 68)
- module: `lib.services.llm_config`
- docstring: Единый резолв LLM-конфигурации из глобальных SETTINGS. Подход (вынесен из навыка audit_analyzer, scripts/skill_config.py): дефолт берётся из ``agents.defaults`` (модель/провайдер) и ``providers.<provider>`` (api_base/api
- static importers (4): `benchmarks/runner.py`, `lib/core/skill_config.py`, `lib/services/llm_client.py`, `tests/test_llm_config.py`
- string/dynamic refs: 1
- test files touching it: — none —
- classes: 0, module functions: 2

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `resolve_llm_config` | 26-71 | `(overrides: dict | None=None) -> dict[str, Any]` | 27 | 5 | Собрать LLM-конфиг (provider/model/api_base/api_key/параметры). Args: overrides: Специфичные переопределения ( |
| `ensure_llm_env` | 74-88 | `() -> None` | 4 | 2 | Гарантировать ``LLM_API_KEY`` в окружении для резолва ``${...}``. Лоадеры конфигурации (nanobot ``resolve_conf |

## `lib/services/llm_usage_store_factory.py` — 85 LOC (code 66)
- module: `lib.services.llm_usage_store_factory`
- docstring: Фабрика для ``LLMUsageStore`` (upstream nanobot). Создаёт ``nanobot.llm_usage.store.LLMUsageStore`` по конфигурации ``gateway.usage_store.*``. Дефолтный путь — ``get_runtime_subdir("usage")/usage.db`` (см. design D4). Ес
- static importers (1): `lib/core/application_context.py`
- string/dynamic refs: 1
- test files touching it: — none —
- classes: 0, module functions: 3

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_ensure_dir` | 24-27 | `(path: Path) -> None` | 2 | 1 | — |
| `_default_sqlite_path` | 30-38 | `() -> Path` | 1 | 1 | Default ``LLMUsageStore`` SQLite path. nanobot 0.3.5 не предоставляет ``nanobot.paths.get_runtime_subdir``; ис |
| `create_usage_store` | 41-85 | `(usage_config: dict[str, Any] | None) -> Any | None` | 8 | 1 | Создать ``LLMUsageStore`` по конфигурации или вернуть ``None``. Args: usage_config: словарь из ``gateway.usage |

## `lib/services/transcription_service.py` — 82 LOC (code 66)
- module: `lib.services.transcription_service`
- docstring: TranscriptionService — резолвинг ключей/URL провайдера транскрипции. Голосовые сообщения в Postgres-канале транскрибируются внешним API (``openai`` / ``groq``). Этот сервис — единственное место, где выбирается, какой про
- static importers (3): `lib/core/application_context.py`, `tests/test_gateway.py`, `tests/test_transcription_service.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 1, module functions: 0

### class `TranscriptionService` — lines 23-82 (60 LOC), 5 methods
- bases: object
- decorators: —
- docstring: Настройки транскрипции голосовых для Postgres-канала. Attributes: _config: runtime-конфиг nanobot (с ``.channels.transcription_*`` и ``.providers.{openai,groq}.{api_key,api_base}``).
- name referenced in 3 file(s); tests: 2

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 31-32 | `(self, config: Any) -> None` | 1 | 0 | 6 | — |
| `provider` | 35-40 | `(self) -> str` | 2 | 0 | 4 | Имя провайдера (``"openai"`` / ``"groq"``) или ``""`` если не задано. |
| `get_api_key` | 42-55 | `(self) -> str` | 3 | 0 | 2 | API-ключ активного провайдера или пустая строка. Все ошибки (``AttributeError``, отсутствие ключа) → ``""``. П |
| `get_base_url` | 57-70 | `(self) -> str` | 5 | 0 | 2 | Базовый URL API транскрипции или пустая строка. Пустая строка означает «использовать стандартный endpoint пров |
| `get_language` | 72-82 | `(self) -> str | None` | 2 | 0 | 2 | Язык распознавания (``"ru"``, ``"en"`` и т.п.) или ``None``. ``None`` — автоопределение языка провайдером. ``" |

