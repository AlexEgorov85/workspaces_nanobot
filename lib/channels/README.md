# Каналы связи nanobot

Два канала для обмена сообщениями между внешними системами и агентом nanobot.

## PostgresChannel

Работает через таблицу `agent_conversation_messages` в PostgreSQL/Greenplum.

### Жизненный цикл сообщения

```
Пользователь (CLI / Gateway)     PostgresChannel          Agent
        │                         │                     │
        │ INSERT (status=pending)  │                     │
        │────────────────────────>│                     │
        │                         │ UPDATE (processing)  │
        │                         │─────────────────────>│
        │                         │ reasoning_delta      │
        │                         │<────────────────────│
        │ (poll: reasoning)       │                     │
        │<────────────────────────│                     │
        │                         │ финальный ответ      │
        │                         │<────────────────────│
        │ (poll: completed)       │                     │
        │<────────────────────────│                     │
```

### Детали

| Механизм | Описание |
|----------|----------|
| **Поллинг** | `_poll_loop` опрашивает БД каждые `poll_interval` секунд. Захват задачи — `UPDATE ... RETURNING` в `_claim_one`: состояние захвата хранится в самой строке задачи (`status='processing'`), таблицы аренды `agent_worker_claims` нет (удалена миграцией `V006`) |
| **Параллельность** | `max_concurrent` (asyncio.Semaphore). Пока сообщение обрабатывается, другие из того же `chat_id` откладываются |
| **Reasoning** | Чанки рассуждений буферизируются и сбрасываются в `metadata.reasoning` каждые `flush_interval` секунд. Race condition исключается через `asyncio.Lock` |
| **Медиа** | Каждый файл кодируется в dict `{"filename": "<имя>", "data": "data:<mime>;base64,<...>"}` и сохраняется в `media`. При загрузке декодируется обратно в `data_store/cache/sessions/`. HTTP/HTTPS-ссылки остаются строками |
| **Эксклюзивность захвата** | Гарантирует внешний `AND status = 'pending'` в `_claim_one`: если задачу уже взял другой захват, повторный UPDATE не срабатывает, двойная обработка невозможна. `FOR UPDATE SKIP LOCKED` не используется — недоступно на Greenplum 6.5 (ядро PG 9.4), а Greenplum при `FOR UPDATE` берёт блокировку уровня таблицы |
| **Возврат в пул** | `_unstick_loop` с интервалом `unstick_interval` (по умолчанию 120 сек) возвращает `processing`-строки с `updated_at` старше `processing_timeout` в `pending` (или `failed` при исчерпании `max_stuck_retries`). Это единственный механизм возврата — таблицы аренды и lease/heartbeat больше нет. При `stop()` незавершённые задачи возвращает `_return_claimed_to_pool` |
| **Ошибки** | Разведены статусы: `error` — повторяемая ошибка, `failed` — терминальный (не повторяется). Известный дефект: ветка повтора `error` в `_claim_one` сейчас недостижима — внешний `AND status = 'pending'` её отсекает, поэтому задача после повторяемой ошибки остаётся в `error` навсегда. Настройка `error_retry_delay` сохранена как контракт `_mark_failed`; см. CHANGELOG |
| **Placeholder** | При захвате сообщения сразу создаётся assistant-запись (`status=processing`), чтобы UI мог начать опрос до завершения генерации |

### Конфигурация

```json
{
    "enabled": true,
    "dsn": "postgresql://user:pass@localhost:5432/nanobot",
    "schema": "public",
    "table_name": "agent_conversation_messages",
    "poll_interval": 2.0,
    "flush_interval": 2.0,
    "max_concurrent": 1,
    "processing_timeout": 120,
    "max_stuck_retries": 3,
    "error_retry_delay": 60.0,
    "unstick_interval": 120.0,
    "worker_id": "",
    "msg_ctx_max_size": 100,
    "media_cache_dir": "data_store/cache/sessions",
    "pool": {
        "min_conn": 1,
        "max_conn": 4,
        "pool_timeout": 5.0
    }
}
```

> С версии 2.0.0 все параметры `channels.postgres.*` управляются через `project.json`.
> В v1.x канал брал DSN напрямую из собственной секции конфига — теперь общий
> `utils.db.resolve_dsn()` собирает DSN из `channels.postgres.{host,port,
> dbname,user}` + `DB_PASSWORD` (или `dsn` override). Полный список ключей —
> в `project.json → channels.postgres` и `channels.redis`.

### DDL

