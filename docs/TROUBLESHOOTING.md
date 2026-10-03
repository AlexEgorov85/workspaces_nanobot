# Troubleshooting

Диагностический runbook для типовых ошибок. Источник — `README.md` v2.4.0
(раздел «Troubleshooting»); сюда перенесён без изменений, чтобы освободить
навигационный хаб от деталей.

> **TL;DR для диагноста:** логи — в stderr (loguru, `sys.stderr`);
> статистика пула соединений — `utils.db.get_stats()`; время и состав снимка —
> payload событий `cache_load_started`/`cache_load_done` в
> `agent_gateway_logs` (снимком грузит и владеет capability `data` платформы,
> агент снимком не владеет); зависшие `processing`-задачи — их вернёт в пул
> фоновый `_unstick_loop`; для разблокировки сразу см. `docs/ARCHITECTURE.md`
> § «Воркеры не берут задачи».

---

## Ошибки конфигурации и переменных окружения

### `ValueError: LLM_API_KEY not set` / `ApiKey not found`

Причина: ключ провайдера не подставился в `os.environ`. Проверьте `.secrets.env`:

```ini
# providers: llm   ← секция обязательна
api_key=XavGPsHjtNt3uOtFGUhabUuad5PRm2D0W
```

Если секция и значение на месте, но ошибка остаётся — `ConfigService._pre_resolve_env_refs` не нашёл ключ.
Проверьте `config.json`: имя провайдера должно совпадать с секцией в `.secrets.env`
(case-insensitive). Имя env-переменной теперь каноническое — `LLM_API_KEY`
(вместо исторического `MISTRAL_API_KEY`).

### JSONC в `profiles/test.jsonc` не парсится

Только `//` и `/* */` поддерживаются. Хэштеги `#` — нет. Кавычки в DSN не должны
пересекаться с комментариями. (`config.json` — строгий JSON, комментариев не
допускает.)

---

## Ошибки подключения к БД

### `psycopg2.OperationalError: connection refused`

1. PostgreSQL/Greenplum запущен? `pg_isready` или `pg_lsclusters`.
2. DSN правильный? `psql "$DATABASE_URL"` работает?
3. На Greenplum 6.25 — `gssencmode=disable` (пул соединений в
   `workspace/utils/db.py:233` уже выставляет его через kwargs `connect()`,
   но если проблема — проверьте).
4. На PG 9.4 — минимум 3 retry, для GP — 50.

### `too many connections` (Greenplum)

`channels.postgres.pool.max_conn` (дефолт `4` в `workspace/utils/db.py`,
применяется к пулам воркеров и PG-сессий). Пул общий для всех сервисов ядра,
поэтому 4 слота на процесс — жёсткий бюджет. Если не хватает, увеличьте
`channels.postgres.pool.max_conn`; число потоков загрузки кэша возьмёт новое
значение автоматически. Мониторинг: `utils.db.get_stats()`.

---

## Ошибки синхронизации и кешей

### `FileNotFoundError: ~/.cache/nanobot/duckdb/cache.duckdb`

Снимок наполняет **только capability `data` платформы**: агент снимком не
владеет и ничего в него не публикует; снятый метод `DuckDbCacheStore.publish()`
не существует — сам `DuckDbCacheStore` снят 2026-10-01, а файл открывает и
закрывает `mcp-platform/libs/enterprise_data/snapshot/store.py`. Путь
объявляет платформа:

  1. `mcp-platform/platform.json` → `data.snapshot_path` (путь к **файлу**;
     пустое значение — «снимок ненастроен», операции снимка отвечают
     `snapshot_unavailable`)
  2. **default** (v2.5.2+): `~/.cache/nanobot/duckdb/cache.duckdb`
     (POSIX `fcntl` работает там штатно)

Файла нет, пока снимок не загружен, а загрузка разовая — она идёт при старте
capability `data`. Состав и время загрузки смотрите в payload события
`cache_load_done` журнала `agent_gateway_logs`. Старый путь
`workspace/skills/audit_analyzer/cache/audit_cache.duckdb` из
`project.json::in_memory_cache_path` больше не используется.

### `IO Error: Could not set lock on file .../cache.duckdb.tmp: Conflicting lock is held in PID 0`

DuckDB `ATTACH ... READ_WRITE` берёт эксклюзивный `flock`, который NFS `lockd`
не отдаёт (POSIX `fcntl` vs NFS NLM — несовместимые протоколы). На свежем файле
после `rm` ошибка воспроизводится стабильно (см. эмпирическую проверку в
коммитах `c522b55` / `85cad2a` и `duckdb/duckdb#4041`). Под `PID 0` в
сообщении — NFS-шный «lock без валидного владельца», а не реальный процесс.

