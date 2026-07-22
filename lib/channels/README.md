# Каналы связи nanobot

Два канала для обмена сообщениями между внешними системами и агентом nanobot.

## PostgresChannel

Работает через таблицу `conversation_messages` в PostgreSQL/Greenplum.

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
| **Поллинг** | `_poll_loop` опрашивает БД каждые `poll_interval` секунд. Берёт самое старое `pending`-сообщение через `UPDATE ... RETURNING` (атомарный захват) |
| **Параллельность** | `max_concurrent` (asyncio.Semaphore). Пока сообщение обрабатывается, другие из того же `chat_id` откладываются |
| **Reasoning** | Чанки рассуждений буферизируются и сбрасываются в `metadata.reasoning` каждые `flush_interval` секунд. Race condition исключается через `asyncio.Lock` |
| **Медиа** | Локальные файлы кодируются в `data:<mime>;base64,<...>` для хранения в БД; при загрузке декодируются обратно в `data_store/cache/sessions/` |
| **Unstick** | Сообщения, зависшие в `processing` дольше `processing_timeout`, возвращаются в `pending` (до 3 retries), затем — `failed` |
| **Placeholder** | При захвате сообщения сразу создаётся assistant-запись (`status=processing`), чтобы Streamlit мог начать опрос до завершения генерации |

### Конфигурация

```json
{
    "enabled": true,
    "dsn": "postgresql://user:pass@localhost:5432/nanobot",
    "schema": "public",
    "table_name": "conversation_messages",
    "poll_interval": 2.0,
    "flush_interval": 2.0,
    "max_concurrent": 1,
    "processing_timeout": 600
}
```

### DDL

Таблица `conversation_messages` создаётся автоматически или вручную:

| Колонка | Тип | Описание |
|---------|-----|----------|
| `id` | UUID/BIGSERIAL | Первичный ключ |
| `chat_id` | TEXT | ID чата/диалога |
| `user_id` | TEXT | ID отправителя |
| `role` | TEXT | `user` / `assistant` |
| `content` | TEXT | Текст сообщения |
| `media` | JSONB | Массив data URL / ссылок |
| `buttons` | JSONB | Массив кнопок (только assistant) |
| `metadata` | JSONB | Reasoning, retry_count, и т.д. |
| `reply_to` | UUID | Ссылка на parent-сообщение |
| `status` | TEXT | `pending` / `processing` / `completed` / `failed` |
| `created_at` | TIMESTAMPTZ | Дата создания |
| `updated_at` | TIMESTAMPTZ | Дата обновления |

Тестовые данные: `sql/seed_messages.sql` (14 user + 3 assistant сообщения).

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

## Как добавить новый канал

1. Создать класс, унаследовав `nanobot.channels.base.BaseChannel`
2. Реализовать `start()`, `stop()`, `send()`, `send_delta()`
3. Подключить в `gateway.py` по аналогии с PostgresChannel/RedisChannel
