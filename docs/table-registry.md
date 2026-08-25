# Реестр таблиц для синхронизации PG → DuckDB

Куда прописывать таблицы, чтобы они попадали в DuckDB-кэш
(`workspace/data_store/duckdb/cache.duckdb`), и как устроен контроль синхронизации.

## Короткий ответ

**Списки таблиц хранятся в `project.json`, секция `skills.audit_analyzer`.**
Код (`lib/`) при добавлении таблицы править не нужно.

```jsonc
// project.json
"skills": {
  "audit_analyzer": {
    "db_schema": "oarb",                    // схема по умолчанию
    "db_tables": [                          // доменные таблицы (голые имена —
      "audit_reports",                      // квалифицируются db_schema)
      "audits",
      "report_items",
      "violations"
    ],
    "db_additional_tables":                 // таблицы из других схем
      [["public", "agent_predefined_scripts"]],
    "predefined_scripts_table": "public.agent_predefined_scripts",
    "mode_vector_db_table": "oarb.audit_vectors"
  }
}
```

## Цепочка: конфиг → реестр → sync

```
project.json (skills.audit_analyzer)
        │  чтение через SETTINGS
        ▼
workspace/skills/audit_analyzer/scripts/register.py
        │  table_registry.register(SkillRegistration(...))
        ▼
lib/services/table_registry.py   ← глобальный singleton table_registry
        │  ApplicationContext.start() → _auto_register_skills()
        │  (сканирует workspace/skills/*/scripts/register.py)
        ▼
lib/core/application_context.py::_make_sync_services()
        │  собирает все регистрации в единый список таблиц
        ▼
AuditSyncService (поллинг PG) ──► AuditMemoryStore ──► DuckDB-снапшот
```

Ключевой принцип: **core не знает имён навыков**. Новый skill регистрируется
собственным `scripts/register.py` без правок `lib/` (pluggable-точка входа).

## Как добавить таблицу

| Случай | Что делать |
|---|---|
| Таблица в основной схеме навыка | Добавить голое имя в `skills.audit_analyzer.db_tables` |
| Таблица из другой схемы | Добавить в `db_additional_tables`: `["schema", "table"]`, `{"schema": ..., "table": ...}` или `"schema.table"` (все формы канонизируются `normalize_table_names`) |
| Таблица эмбеддингов | Поле `mode_vector_db_table` (одно на навык; попадает и в sync, и в FAISS-пайплайн) |
| Новый навык целиком | Создать `workspace/skills/<name>/scripts/register.py` с функцией `register(table_registry)` |

После правки конфига нужен перезапуск gateway — регистрации читаются при старте.
Дубликаты (одна таблица в двух списках / у двух навыков) безопасны: список
дедуплицируется с сохранением порядка.

## Контроль изменений (track-колонки)

Инкрементальный поллинг сравнивает значение track-колонки с последней меткой:

- по умолчанию — `updated_at`;
- per-table переопределение — `SkillRegistration.track_column_overrides`
  (например, `{"oarb.audit_vectors": "id"}` — у векторной таблицы меток нет);
- lookup идёт через `table_registry.skill_for_table(table)` →
  `reg.track_column_for(table)`; для незарегистрированных таблиц fallback:
  `id` для vector_table, иначе `updated_at`.

Требование к таблице: track-колонка должна существовать и быть монотонной
(для `updated_at` — с таймзоной). Строки «строго больше» последней метки
подтягиваются при каждом поллинге.

## Параметры контроля синхронизации

Все — в `project.json`, секция `skills.audit_analyzer`:

| Параметр | Смысл |
|---|---|
| `poll_interval_sec` | Интервал инкрементального поллинга (в проекте 14400 = 4 ч) |
| `full_resync_every` | Полная перезагрузка таблиц каждые N циклов — сверка удалённых строк (0 = выкл) |
| `sync_max_queue_size` | Ёмкость очереди sync-команд |
| `reconnect_backoff_sec` / `reconnect_backoff_max_sec` | Backoff после обрыва соединения |

Наблюдаемость:

- `AuditSyncService.get_stats()` — `polls`, `full_resyncs`, `reconnects`,
  `errors`, размер очереди;
- отсутствующая таблица-источник логируется как `UndefinedTable` с подсказкой
  проверить `db_tables`/`db_additional_tables` (sync продолжается по остальным);
- отключить навык без удаления конфига: `enabled=False` в `SkillRegistration`.

## Где лежит снапшот

Единый файл для всех навыков: `workspace/data_store/duckdb/cache.duckdb`
(`TableRegistry.snapshot_path`). Запросы — через tool `duckdb_query`
(read-only SELECT); cross-skill запросы видят таблицы всех зарегистрированных
навыков.

## См. также

- `DEVELOPMENT.md` — § AuditSyncService/AuditMemoryStore (колбэки, пул БД);
- `tests/test_table_registry.py`, `tests/test_table_utils.py`,
  `tests/test_skill_auto_register.py`, `tests/test_audit_sync_service.py`.
