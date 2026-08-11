# Векторные индексы — разработчик

## Архитектура

```
source table           build_vectors.py                audit_vectors              vector_index_store
(oarb.audits)  ──►  read → embed → insert       ──►  REAL[] + row_data     ──►  BYTEA (serialized FAISS)
(oarb.violations)                                                               
                                                          │
                                                          ▼
                                                   vector_mode.py
                                                   (search: deserialize
                                                    from store, cache
                                                    in memory)
```

Три таблицы в `oarb.`:

| Таблица | Назначение |
|---------|-----------|
| `audit_vectors` | Сырые векторы (REAL[]) + метаданные + row_data. Строится `build_vectors.py` |
| `vector_index_store` | Сериализованный FAISS-индекс (BYTEA). Перестраивается при каждом `build_vectors.py` с изменениями |
| `vector_index_config` | Конфигурация индексов (альтернатива config.json). Опциональна |

## `build_vectors.py` — управление индексами

### Режимы запуска

| Флаг | Действие |
|------|----------|
| *(без флагов)* | Инкрементальная синхронизация: только новые/изменённые/удалённые строки |
| `--full-rebuild` | Полная перестройка: очистить индекс и пересобрать все строки |
| `--check` | Быстрая проверка сигнатуры (COUNT + MAX track_column). Если данные не менялись — выход без синхронизации |
| `--status` | Показать состояние всех индексов без синхронизации |
| `--dry-run` | Показать что будет сделано, без вставки в БД |
| `--index audits_index` | Собрать только один индекс |

### Примеры

```bash
# Стартовая сборка (первичная)
python build_vectors.py

# Только проверить состояние
python build_vectors.py --status

# Быстрая проверка при старте контейнера
python build_vectors.py --check

# Принудительная перестройка
python build_vectors.py --full-rebuild

# Репетиция: посмотреть что изменится
python build_vectors.py --dry-run

# Собрать только один индекс
python build_vectors.py --index violations_index
```

### Алгоритм работы

1. Читает конфиг: `oarb.vector_index_config` (если есть данные) → `project.json` (`skills.audit_analyzer.vector_indexes`)
2. Для каждого активного (`enabled: true`) индекса:
   - Загружает все строки из source-таблицы
   - Сравнивает с существующими записями в `audit_vectors` по `(source, pk_value)`
   - Классифицирует на три группы:
     - **NEW** — строки, которых нет в `audit_vectors` → INSERT
     - **CHANGED** — строки, у которых изменился `content_hash` (MD5 от search_text) → DELETE + INSERT
     - **DELETED** — строки, которые есть в `audit_vectors`, но удалены из источника → DELETE
   - Отправляет новые/изменённые тексты в Ollama `/api/embed` батчами
   - Если текст длиннее `--chunk-size` символов — автоматически разбивает на чанки
   - Вставляет векторы в `audit_vectors`
   - Перестраивает FAISS-индекс и сохраняет в `vector_index_store` (BYTEA)

## `--status` — что означают цифры

```
> python build_vectors.py --status

  audits_index
    векторов: 10              # записей в audit_vectors для этого source
    размерность: 1024          # размер эмбеддинга (должен совпадать с моделью)
    строк в источнике: 10      # COUNT(*) FROM source_table
    последняя синхр.: 2026-06-29 15:57:51+03
```

Если `векторов < строк в источнике` — не все строки проиндексированы. Запустить `build_vectors.py` (без флагов).

## Как добавить новый индекс

### Через project.json (fallback)

Добавить секцию `skills.audit_analyzer.vector_indexes` в `project.json`:

```json
"vector_indexes": {
    "new_index_name": {
        "table": "oarb.new_table",
        "pk": "id",
        "source_table": "new_table",
        "content_columns": ["title"],
        "embedding_columns": ["title", "description"],
        "track_column": "updated_at",
        "enabled": true
    }
}
```

Поля:

| Поле | Описание |
|------|----------|
| `table` | Полное имя исходной таблицы (schema.table) |
| `pk` | Первичный ключ |
| `source_table` | Короткое имя для колонки `source` в `audit_vectors` |
| `content_columns` | Колонки для поля `content` (отображается в результатах) |
| `embedding_columns` | Колонки для эмбеддинга. Если текст длиннее `chunk_size` — разбивается на чанки |
| `track_column` | Колонка для сигнатуры в `--check` |
| `enabled` | `true` — активен, `false` — пропускается |

### Через таблицу БД

```sql
INSERT INTO oarb.vector_index_config
    (index_name, source_table, src_table, pk_column,
     content_cols, embedding_cols, track_column, enabled)
VALUES (
    'new_index_name',
    'new_table',
    'oarb.new_table',
    'id',
    ARRAY['title'],
    '["title", "description"]'::jsonb,
    'updated_at',
    true
);
```

Приоритет: если в таблице есть данные — используется она, иначе `project.json` (`skills.audit_analyzer.vector_indexes`).

### Запустить сборку

```bash
python build_vectors.py --index new_index_name
```

## Чанкование длинных текстов