**Решение** (с версии v2.5.2):

  * **по умолчанию** снимок уходит на `~/.cache/nanobot/duckdb/cache.duckdb` —
    POSIX `fcntl` работает там штатно, проблема исчезает без действий;
  * если хотите хранить снимок в другой локальной директории (например,
    `/var/lib/nanobot/cache/`) — путь снимка объявляет платформа,
    `mcp-platform/platform.json` → `data.snapshot_path` (ключ
    `gateway.cache.local_path` агента снят);
  * если путь уводит снимок на NFS (в том числе через symlink), capability
    `data` открывает файл с `UnsupportedFilesystemError`, а не предупреждением:
    сначала уберите NFS из `data.snapshot_path`;

### `FAISS preload: no data in cache`

Индексы в память прогревает capability `vectors` (лениво, отдельного вызова не
требует), а снимок наполняет capability `data`. Агент в обоих не участвует —
`CacheLoadService` (снят) и его `preload_indexes()` в дереве агента больше нет.

Если preload не нашёл данных, причина одна из трёх:

1. таблица векторов не попала в загрузку — смотрите `tables` и
   `missing_tables` в payload `cache_load_done` журнала `agent_gateway_logs`
   (те же поля отдаёт `SnapshotLoadService.get_stats()`);
2. загрузка не состоялась — в журнале `agent_gateway_logs` есть
   `cache_load_done` с `errors` и `loaded_ok`; состав снимка объявляет
   capability `data` (`mcp-platform/platform.json` → `data`);
3. файл кэша старше данных — время снимка в
   `cache_load_done.payload.loaded_at`; обновляется перезапуском процесса
   платформы.

---

## Бенчмарки и оценка

Подсистема бенчмарков качества удалена в фазе 1 миграции
`enterprise-mcp-platform`: пакет `benchmarks/` (runner, evaluator, scorer,
loader, reporter, db, hooks, models), скрипты `tools/legal_benchmark.py`,
`tools/test_audit.py`, таблицы
`agent_benchmark_runs` / `agent_benchmark_results` и секция `benchmark.*`
в `config.json`. Разделы этого файла про LLM-судью и загрузчик YAML- suites
больше не применимы.

> `tools/legacy_audit.py` при этом **остался** (файл на диске есть) и к
> бенчмаркам отношения не имеет: это zero-reference аудит legacy-символов —
> regression guard с тремя режимами `audit()` / `assert_no_legacy()` / `main()`,
> сканирующий `lib/`, `workspace/`, `tools/`, `tests/`, `sql/`.

**Не путать** с каталогом `tests/benchmarks/` — он остался: это тесты
quality-бенчмарков навыка `legal_summarizer` (проверка golden-датасета
`required_facts` и наличие canonical-модулей скилла), а не тесты пакета
`benchmarks/`.

---

## Web-UI

Streamlit-UI удалён в фазе 1 миграции `enterprise-mcp-platform`
(`streamlit_app.py`, `lib/services/subprocess_manager.py`, секция `streamlit.*`,
связанные тесты). Диагностика зависшего UI теперь сводится к каналу:
задача в `processing` дольше `processing_timeout` вернёт в пул `_unstick_loop`,
см. `docs/ARCHITECTURE.md` § «Воркеры не берут задачи».

---

## CLI и PowerShell

### `--params year=2024` не работает в PowerShell

PowerShell интерпретирует `=` по-своему. Используйте кавычки: `"year=2024"` или
`'{"year":2024}'` (Linux-формат).

### В консоли печатается `?[1m` вместо цвета

`lib/utils/windows_terminal.py` — поддержка Windows-консоли без VT
(legacy cmd/PowerShell). `?[1m` появляется ровно тогда, когда на хосте выключен
`ENABLE_VIRTUAL_TERMINAL_PROCESSING`: консоль получает ESC, но не
интерпретирует последовательность. Кодировка и Rich тут ни при чём — чинит
именно этот модуль.

---

## Тесты

### Тесты падают на импорте `nanobot`

`nanobot-ai==0.3.5` нужен (закреплён в `requirements.txt`). Проверьте: `pip show nanobot-ai`.
Если ниже — `pip install --upgrade 'nanobot-ai==0.3.5'`.

---

## Диагностические утилиты

