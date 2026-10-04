# Хранение сессий — `lib/session/`

> **Класса `PGSessionManager` в проекте нет.** Он существовал как подкласс
> `SessionManager`, но был снят: PostgreSQL перестал быть основным хранилищем
> сессий. Ниже — что осталось и как устроено сейчас.

## Что здесь есть

Модуль `pg_session_manager.py` экспортирует три вещи
(`lib/session/pg_session_manager.py:109`):

| Символ | Роль |
|--------|------|
| `SanitizingSessionStore` | `SessionStore` upstream'а + санитизация NUL на границе записи |
| `build_session_manager(workspace)` | собирает upstream `SessionManager` поверх этого стора |
| `clean_session_content` | санитизация одного сообщения |

Имя файла осталось историческим.

## Модель хранения

**Hot path — JSONL. PostgreSQL — только cold-storage mirror.**

Менеджер сессий — всегда класс библиотеки `SessionManager`. Своё поведение
агент добавляет не подклассом, а слоем `SessionStore`
(`SanitizingSessionStore`), а PostgreSQL обслуживает отдельный фоновый
`SessionMirror` (`lib/gateway/mirror/`).

`storage="postgres"` в конфигурации означает **«холодное зеркало включено»**, а
не «сессии хранятся в PostgreSQL»: сам `SessionManager` про эти таблицы не
знает, и в нём их имена не встречаются (`lib/services/session_storage.py:203`).
Имена таблиц уходят в `SessionMirror`, а их отсутствие — ошибка
конфигурации, которую фабрика называет сразу, а не роняет позже на старте
синка.

## Использование

Прямое создание менеджера — не точка входа рантайма. Им пользуется
`SessionStorageService.create()` (`lib/services/session_storage.py:112`),
которая и выбирает режим, и поднимает общий пул. Режимы:

| `storage` | Что делает |
|-----------|-----------|
| `auto` | `postgres` (зеркало), если задан DSN, иначе `file` |
| `postgres` | зеркало обязательно; без DSN — `SessionStorageError` |
| `file` | только JSONL, PostgreSQL не трогается |

Низкоуровневая сборка выглядит так:

```python
from lib.session.pg_session_manager import build_session_manager

manager = build_session_manager(workspace)   # upstream SessionManager + SanitizingSessionStore
```

## Почему `SanitizingSessionStore` наследует `JsonlSessionStore`

`SessionManager.save_runtime_checkpoint` (строка 1794 в upstream) ускоряет
оборот только при `self._store is self._jsonl_store`. Обёртка вокруг стора
молча деградировала бы до полной перезаписи транскрипта на каждом чекпойнте —
то есть наследование здесь не стилистический выбор, а условие сохранения
скорости горячего пути.

## Санитизация

`SanitizingSessionStore.save()` сперва прогоняет контент всех сообщений через
`clean_text` (`workspace/utils/clean_text.py`). Чистятся две формы одного и
того же невалидного символа:

- настоящий NUL-байт (`0x00`) — PostgreSQL не принимает его в `text`-литералах
  (`A string literal cannot contain NUL (0x00) characters.`);
- литеральные escape-последовательности `backslash-u-0000` .. `backslash-u-0003`
  — psycopg2 трактует их как управляющие символы и падает с
  `UntranslatableCharacter`.

Попасть они могут из бинарного вывода инструментов (`exec`/`read_file`) или из
LLM-вывода. Причина не в JSONL, а в PostgreSQL, поэтому чистить надо на
границе записи, рядом с потребителем.

Раньше эту чистку делал патч `Session.add_message`; теперь санитизация стоит в
сторе, и отдельный патч не нужен. Вторая точка применения — страховка на
границе БД, `_sanitize_param` в `utils/db`.

## Имена таблиц

Задаются конфигурацией, авто-дефолтов в коде нет:

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

Источник — `config.json` (не `project.json`: такого файла в проекте нет).

> **v2.0.0+:** таблицы названы `agent_session_meta` / `agent_session_messages`
> (единый `agent_`-префикс). DDL в `sql/session/` —
> `create_public_agent_session_meta.sql`,
> `create_public_agent_session_messages.sql`.

DSN собирается общим `utils.db.resolve_dsn()` из `channels.postgres.{host,port,
dbname,user}` + `DB_PASSWORD` (или `dsn` override), а не передаётся напрямую.

