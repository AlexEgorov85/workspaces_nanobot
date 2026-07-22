# PGSessionManager — хранение сессий в PostgreSQL

Замена штатному `SessionManager` (JSONL-файлы) на PostgreSQL/Greenplum.

## Зачем

Стандартный nanobot хранит сессии в `workspace/sessions/*.jsonl`. Это неудобно при:
- Нескольких репликах gateway (файлы не расшарить)
- Необходимости анализировать историю через SQL
- Большом количестве сессий (JSONL не индексирован)

PGSessionManager хранит все данные в двух таблицах и автоматически падает на JSONL при недоступности БД.

## Использование

```python
from lib.session.pg_session_manager import PGSessionManager

sm = PGSessionManager(
    workspace=config.workspace_path,
    dsn="postgresql://user:pass@localhost:5432/nanobot",
)
agent = AgentLoop.from_config(config, bus, session_manager=sm)
```

## Схема БД

### session_meta

| Колонка | Тип | Описание |
|---------|-----|----------|
| `session_key` | TEXT PK | Уникальный ключ сессии (например `user:dev`) |
| `created_at` | TIMESTAMPTZ | Дата создания |
| `updated_at` | TIMESTAMPTZ | Последнее обновление |
| `last_consolidated` | INT | Номер последней консолидации |
| `metadata` | JSONB | Произвольные метаданные (title и т.д.) |

### session_messages

| Колонка | Тип | Описание |
|---------|-----|----------|
| `id` | BIGSERIAL | Первичный ключ |
| `session_key` | TEXT | Ключ сессии (FK логический, без constraint для GP) |
| `seq` | INT | Порядковый номер сообщения |
| `role` | TEXT | `user` / `assistant` / `system` |
| `content` | TEXT | Текст сообщения |
| `msg_timestamp` | TEXT | Временная метка сообщения |
| `tool_calls` | JSONB | Вызовы инструментов |
| `reasoning_content` | TEXT | Рассуждения агента |
| `thinking_blocks` | JSONB | Блоки размышлений |
| `media` | JSONB | Медиафайлы |
| `cli_apps` | JSONB | CLI-приложения |
| `mcp_presets` | JSONB | MCP-пресеты |

Индекс: `(session_key, seq)` для быстрой загрузки.

## Создание таблиц

```bash
# PostgreSQL
psql -d nanobot -f lib/session/sql/create_session_tables.sql

# Greenplum 6.25
psql -d nanobot -f lib/session/sql/create_session_tables_gp.sql
```

Разница GP-версии: `DISTRIBUTED BY (session_key)`, отсутствие `BIGSERIAL` → `BIGSERIAL` (для seq), нет FK.

## Graceful degradation

При любой ошибке БД (отключение, таймаут, недоступность) PGSessionManager автоматически падает на JSONL-файлы через `super()`:

- `_load()` → `super()._load()` — чтение из JSONL
- `save()` → `super().save()` — запись в JSONL
- `delete_session()` → `super().delete_session()`
- `list_sessions()` → `super().list_sessions()`

Логика: `DB_RETRYABLE_ERRORS` (определены в `utils.db`) перехватываются, ошибка логируется, вызывается родительский метод.

## Методы

| Метод | Описание |
|-------|----------|
| `get_or_create(key)` | Получить сессию по ключу (из кеша или БД), создать если нет |
| `save(session)` | Сохранить сессию (UPSERT meta + batch-INSERT сообщений) |
| `delete_session(key)` | Удалить сессию (сначала messages, потом meta — для GP) |
| `list_sessions()` | Список сессий с превью первого сообщения |
| `read_session_file(key)` | Полный payload сессии (meta + все сообщения) |
| `flush_all()` | Сохранить все закешированные сессии (shutdown gateway) |

## Безопасность

`_quote()` экранирует имена схем и таблиц кавычками. `_validate_ident()` проверяет каждый сегмент: только буквы, цифры, `_` и `$`. При недопустимых символах — `ValueError`.

## Зависимости

- `psycopg2` / `psycopg2-binary`
- `utils.db` (коннектор с пулом, async/sync, retry)