| Утилита | Назначение |
|---|---|
| `python tools/diagnose_startup.py --log <PATH>` | Парсер startup-лога gateway/CLI: извлекает секции `Hooks connected` / `Registered N tools` / `Custom (project) tools` / `Runtime patches`, сверяет с каноническими списками из `lib/services/runtime_inventory.py`. Печатает OK / DRIFT / CRITICAL по хукам/project tools/runtime patches. Exit 0 (ОК), 1 (critical), 2 (drift). Опции: `--strict` (warning → exit 1), `--json` (для CI), `--no-color`. См. «Startup-inventory drift» ниже. |
| `python tools/diagnose_startup.py` (без `--log`) | Читает startup-лог из stdin — удобно для pipe: `python gateway.py --profile=prod 2>&1 \| python tools/diagnose_startup.py --no-color` |
| ~~`CacheLoadService.get_stats()`~~ — **снят** | Те же поля (`tables`, `loaded_at`, `loaded_ok`, `errors`, `missing_tables`, `rows_total`, `max_workers`) отдаёт `SnapshotLoadService.get_stats()` у capability `data`; они же лежат в payload `cache_load_done`, поэтому отдельный вызов не нужен |
| `DbLoggingService.get_stats()` | `written`, `failed`, `queued`, `queue_size`, `batch_count`, `queue_full`, `connected`, `last_error`, `question_runs`, `last_purge_*` |

## Startup-inventory drift

В startup-логе gateway/CLI `ApplicationContext` после `_log_connected_hooks()`
и `apply_all()` автоматически выводит **prominent-баннер** (`rich.Panel`,
stderr), если фактический инвентарь расходится с каноном:

```
┌─ HOOK INVENTORY: critical drift detected ──────────────────────────────┐
│ MISSING REQUIRED: SessionFileRedirectHook, RecentFilesHook              │
│ MISSING FACTORY: DatabaseLoggingHook                                    │
└─────────────────────────────────────────────────────────────────────────┘

┌─ RUNTIME PATCH INVENTORY: critical drift ──────────────────────────────┐
│ FAILED REQUIRED: subagent_logging                                       │
└─────────────────────────────────────────────────────────────────────────┘

┌─ PROJECT TOOLS INVENTORY: critical drift ──────────────────────────────┐
│ MISSING REQUIRED: document_read                                         │
│ FAILED: compact_context                                                 │
└─────────────────────────────────────────────────────────────────────────┘
```

Что означают категории:

* **MISSING REQUIRED** — обязательный хук/tool/патч не зарегистрирован. Как правило, runtime сломан (подагент не пишется в БД, файлы сессии не перенаправляются, и т.п.).
* **MISSING FACTORY** — `DatabaseLoggingHook` не зарегистрирован как per-turn factory. Tool/llm-события не попадают в `agent_gateway_logs`.
* **FAILED REQUIRED** — патч пытался примениться, но упал (изменился upstream API, ImportError, и т.п.). См. деталь в `Runtime patches:` блоке выше.
* **UNEXPECTED** — лишний хук/tool, которого нет в каноне. Может быть диагностическим (`StreamDiagnosisHook`) или следствием ручного monkey-patch.
* **MISSING OPTIONAL** — хук не критичный, но ожидался. Например, `StreamDiagnosisHook` после REMOVED-разметки.

Типовые причины MISSING REQUIRED/FAILED:

1. **`subagent_logging` failed** — `ImportError` в `RuntimePatcher.patch_subagent_logging`. Часто из-за отсутствия `_usage_to_dict` (см. CHANGELOG v2.5.3 — фикс в `lib/hooks/database_logging_hook.py`).
2. **`exec_timeout_cap`/`turn_delivery_fail` failed** — модуль shell / TurnDelivery не загружен (smoke-режим, или upstream-переименование).
3. **`session_content_cleanup` failed** — workspace не добавлен в `sys.path` (см. `gateway.py:571`).
4. **Плагин `workspace/hooks/*.py` MISSING** — `lib.cli.hook_loader._allowed_hook_names()` не знает про новый плагин, или `workspace/hooks/__init__.py` пуст.

Для автономной диагностики по уже существующему лог-файлу — `python tools/diagnose_startup.py --log gateway.log`.

См. также: [docs/ARCHITECTURE.md](ARCHITECTURE.md) — разделы по сервисам,
[docs/architecture/runtime-patcher-inventory.md](architecture/runtime-patcher-inventory.md)
— каталог monkey-patch'ей и upgrade-risk.
