# Data Cache (Кеш данных)

## Назначение

Определение контракта подсистемы кеша: источник истины, lifecycle snapshot'ов, публикация, атомарность и контракт потребителя. Кеш — это локальный DuckDB-based слой быстрого доступа для read-mostly данных, синхронизируемых из PostgreSQL.

## Ответственность

Cache отвечает за:
- определение источника истины (PostgreSQL)
- управление lifecycle snapshot'ов DuckDB
- предоставление единого consumer contract через CacheProvider
- обеспечение атомарности операций кеша

## Граница

### Владеет
- синхронизацией данных из PostgreSQL в локальный DuckDB
- управлением snapshot lifecycle (создание, публикация, атомарный swap)
- предоставлением API для query_cached_data через CacheProvider

### Не владеет
- бизнес-логикой Skills
- прямым доступом к DuckDB файлу извне CacheProvider
- хранением NFS-mounted файлов (только local ext4)

### Может зависеть от
- PostgreSQL (источник истины)
- локальной файловой системы ext4
- конфигурации gateway.cache.local_path

### Не должен зависеть от
- конкретной реализации Skills
- NFS storage
- других cache store implementations

## Публичный контракт

CacheProvider предоставляет:
- `query_sql` — выполнение SQL query на cached data
- `get_schema` — получение схемы cached таблиц
- `explain` — объяснение плана выполнения
- `search_vector` — векторный поиск (если применимо)

## Требования

### Требование: PostgreSQL — источник истины

Система ДОЛЖНА рассматривать PostgreSQL как источник истины для кешированных таблиц.

#### Сценарий: Cache desync от PostgreSQL

- **КОГДА** кешированная строка не согласуется с PostgreSQL
- **ТОГДА** PostgreSQL ДОЛЖЕН выиграть при следующей синхронизации; кеш НЕ ДОЛЖЕН молча сохранять stale data бессрочно

### Требование: Локальное ext4 хранилище

Система ДОЛЖНА сохранять DuckDB cache файл по пути `gateway.cache.local_path` (локальный ext4 путь, НЕ NFS путь).

#### Сценарий: Попытка NFS storage

- **КОГДА** `gateway.cache.local_path` разрешается в NFS mount
- **ТОГДА** cache provider ДОЛЖЕН fail fast при старте с явной ошибкой, а не молча corrupt состояние

### Требование: Единый consumer contract

Система ДОЛЖNA предоставлять операции кеша исключительно через `CacheProvider` (`query_sql`, `get_schema`, `explain`, `search_vector`).

#### Сценарий: Skill query к кешу

- **КОГДА** Skill нуждается в query cached таблицы
- **ТОГДА** он ДОЛЖЕН использовать `CacheProvider` и НЕ ДОЛЖЕН открывать DuckDB файл напрямую

## Запрещённое поведение

Система НЕ ДОЛЖНА:

- писать DuckDB cache файл напрямую на NFS mount (эмпирически fails with `PID 0` locking errors)
- создавать вторую cache store implementation рядом с `cache_provider.py`
- молча fallback на PostgreSQL когда кеш invalid; потребители ДОЛЖНЫ быть уведомлены
- bypass cache provider из Skill кода
- дублировать cache state вне единственного cache file path

## Зависимости

- `docs/TARGET_ARCHITECTURE.md` — глобальные архитектурные принципы
- `lib/services/cache_provider.py:CacheProvider` — реализация
- PostgreSQL — источник истины
- DuckDB — embedded DB engine

## Конфигурация

```json
{
  "gateway": {
    "cache": {
      "local_path": "/path/to/local/ext4/cache.duckdb",
      "sync_tables": ["table1", "table2"]
    }
  }
}
```

## Жизненный цикл

1. **Инициализация**: CacheProvider создаётся при старте ApplicationContext
2. **Синхронизация**: данные копируются из PostgreSQL в DuckDB
3. **Публикация**: новый snapshot атомарно заменяет старый
4. **Query**: потребители читают данные через CacheProvider API
5. **Обновление**: периодическая resync по расписанию или событию

## Состояние

CacheProvider хранит:
- путь к DuckDB файлу
- список sync таблиц
- статус последней синхронизации

## Инварианты

- PostgreSQL всегда является источником истины
- DuckDB файл находится только на local ext4
- Все операции кеша идут через CacheProvider
- Нет второго cache store

## Поведение при ошибке

- NFS mount detected → fail fast с явной ошибкой
- Sync failure → кеш остаётся со старыми данными, потребители уведомлены
- Query error → ошибка возвращается потребителю, нет silent fallback

## Потребители

- Skills — query cached данных
- AuditAnalyzer — анализ паттернов из кеша
- LegalSummarizer — агрегация данных

## Реализация

Основная реализация:
- `lib/services/cache_provider.py:CacheProvider`

Связанные компоненты:
- `lib/data/duckdb_sync.py:DuckDBSync`
- `lib/core/application_context.py:ApplicationContext`

## Проверка

Валидация включает:
1. Проверка отсутствия прямого доступа к DuckDB файлу (code review)
2. Проверка fail fast на NFS mount (тесты)
3. Проверка атомарности snapshot swap (тесты)
