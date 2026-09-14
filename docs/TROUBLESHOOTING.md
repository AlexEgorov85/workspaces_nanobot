# Troubleshooting

Диагностический runbook для типовых ошибок. Источник — `README.md` v2.4.0
(раздел «Troubleshooting»); сюда перенесён без изменений, чтобы освободить
навигационный хаб от деталей.

> **TL;DR для диагноста:** логи — в stderr (loguru, `sys.stderr`); файловый
> лог только у Streamlit — `logs/streamlit.log`; статистика пула —
> `PgDuckDbSyncService.get_stats()`;
> целостность пула воркеров — `python tools/check_worker_pool_integrity.py --fix`.

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

### JSONC в `project.json` не парсится

Только `//` и `/* */` поддерживаются. Хэштеги `#` — нет. Кавычки в DSN не должны
пересекаться с комментариями.

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
применяется к пулам воркеров и PG-сессий). Если не хватает — уменьшите
`PgDuckDbSyncService.poll_interval_sec` (меньше опрос → меньше пиков),
либо пул `channels.postgres.pool.min_conn/max_conn`.
Мониторинг: `PgDuckDbSyncService.get_stats().reconnects`.

---

## Ошибки синхронизации и кешей

### `FileNotFoundError: ~/.cache/nanobot/duckdb/cache.duckdb` (или `workspace/data_store/duckdb/cache.duckdb`, если задан `gateway.cache.use_workspace_path: true`)

DuckDB-кеш публикуется **только gateway'ом** через `DuckDbCacheStore.publish()`.
Путь определяется в `_resolve_publish_path()` (`lib/core/application_context.py`)
в порядке приоритета:

  1. `gateway.cache.local_path` (если задан) → `<это>/cache.duckdb`
  2. `gateway.cache.use_workspace_path: true` → legacy `<workspace>/data_store/duckdb/cache.duckdb`
  3. **default**: `~/.cache/nanobot/duckdb/cache.duckdb` (POSIX `fcntl` работает там штатно)

Запустите `python gateway.py` и подождите первого цикла синхронизации. Старый путь
`workspace/skills/audit_analyzer/cache/audit_cache.duckdb` из
`project.json:in_memory_cache_path` больше не используется.

### `IO Error: Could not set lock on file .../cache.duckdb.tmp: Conflicting lock is held in PID 0`

DuckDB `ATTACH ... READ_WRITE` берёт эксклюзивный `flock`, который NFS `lockd`
не отдаёт (POSIX `fcntl` vs NFS NLM — несовместимые протоколы). На свежем файле
после `rm` ошибка воспроизводится стабильно (см. эмпирическую проверку в
коммите `c522b55` и `duckdb/duckdb#4041`). Под `PID 0` в сообщении — NFS-шный
«lock без валидного владельца», а не реальный процесс.

**Решение** (с версии v2.5.2):

  * **по умолчанию** снимок уходит на `~/.cache/nanobot/duckdb/cache.duckdb` —
    POSIX `fcntl` работает там штатно, проблема исчезает без действий;
  * если хотите хранить снимок в другой локальной директории (например,
    `/var/lib/nanobot/cache/`) — задайте `gateway.cache.local_path` в `project.json`;
  * если старт выкидывает `[cache] WARNING: ... is on nfs ...` — путь попал
    на NFS через symlink или escape hatch `gateway.cache.use_workspace_path: true`;
    см. `_warn_if_publish_path_on_nfs()` в `lib/core/application_context.py`
    и уберите NFS из пути.

### `FAISS preload: no data in cache`

Race condition: callbacks на `PgDuckDbSyncService` установлены **после** `ctx.start()`.
Уже исправлено в `gateway.py:main()` (callbacks идут до `start()`). Если столкнулись —
проверьте, что ваш код вызывает `set_on_*_callback` ДО `ctx.start()`.

---

## Бенчмарки и оценка

### `match_type: llm_judge` не даёт 1.0 / «LLM judge returned no parseable JSON»

LLM-судья реализован (`benchmarks/evaluator.py:_check_llm_judge()`): запрашивает
у LLM JSON `{"score": 0.0|0.5|1.0, "reason": ...}` и нормализует на дискретную
шкалу. Проверка считается пройденной при `score >= 0.5`. При любом сбое
(нет конфига провайдера, сеть, невалидный JSON) балл — `0.0`, нейтральный
`0.5` не подставляется. Проверьте `config.json:providers.llm.api_key`.

### Файл `.yaml` в `benchmarks/items/` игнорируется

Файлы, начинающиеся с `_` (например `_template.yaml`), пропускаются загрузчиком.
Уберите `_` из имени.

---

## Streamlit UI

### `Streamlit` ждёт ответ бесконечно

С v2.0.0 streamlit-цикл не имеет таймаута: на статусе `failed` он делает re-check
5 минут, далее ждёт возврата в `processing` бесконечно. Это сделано умышленно
(обход `st.rerun maxReruns`). Если поведение не устраивает — меняйте `streamlit_app.py`.

---

## CLI и PowerShell

### `--params year=2024` не работает в PowerShell

PowerShell интерпретирует `=` по-своему. Используйте кавычки: `"year=2024"` или
`'{"year":2024}'` (Linux-формат).

---

## Тесты

### Тесты падают на импорте `nanobot`

`nanobot-ai==0.3.0` нужен (закреплён в `requirements.txt`). Проверьте: `pip show nanobot-ai`.
Если ниже — `pip install --upgrade 'nanobot-ai==0.3.0'`.

---

## Диагностические утилиты

| Утилита | Назначение |
|---|---|
| `python tools/check_worker_pool_integrity.py` | Проверка orphan-claims в `agent_worker_claims` (имя настраивается через `channels.postgres.claims_table`) |
| `python tools/check_worker_pool_integrity.py --fix` | Возврат задач «мёртвых» воркеров в `pending` + снятие claim |
| `PgDuckDbSyncService.get_stats()` | `polls`, `full_resyncs`, `reconnects`, `errors`, размер очереди |
| `DbLoggingService.get_stats()` | `written`, `failed`, `queued`, `queue_size`, `batch_count`, `queue_full`, `connected`, `last_error`, `question_runs`, `last_purge_*` |

См. также: [docs/ARCHITECTURE.md](ARCHITECTURE.md) — разделы по сервисам,
[docs/architecture/runtime-patcher-inventory.md](architecture/runtime-patcher-inventory.md)
— каталог monkey-patch'ей и upgrade-risk.