Включается **автоматически** — не нужно явно помечать колонки.

- Если любая из `embedding_columns` длиннее `--chunk-size` (по умолч. 500 символов) — текст разбивается на чанки
- Перекрытие соседних чанков: `--chunk-overlap` (по умолч. 80 символов)
- `search_text` каждого чанка содержит только свою часть длинной колонки + все короткие колонки целиком
- `content` (отображаемый) всегда полный, собирается из `content_columns`
- `row_data` (JSONB) содержит всю исходную строку целиком, повторяется в каждом чанке
- Дублирование: при поиске из нескольких чанков одного документа возвращается только один (с наибольшим score), `matched_chunks` показывает сколько чанков совпало

```bash
# Кастомные параметры чанкования
python build_vectors.py --chunk-size 800 --chunk-overlap 150
```

## Как работает store (BYTEA)

После синхронизации `build_vectors.py`:
1. Читает все векторы для `source` из `audit_vectors`
2. Строит FAISS `IndexFlatIP` в памяти
3. Сериализует через `faiss.serialize_index()` → `bytes`
4. UPSERT в `oarb.vector_index_store`

`vector_mode.py` при поиске:
1. Проверяет `_INDEX_CACHE` (in-memory, живёт до перезапуска процесса)
2. Если промах — читает `vector_index_store` → `faiss.deserialize_index()`
3. Если store пуст — перестраивает из `audit_vectors` и сохраняет в store

На 100 векторов store занимает ~400 КБ. На 100 000 — ~400 МБ.
Максимум BYTEA — 1 ГБ.

При перезапуске агента **in-memory кеш сбрасывается**. Индекс загружается из store заново (десериализация, ~1-2 сек на 100K векторов).

## Когда вызывать `build_vectors.py`

| Сценарий | Команда |
|----------|---------|
| Первичная настройка | `python build_vectors.py --full-rebuild` |
| По расписанию (cron) | `python build_vectors.py --check` |
| После вставки новых строк в источнике | `python build_vectors.py` |
| После массового обновления | `python build_vectors.py --full-rebuild` |
| После удаления строк из источника | `python build_vectors.py` |
| После изменений в `vector_index_config`/`project.json` | `python build_vectors.py --full-rebuild` |

## Как удалить индекс

```bash
# 1. Отключить в конфиге
# project.json (skills.audit_analyzer.vector_indexes): "enabled": false
# или UPDATE oarb.vector_index_config SET enabled = false WHERE index_name = '...'

# 2. Очистить данные
python -c "
from utils.db import execute
execute('DELETE FROM oarb.audit_vectors WHERE source = %s', 'index_name')
execute('DELETE FROM oarb.vector_index_store WHERE source = %s', 'index_name')
"
```

## Как вручную сбросить кеш (без перезапуска)

```python
from vector_mode import invalidate_cache
invalidate_cache('audits_index')           # только один
invalidate_cache()                         # все индексы
```

## Диагностика

```bash
# Проверить что в vector_index_store
python -c "
from utils.db import fetch
rows = fetch('SELECT source, dimension, vector_count, length(index_binary) AS size_bytes, updated_at FROM oarb.vector_index_store')
for r in rows: print(r)
"

# Проверить что в audit_vectors
python -c "
from utils.db import fetch
rows = fetch('SELECT source, COUNT(*) AS cnt, MIN(chunk_index) AS min_ch, MAX(chunk_index) AS max_ch FROM oarb.audit_vectors GROUP BY source')
for r in rows: print(r)
"
```

## Таблицы БД

### oarb.audit_vectors

| Колонка | Тип | Описание |
|---------|-----|----------|
| `id` | SERIAL | Первичный ключ |
| `source` | TEXT | Имя индекса (audits_index, violations_index, ...) |
| `content` | TEXT | Текст для отображения в результатах |
| `search_text` | TEXT | Текст по которому строился эмбеддинг |
| `table` | TEXT | Исходная таблица (короткое имя) |
| `pk_value` | INTEGER | Первичный ключ исходной строки |
| `chunk_index` | INT | Номер чанка (0-based) |
| `chunk_count` | INT | Всего чанков в документе |
| `row_data` | JSONB | Полная строка исходной таблицы |
| `embedding` | REAL[] | Вектор (1024 float) |
| `content_hash` | TEXT | MD5 от search_text |
| `max_src_track` | TEXT | MAX(track_column) на момент синхронизации |
| `synced_at` | TIMESTAMPTZ | Время последней синхронизации |
| `created_at` | TIMESTAMPTZ | Время создания записи |

### oarb.vector_index_store

| Колонка | Тип | Описание |
|---------|-----|----------|
| `source` | TEXT PRIMARY KEY | Имя индекса |
| `index_binary` | BYTEA | Сериализованный FAISS-индекс |
| `metadata` | JSONB | Метаданные (source, table, row_data для каждого вектора) |
| `dimension` | INT | Размерность эмбеддинга (1024) |
| `vector_count` | INT | Количество векторов в индексе |
| `updated_at` | TIMESTAMPTZ | Время последнего обновления |
