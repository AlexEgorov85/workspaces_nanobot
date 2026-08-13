# Скрипты миграции `auto_migrate_1.4_2.0`

Папка содержит генераторы SQL для миграции **v1.4 → v2.0**
(Greenplum 6.5 / PostgreSQL 9.4+).

Все скрипты запускаются **без параметров** из корня `.nanobot` и
генерируют один `.sql` файл рядом с собой.

| Скрипт | Генерирует |
|--------|------------|
| `generate_vector_indexes_migration.py` | `vector_indexes_migration.sql` |
| `generate_predefined_scripts_migration.py` | `predefined_scripts_migration.sql` |
| `created_tables.sql` | готовый DDL всех таблиц v2.0 (отдельный шаг) |

DDL-операции (CREATE TABLE, ADD COLUMN, CREATE INDEX, RENAME) **не**
генерируются скриптами — это ручной/отдельный шаг через `created_tables.sql`.

---

## Запуск из корня `C:\Users\Алексей\.nanobot`

```powershell
python sql\auto_migrate_1.4_2.0\generate_vector_indexes_migration.py
python sql\auto_migrate_1.4_2.0\generate_predefined_scripts_migration.py
```

Каждый скрипт напечатает путь к сгенерированному `.sql`.

---

## 1. `generate_vector_indexes_migration.py`

Перенос конфигов vector-индексов из JSON v1.4 в
`public.agent_vector_index_config` (upsert через `DO`-блок,
GP 6.5 совместимо).

Источник (первый найденный):
- `<корень>/v15_vector_indexes.json`
- `<корень>/data_store/cache/migration_v14/v15_vector_indexes.json`
- `<корень>/data_store/cache/migration_v14/vector_indexes.json`
- `<корень>/vector_indexes.json`

```powershell
psql "<DSN>" -f sql\auto_migrate_1.4_2.0\vector_indexes_migration.sql
```

Требование: таблица `public.agent_vector_index_config` создана заранее.

### Шаг 5b. Построить векторные индексы

После применения `vector_indexes_migration.sql` конфиги индексов лежат в БД,
но самих векторов (FAISS) и строк в `oarb.audit_vectors` ещё нет. Запустите
`tools/build_vectors.py` из корня проекта:

```powershell
cd C:\Users\Алексей\.nanobot

# Статус: сколько векторов уже в БД
python tools\build_vectors.py --status

# Dry-run — показать, что будет добавлено
python tools\build_vectors.py --dry-run

# Полная перестройка по всем включённым индексам (enabled=true)
python tools\build_vectors.py --full-rebuild

# Инкрементально — только новые/изменённые строки
python tools\build_vectors.py

# Только один индекс
python tools\build_vectors.py --index audits_index --full-rebuild

# С кастомным размером чанка
python tools\build_vectors.py --chunk-size 800 --chunk-overlap 150
```

`build_vectors.py`:
- читает конфиг из `public.agent_vector_index_config` (только `enabled=true`);
- использует `project.json: skills.audit_analyzer.{embedding_base_url,
  embedding_model, embedding_dimension, text_chunk_size, text_chunk_overlap}`
  для запросов к Ollama `/api/embed`;
- пишет векторы в `oarb.audit_vectors`, сериализует FAISS в
  `public.agent_vector_index_store.index_binary`.

### Что нужно сделать перед запуском `build_vectors.py`

1. Убедиться, что в `public.agent_vector_index_config` нужные индексы
   включены (`enabled = TRUE`). По умолчанию они приходят из JSON как
   `enabled=true`, но если что-то отключено — включить SQL-запросом:

   ```sql
   UPDATE public.agent_vector_index_config
   SET enabled = TRUE, updated_at = NOW()
   WHERE index_name IN ('audits_index', 'violations_index', 'audit_reports_index');
   ```

2. Убедиться, что в `project.json` указаны рабочие `embedding_base_url`,
   `embedding_model`, `embedding_dimension`. Иначе скрипт упадёт при первом
   запросе эмбеддингов.

3. Опционально — настроить параметры поиска (`top_k` / `threshold`) через
   `public.agent_vector_search_params`:

   ```sql
   INSERT INTO public.agent_vector_search_params (index_name, top_k, threshold)
   VALUES ('audits_index', 5, 0.75)
   ON CONFLICT (index_name) DO UPDATE
       SET top_k = EXCLUDED.top_k,
           threshold = EXCLUDED.threshold;
   ```

### Регулярное обновление (cron / schtasks)

`build_vectors.py` идемпотентен: сравнивает `COUNT + MAX(track_column)`
источника и `oarb.audit_vectors`, при совпадении — пропускает. Подходит
для периодического запуска:

```powershell
# Каждый час — инкрементальное обновление
schtasks /create /tn "build_vectors_hourly" `
    /tr "python C:\Users\Алексей\.nanobot\tools\build_vectors.py" `
    /sc hourly
```

Логи:
```powershell
python tools\build_vectors.py >> logs\build_vectors.log 2>&1
```

> Подробная инструкция (включая миграцию FAISS из v1.4, ручную правку
> `content_cols` и проверку после миграции) — в
> `data_store/cache/VECTOR_INDEX_MIGRATION.md`.

---

## 2. `generate_predefined_scripts_migration.py`

Перенос реестра SQL-скриптов из `scripts_registry.py` v1.4 в
`public.agent_predefined_scripts` (DELETE+INSERT, GP 6.5 совместимо).

Источник (парсится через AST, без `exec`/`import`):
`<корень>/data_store/cache/migration_v14/workspace/skills/audit_analyzer/scripts/scripts_registry.py`

```powershell
psql "<DSN>" -f sql\auto_migrate_1.4_2.0\predefined_scripts_migration.sql
```

Требование: таблица `public.agent_predefined_scripts` создана заранее.

---

## 3. `created_tables.sql`

Готовый DDL всех таблиц v2.0 для Greenplum 6.5 (UUID, `DISTRIBUTED BY`,
комментарии). Применяется как отдельный шаг **перед** миграцией данных.

```powershell
psql "<DSN>" -f sql\auto_migrate_1.4_2.0\created_tables.sql
```

Требует расширения `uuid-ossp`:
```sql
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
```

---

## Полный сценарий миграции

```powershell
# 1. Сгенерировать SQL
python sql\auto_migrate_1.4_2.0\generate_vector_indexes_migration.py
python sql\auto_migrate_1.4_2.0\generate_predefined_scripts_migration.py

# 2. Создать расширение (если ещё нет)
psql "<DSN>" -c 'CREATE EXTENSION IF NOT EXISTS "uuid-ossp";'

# 3. Создать таблицы v2.0
psql "<DSN>" -f sql\auto_migrate_1.4_2.0\created_tables.sql

# 4. Переименовать legacy-таблицы (вручную)
psql "<DSN>" -c "ALTER TABLE IF EXISTS public.session_messages RENAME TO agent_session_messages;"
# ... остальные 5 переименований

# 5. Добавить недостающие колонки (question/response/media, request_id/name) — вручную
# 6. Создать индексы — вручную

# 7. Применить миграцию данных
psql "<DSN>" -f sql\auto_migrate_1.4_2.0\vector_indexes_migration.sql
psql "<DSN>" -f sql\auto_migrate_1.4_2.0\predefined_scripts_migration.sql
```