## Схема БД

### agent_session_meta

| Колонка | Тип | Описание |
|---------|-----|----------|
| `replica_id` | TEXT PK | Владелец строки; вторая половина составного ключа |
| `session_key` | TEXT PK | Ключ сессии (например `user:dev`) |
| `created_at` | TIMESTAMPTZ | Дата создания |
| `updated_at` | TIMESTAMPTZ | Последнее обновление. **Поле разрешения конфликта, не признак изменения** |
| `last_consolidated` | INT | Номер последней консолидации |
| `metadata` | JSONB | Произвольные метаданные (title и т.д.) |
| `source_digest` | TEXT | SHA-256 файла сессии. **Признак изменения** — сравнение с `updated_at` замирало, потому что upstream не поднимает метку при правке `metadata` |
| `missing_cycles` | INT | Сколько циклов подряд сессии не было в списке upstream; удаление по порогу, не по первому пропуску |
| `message_count` | INT | Сколько строк сообщений зеркала принадлежит сессии |
| `synced_at` | TIMESTAMPTZ | Момент последней записи строки зеркала |

Ключ — составной `(replica_id, session_key)`, а не `session_key`. Одна и та
же сессия на двух репликах — это две строки, а не одна под общим
last-write-wins.

### agent_session_messages

| Колонка | Тип | Описание |
|---------|-----|----------|
| `id` | BIGSERIAL NOT NULL | Суррогатный номер строки, **ключом не является**. Нужен для разбора неустойчивых позиций: `seq` меняет смысл при сдвиге нумерации |
| `replica_id` | TEXT PK | Владелец строки |
| `session_key` | TEXT PK | Ключ сессии (FK логический, без constraint для GP) |
| `seq` | INT PK | Порядковый номер сообщения |
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

Ключ таблицы — составной `(replica_id, session_key, seq)`, и он **единственный**.
Greenplum 6 допускает на хеш-распределённой таблице ровно один
`UNIQUE`/`PRIMARY KEY`, поэтому прежняя пара «`PRIMARY KEY (id)` плюс
`UNIQUE (replica_id, session_key, seq)`» делала таблицу не создаваемой вовсе.
Уникальность по составному ключу держит писатель: `mirror_session` удаляет
все сообщения сессии и вставляет заново с `seq = 0…N-1`, поэтому дубль на
одну позицию не может возникнуть в принципе.

Распределение сообщений — по `(replica_id, session_key)`, то есть
подмножество ключа и ровно как у `agent_session_meta`. Сообщения сессии
оказываются на том же сегменте, что и её метаданные, поэтому восстановление
сессии не собирает данные со всего кластера.

Индексы сверх первичного ключа создаёт `V011`
(`agent_session_messages_replica_session_seq_idx`); на новой установке он
избыточен, потому что те же столбцы уже под ключом.

## Создание таблиц

```bash
psql -d nanobot -f sql/session/create_public_agent_session_meta.sql
psql -d nanobot -f sql/session/create_public_agent_session_messages.sql
```

Оба скрипта рассчитаны на Greenplum 6.5 (ядро PostgreSQL 9.4) и одновременно
применяются к PostgreSQL 13.22 (тестовый контур): клауза распределения
вынесена в ограждённый шаг `SET DISTRIBUTED BY`, а не стоит в теле
`CREATE TABLE`, где она была бы синтаксической ошибкой на PostgreSQL.
`pgcrypto` — для UUID, FK не объявляется (GP 6.5 не поддерживает
referential integrity; каскад и так выполняет писатель).

## Архитектурный инвариант

Ни один runtime-модуль вне `SessionMirror` **не пишет** в
`agent_session_meta` / `agent_session_messages` напрямую. Проверяется
`tests/test_storage_hybridization.py::TestNoDirectSQLToSessionTables`.

Раньше это обеспечивал `PGSessionManager` перехватом `DB_RETRYABLE_ERRORS` и
переходом в `super()` (graceful degradation «на JSONL»). Теперь переход
не нужен вовсе: горячий путь и есть JSONL, а падение PostgreSQL означает лишь
отставание холодного зеркала, а не потерю сессий.

## Зависимости

- `psycopg2` / `psycopg2-binary` — только для зеркала
- `utils.db` (пул, retry) — общий с остальными подсистемами
