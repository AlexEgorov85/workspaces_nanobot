# Troubleshooting

Диагностический runbook для типовых ошибок. Источник — `README.md` v2.4.0
(раздел «Troubleshooting»); сюда перенесён без изменений, чтобы освободить
навигационный хаб от деталей.

> **TL;DR для диагноста:** логи — `logs/gateway.log` и `logs/cli.log`;
> статистика пула — `PgDuckDbSyncService.get_stats()`;
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
3. На Greenplum 6.25 — `gssencmode=disable` (`ConfigService` уже выставляет его
   через kwargs `connect()`, но если проблема — проверьте).
4. На PG 9.4 — минимум 3 retry, для GP — 50.

### `too many connections` (Greenplum)

`pool_max_conn = 1` в `PGSessionManager`. Если не хватает — уменьшите
`PgDuckDbSyncService.poll_interval_sec` (меньше опрос → меньше пиков).
Мониторинг: `PgDuckDbSyncService.get_stats().reconnects`.

---

## Ошибки синхронизации и кешей

### `FileNotFoundError: workspace/data_store/duckdb/cache.duckdb`

DuckDB-кеш публикуется **только gateway'ом** через `DuckDbCacheStore.publish()`
(путь вычисляется `table_registry.snapshot_path()` →
`workspace/data_store/duckdb/cache.duckdb`). Запустите `python gateway.py` и
подождите первого цикла синхронизации. Старый путь
`workspace/skills/audit_analyzer/cache/audit_cache.duckdb` из
`project.json:in_memory_cache_path` больше не используется.

### `FAISS preload: no data in cache`

Race condition: callbacks на `PgDuckDbSyncService` установлены **после** `ctx.start()`.
Уже исправлено в `gateway.py:main()` (callbacks идут до `start()`). Если столкнулись —
проверьте, что ваш код вызывает `set_on_*_callback` ДО `ctx.start()`.

---

## Бенчмарки и оценка

### `match_type: llm_judge` всегда даёт 0.5

LLM-судья — заглушка (`evaluator.py:_check_llm_judge()` возвращает 0.5).
Используйте `match_type: "keyword"` или реализуйте судью.

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

`nanobot==0.3.0` нужен (закреплён в `requirements.txt`). Проверьте: `pip show nanobot`.
Если ниже — `pip install --upgrade 'nanobot==0.3.0'`.

---

## Диагностические утилиты

| Утилита | Назначение |
|---|---|
| `python tools/check_worker_pool_integrity.py` | Проверка orphan-claims в `agent_worker_claims` (имя настраивается через `channels.postgres.claims_table`) |
| `python tools/check_worker_pool_integrity.py --fix` | Возврат задач «мёртвых» воркеров в `pending` + снятие claim |
| `PgDuckDbSyncService.get_stats()` | `polls`, `full_resyncs`, `reconnects`, `errors`, размер очереди |
| `DbLoggingService.get_stats()` | `written`, `failed`, `queue_size`, `fallback_written`, `connected`, `last_error` |

См. также: [docs/ARCHITECTURE.md](ARCHITECTURE.md) — разделы по сервисам,
[docs/architecture/runtime-patcher-inventory.md](architecture/runtime-patcher-inventory.md)
— каталог monkey-patch'ей и upgrade-risk.
