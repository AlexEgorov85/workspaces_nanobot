# PGSessionManager — хранение сессий в PostgreSQL

Замена штатного `SessionManager` (JSONL-файлы) на PostgreSQL/Greenplum.

## Зачем

Стандартный nanobot хранит сессии в `workspace/sessions/*.jsonl`. Это неудобно при:
- Нескольких репликах gateway (файлы не расшарить)
- Необходимости анализировать историю через SQL
- Большом количестве сессий (JSONL не индексирован)

PGSessionManager хранит все данные в двух таблицах и автоматически падает на JSONL при недоступности БД.

> **v2.0.0+:** таблицы названы `agent_session_meta` /
> `agent_session_messages` (единый `agent_`-префикс для таблиц агента).
> DDL в `sql/session/` — `create_public_agent_session_meta.sql`,
> `create_public_agent_session_messages.sql`.

## Использование

```python
from lib.session.pg_session_manager import PGSessionManager

sm = PGSessionManager(
    workspace=config.workspace_path,
    dsn="postgresql://user:pass@localhost:5432/nanobot",
)
agent = AgentLoop.from_config(config, bus, session_manager=sm)
```

Имена таблиц настраиваются через `project.json → channels.postgres`:
```json
{
    "channels": {
        "postgres": {
            "meta_table": "agent_session_meta",
            "messages_table": "agent_session_messages"
        }
    }
}
```

DSN собирается общим `utils.db.resolve_dsn()` из `channels.postgres.{host,port,
dbname,user}` + `DB_PASSWORD` (или `dsn` override), а не передаётся напрямую.

## Схема БД

### agent_session_meta

| Колонка | Тип | Описание |
|---------|-----|----------|
| `session_key` | TEXT PK | Уникальный ключ сессии (например `user:dev`) |
| `created_at` | TIMESTAMPTZ | Дата создания |
| `updated_at` | TIMESTAMPTZ | Последнее обновление |
| `last_consolidated` | INT | Номер последней консолидации |
| `metadata` | JSONB | Произвольные метаданные (title и т.д.) |

### agent_session_messages

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
| `tool_call_id` | TEXT | ID вызова инструмента |
| `name` | TEXT | Имя инструмента |
| `injected_event` | TEXT | Инжектированное событие |
| `_command` | BOOLEAN | Флаг командного сообщения |
| `_channel_delivery` | BOOLEAN | Флаг доставки через канал |
| `created_at` | TIMESTAMPTZ | Дата создания |

Индекс `(session_key, seq)` в create-скриптах не создаётся (только таблица +
COMMENT) — при необходимости подавайте его отдельно при развёртывании.

## Создание таблиц

```bash
psql -d nanobot -f sql/session/create_public_agent_session_meta.sql
psql -d nanobot -f sql/session/create_public_agent_session_messages.sql
```

Оба скрипта — Greenplum 6.5: `DISTRIBUTED BY (...)`, `pgcrypto`, без FK.

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
