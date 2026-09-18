# DuckDB Sync

## Назначение

DuckDB Sync обеспечивает синхронизацию данных между PostgreSQL и DuckDB для аналитических запросов и обработки больших объёмов данных, используя DuckDB как in-memory/columnar хранилище для быстрых аналитических операций.

## Ответственность

- Инициализация соединения с DuckDB
- Синхронизация данных из PostgreSQL в DuckDB
- Поддержка актуальности данных в DuckDB (incremental sync)
- Выполнение аналитических запросов через DuckDB
- Очистка временных данных DuckDB
- Управление памятью DuckDB

## Граница

### Владеет
- Соединением с DuckDB (in-memory или file-based)
- Маппингом таблиц PostgreSQL → DuckDB
- Расписанием/триггерами синхронизации
- Кэшем результатов аналитических запросов

### Не владеет
- Исходными данными в PostgreSQL (только чтение)
- Бизнес-логикой аналитики (предоставляет интерфейс запросов)
- Персистентным хранением DuckDB данных (опционально)
- Транзакционной гарантией синхронизации (best effort)

## Контракт

### Публичный интерфейс

```python
class DuckDBSync:
    def initialize(self, config: DuckDBConfig) -> None
    def sync_table(
        self,
        source_table: str,
        target_table: Optional[str] = None,
        columns: Optional[List[str]] = None,
        where_clause: Optional[str] = None
    ) -> SyncResult
    
    def sync_query(
        self,
        query: str,
        target_table: str
    ) -> SyncResult
    
    def execute_analytical_query(
        self,
        query: str,
        parameters: Optional[Dict] = None
    ) -> QueryResult
    
    def refresh_table(self, table_name: str) -> SyncResult
    def drop_table(self, table_name: str) -> None
    def get_synced_tables(self) -> List[str]
    def cleanup(self) -> None
```

### Требования

#### Требование: Инициализация DuckDB

**Сценарий: Подготовка DuckDB к работе**
- **КОГДА** система запускается
- **ТОГДА** инициализируется DuckDB с конфигурацией
- **И** создаётся соединение (in-memory или file-based)
- **И** настраиваются параметры памяти и производительности

#### Требование: Синхронизация таблицы

**Сценарий: Копирование данных из PostgreSQL**
- **КОГДА** требуется синхронизировать таблицу
- **ТОГДА** вызывается `sync_table()` с именем таблицы
- **И** данные читаются из PostgreSQL batch'ами
- **И** загружаются в DuckDB таблицу
- **И** возвращается статистика синхронизации

#### Требование: Инкрементальная синхронизация

**Сценарий: Обновление только изменённых данных**
- **КОГДА** таблица уже синхронизирована ранее
- **ТОГДА** при повторной синхронизации используется where_clause
- **И** копируются только новые/изменённые записи
- **И** существующие данные обновляются или добавляются

#### Требование: Аналитический запрос

**Сценарий: Выполнение сложного аналитического запроса**
- **КОГДА** требуется выполнить аналитический запрос
- **ТОГДА** вызывается `execute_analytical_query()` с SQL
- **И** запрос выполняется через DuckDB engine
- **И** результаты возвращаются как pandas DataFrame или dict

### Запрещённое поведение

DuckDB Sync НЕ ДОЛЖЕН:
- Модифицировать данные в PostgreSQL (только чтение)
- Синхронизировать таблицы без явного запроса
- Превышать лимиты памяти DuckDB
- Блокировать основную систему во время синхронизации
- Хранить чувствительные данные в file-based DuckDB без шифрования

## Зависимости

### Может зависеть от
- ConfigService для конфигурации подключения
- PostgreSQL connection pool для чтения данных
- Logger для логирования операций синхронизации
- CacheProvider для кэширования результатов запросов

### Не должен зависеть от
- Конкретных бизнес-таблиц (конфигурируемый маппинг)
- Внешних аналитических систем
- Direct filesystem operations (кроме DuckDB file management)

## Конфигурация

```yaml
duckdb_sync:
  enabled: true
  mode: "memory"  # memory, file
  file_path: "/tmp/nanobot_duckdb.db"
  memory_limit_mb: 1024
  threads: 4
  batch_size: 10000
  sync_tables:
    - source: "session_messages"
      target: "messages"
      columns: ["id", "session_id", "role", "content", "timestamp"]
    - source: "skill_executions"
      target: "skill_executions"
  auto_sync_enabled: false
  auto_sync_interval_sec: 300.0
  query_timeout_sec: 60.0
  cleanup_on_shutdown: true
```

## Жизненный цикл

1. **Инициализация**: Создание DuckDB соединения, настройка параметров
2. **Синхронизация**: Копирование данных из PostgreSQL по запросу или расписанию
3. **Запросы**: Выполнение аналитических запросов через DuckDB
4. **Обслуживание**: Периодическая очистка старых данных, оптимизация
5. **Завершение**: Очистка ресурсов, сохранение (если file-based)

## Состояние

```python
{
    "connection": Optional[duckdb.DuckDBPyConnection],
    "synced_tables": Dict[str, TableMapping],
    "last_sync_time": Dict[str, datetime],
    "query_cache": Dict[str, CachedResult],
    "is_initialized": bool
}
```

## Инварианты

- DuckDB инициализируется до первой операции синхронизации
- Каждая синхронизированная таблица имеет уникальный маппинг
- Лимиты памяти DuckDB не превышаются
- При shutdown все ресурсы корректно освобождаются

## Поведение при ошибке

- Ошибка подключения → исключение, DuckDB остаётся неинициализированным
- Ошибка синхронизации → логирование, частичные данные могут остаться
- Ошибка запроса → исключение с деталями ошибки DuckDB
- Превышение памяти → исключение, возможна автоматическая очистка кэша

## Потребители

- Analytics Skills — выполнение сложных аналитических запросов
- Reporting — генерация отчётов по большим данным
- Data Science — анализ данных через DuckDB
- Audit — агрегация логов и событий

## Реализация

Основная реализация:
- `lib/data/duckdb_sync.py:DuckDBSync`

Связанные компоненты:
- `lib/core/application_context.py:ApplicationContext`
- `lib/services/config_service.py:ConfigService`
- `lib/data/cache_provider.py:CacheProvider`

## Проверка

### Автоматическая проверка
- Тесты на синхронизацию таблиц
- Тесты на аналитические запросы
- Тесты на обработку ошибок
- Тесты на производительность при больших объёмах данных

### Ручная проверка
- Анализ производительности запросов
- Проверка использования памяти
- Валидация актуальности синхронизированных данных
