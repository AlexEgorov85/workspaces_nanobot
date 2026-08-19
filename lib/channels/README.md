# Каналы связи nanobot

Два канала для обмена сообщениями между внешними системами и агентом nanobot.

## PostgresChannel

Работает через таблицу `agent_conversation_messages` в PostgreSQL/Greenplum.

### Жизненный цикл сообщения

```
Пользователь (Streamlit)     PostgresChannel          Agent
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
| **Поллинг** | `_poll_loop` опрашивает БД каждые `poll_interval` секунд. Захват — через `_claim_one`: INSERT в `agent_worker_claims` (UNIQUE PK `task_id` — арбитр эксклюзивности) + UPDATE `processing`, в одной транзакции |
| **Параллельность** | `max_concurrent` (asyncio.Semaphore). Пока сообщение обрабатывается, другие из того же `chat_id` откладываются |
| **Reasoning** | Чанки рассуждений буферизируются и сбрасываются в `metadata.reasoning` каждые `flush_interval` секунд. Race condition исключается через `asyncio.Lock` |
| **Медиа** | Каждый файл кодируется в dict `{"filename": "<имя>", "data": "data:<mime>;base64,<...>"}` и сохраняется в `media`. При загрузке декодируется обратно в `data_store/cache/sessions/`. HTTP/HTTPS-ссылки остаются строками |
| **Аренда (пул воркеров)** | Каждая задача защищена lease (`lease_until = NOW() + processing_timeout`), heartbeat продлевает её каждые `lease_interval` сек. Мульти-машинная схема: одна задача физически не может обрабатываться двумя воркерами (UNIQUE PK `claims`) |
| **Reclaim+heal** | Истёкшие lease возвращают задачи в `pending` (или `failed` при исчерпании `max_stuck_retries`); `processing`-без-claim → `error`; висячие аренды и orphaned-placeholder чистятся. См. `_reclaim_and_heal` |
| **Ошибки** | Разведены статусы: `error` — повторяемая ошибка (повтор после `error_retry_delay`), `failed` — терминальный (не повторяется) |
| **Placeholder** | При захвате сообщения сразу создаётся assistant-запись (`status=processing`), чтобы Streamlit мог начать опрос до завершения генерации |

### Конфигурация

```json
{
    "enabled": true,
    "dsn": "postgresql://user:pass@localhost:5432/nanobot",
    "schema": "public",
    "table_name": "agent_conversation_messages",
    "claims_table": "agent_worker_claims",
    "poll_interval": 2.0,
    "flush_interval": 2.0,
    "max_concurrent": 1,
    "processing_timeout": 120,
    "max_stuck_retries": 3,
    "lease_interval": 15.0,
    "error_retry_delay": 60.0,
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
| `status` | TEXT | `pending` / `processing` / `completed` / `failed` |
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
(Postgres / Redis / Streamlit) и для чтения истории. Инкапсулирует:

- кодирование/декодирование `InboundMessage` / `OutboundMessage`;
- JSONB-кодек медиа (`lib/utils/media_jsonb.py`);
- поллинг и публикацию outbound;
- фильтрацию служебных outbound (`lib/utils/outbound_filter.py`).

`PostgresChannel` и `RedisChannel` — тонкие обёртки над `MessageExchange`;
публичный API не изменился. `streamlit_app.py` использует тот же движок для
чтения истории, поэтому поведение в Streamlit и в каналах синхронизировано.

## Как добавить новый канал

1. Создать класс, унаследовав `nanobot.channels.base.BaseChannel`.
2. Делегировать `start()` / `stop()` / `send()` / `send_delta()` в
   `MessageExchange` — иначе поведение канала разъедется с Postgres/Redis.
3. Подключить в `gateway.py` через `ChannelFactory.create_all()` (по аналогии
   с PostgresChannel/RedisChannel). Если новый канал — только читатель истории
   (как Streamlit), достаточно обёртки над `MessageExchange.poll_once(...)`.