Таблица `agent_conversation_messages` создаётся автоматически или вручную:

| Колонка | Тип | Описание |
|---------|-----|----------|
| `id` | UUID/BIGSERIAL | Первичный ключ |
| `chat_id` | TEXT | ID чата/диалога |
| `user_id` | TEXT | ID отправителя |
| `role` | TEXT | `user` / `assistant` |
| `content` | TEXT | Текст сообщения |
| `media` | JSONB | AW-формат `{"filename": ..., "file_id": ..., "mime_type": ..., "file_size": ...}` (с v2.3.0); старые dict `{filename, data}` и data-URL продолжают читаться через `lib/utils/media.py` (HTTP/HTTPS-ссылки — строкой) |
| `buttons` | JSONB | Массив кнопок (только assistant) |
| `metadata` | JSONB | Reasoning, retry_count, и т.д. |
| `reply_to` | UUID | Ссылка на parent-сообщение |
| `status` | TEXT | `pending` / `processing` / `completed` / `error` (повторяемая) / `failed` (терминал) |
| `created_at` | TIMESTAMPTZ | Дата создания |
| `updated_at` | TIMESTAMPTZ | Дата обновления |

Тестовые данные: `sql/channels/seed_messages.sql` (14 user + 4 assistant сообщения).

---

## RedisChannel

Работает через Redis-списки (блокирующие очереди).

### Поток сообщения

```
Внешняя система            RedisChannel              Agent
     │                         │                       │
     │ LPUSH nanobot:inbox     │                       │
     │────────────────────────>│                       │
     │                         │ BRPOP → InboundMessage│
     │                         │──────────────────────>│
     │                         │  OutboundMessage      │
     │                         │<──────────────────────│
     │ LPUSH nanobot:outbox:X  │                       │
     │<────────────────────────│                       │
```

### Формат входящего сообщения

Кладётся в `nanobot:inbox` (настраивается через `incoming_key`):

```json
{
    "sender_id": "user_42",
    "chat_id": "support_1",
    "content": "Привет!",
    "media": ["https://example.com/img.png"],
    "metadata": {"priority": 1},
    "message_id": "ext_msg_001"
}
```

### Формат ответа

Пишется в `nanobot:outbox:{chat_id}` (настраивается через `outgoing_prefix`):

```json
{
    "channel": "redis",
    "chat_id": "support_1",
    "content": "Чем могу помочь?",
    "reply_to": "ext_msg_001",
    "media": [],
    "metadata": {},
    "buttons": []
}
```

### Особенности

- Reasoning и прогресс не пишутся — только финальный ответ
- `reply_to` берётся из `message_id` последнего входящего сообщения от этого `chat_id`
- `session_key_override` позволяет привязать сессию к произвольному ключу

### Конфигурация

```json
{
    "enabled": true,
    "host": "127.0.0.1",
    "port": 6379,
    "db": 0,
    "incoming_key": "nanobot:inbox",
    "outgoing_prefix": "nanobot:outbox",
    "poll_timeout": 5.0,
    "max_concurrent": 1
}
```

---

## MessageExchange — общий движок (v2.3.0)

`lib/channels/message_exchange.py` — общий `MessageExchange` для всех каналов
(Postgres / Redis) и для чтения истории. Инкапсулирует:

- кодирование/декодирование `InboundMessage` / `OutboundMessage`;
- JSONB-кодек медиа (`workspace/utils/media.py`);
- поллинг и публикацию outbound;
- фильтрацию служебных outbound (`lib/utils/outbound_meta.py`).

`PostgresChannel` и `RedisChannel` — тонкие обёртки над `MessageExchange`;
публичный API не изменился. Любая другая поверхность (например, web-UI) может
читать историю через тот же движок — тогда поведение UI и каналов
синхронизировано по построению. Отдельного потребителя-приложения больше нет:
`streamlit_app.py` удалён в фазе 1 миграции `enterprise-mcp-platform`.

## Как добавить новый канал

1. Создать класс, унаследовав `nanobot.channels.base.BaseChannel`.
2. Делегировать `start()` / `stop()` / `send()` / `send_delta()` в
   `MessageExchange` — иначе поведение канала разъедется с Postgres/Redis.
3. Подключить в `gateway.py` через `ChannelFactory.create_all()` (по аналогии
   с PostgresChannel/RedisChannel). Если новый канал — только читатель истории
   (как была web-поверхность), достаточно обёртки над
   `MessageExchange.poll_once(...)`.
