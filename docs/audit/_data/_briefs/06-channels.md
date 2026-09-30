# Work brief `06-channels`

Product files: **5**, LOC: **2819**

## `lib/channels/postgres_channel.py` — 2190 LOC (code 1747)
- module: `lib.channels.postgres_channel`
- docstring: PostgreSQL / Greenplum канал — связывает БД веб-сервера с nanobot-агентом. Канал опрашивает таблицу ``agent_conversation_messages``, забирает входящие сообщения от пользователей (status='pending'), отправляет их агенту, 
- static importers (8): `lib/services/channel_factory.py`, `tests/integration/test_postgres_channel_lifecycle_stress.py`, `tests/integration/test_worker_pool_concurrency.py`, `tests/test_gateway_live_media_e2e.py`, `tests/test_postgres_channel.py`, `tests/test_smoke_postgres_channel_media.py`, `tests/test_user_stop_signal.py`, `tests/test_user_stop_signal_priority.py`
- string/dynamic refs: 9
- test files touching it: — none —
- classes: 1, module functions: 1

### class `PostgresChannel` — lines 83-2190 (2108 LOC), 42 methods
- bases: BaseChannel
- decorators: —
- docstring: — NONE —
- name referenced in 12 file(s); tests: 11

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 108-240 | `(self, config: dict, bus: MessageBus, *, db_logging_service: Any | None=None, compaction_e` | 7 | 0 | 6 | — |
| `_embed_media_for_db` | 246-252 | `(self, media: list[str]) -> list[Any]` | 1 | 2 | 5 | Прочитать локальные файлы и закодировать для БД (AW-формат). Делегирует общему ``utils.media.serialize`` — еди |
| `_decode_media_from_db` | 254-264 | `(self, media: list[Any], session_key: str='default') -> list[Any]` | 1 | 2 | 2 | Декодировать storage-медиа обратно в локальные файлы сессии. Делегирует общему ``utils.media.deserialize`` — т |
| `_resolve_media_paths_and_hints` | 267-272 | `(media: list[Any]) -> tuple[list[str], list[str]]` | 1 | 2 | 2 | Из декодированных media (строки-пути или dict filename/path) извлечь пути для агента и подсказки «файл лежит т |
| `file_store` | 279-281 | `(self) -> SessionFileStore` | 1 | 0 | 0 | Хранилище вложений для ``MessageExchange`` (кодек media). |
| `start` | 283-305 | `(self) -> None` | 2 | 0 | 21 | Запустить циклы опроса БД, продления аренды и сброса рассуждений. |
| `stop` | 307-327 | `(self) -> None` | 7 | 0 | 16 | Остановить все циклы, освободить аренды и сбросить рассуждения. |
| `_lease_loop` | 335-377 | `(self) -> None` | 6 | 1 | 2 | Фоновая задача: периодически продлевает аренды и возвращает задачи с истёкшими арендами обратно в пул. Каждые  |
| `_reclaim_needed` | 379-404 | `(self) -> bool` | 3 | 1 | 2 | Быстрый гейт перед тяжёлым reclaim: есть ли вообще работа. Возвращает False, если в таблице нет ни одной ``pro |
| `_reclaim_and_heal` | 406-512 | `(self) -> None` | 7 | 1 | 4 | Вернуть задачи с истёкшими арендами и вылечить рассинхроны claims. Инвариант: ``processing ⇔ claim``. Нарушени |
| `_delete_claim` | 514-531 | `(self, conn: Any | None, task_id: str) -> None` | 3 | 9 | 4 | Удалить claim задачи из ``agent_worker_claims``. В single-режиме (``claim_strategy == "single"``) — no-op: таб |
| `_release_all_leases` | 533-555 | `(self) -> None` | 3 | 1 | 2 | Освободить все аренды этого воркера при остановке. Задачи возвращаются в ``pending`` (если всё ещё ``processin |
| `_activity_print` | 566-573 | `(self, line: str) -> None` | 2 | 7 | 3 | Напечатать строку активности воркера, если флаг включён. |
| `_lifecycle_log` | 575-603 | `(self, phase: str, user_msg_id: str | None, *, chat_id: str | None=None, assistant_msg_id:` | 8 | 10 | 1 | Одна строка lifecycle-лога на каждую фазу задачи. Формат: ``TASK lifecycle task=<id> phase=<phase> chat=<id> a |
| `_journal_event` | 605-641 | `(self, event_type: str, summary: str, payload: dict[str, Any] | None=None, *, level: str='` | 5 | 4 | 1 | Долговечно записать ошибку канала в ``agent_gateway_logs``. Дублирует loguru-строку из циклов опроса БД (``pol |
| `_preview` | 644-649 | `(content: Any, limit: int=60) -> str` | 4 | 2 | 1 | Короткий однострочный превью контента задачи для лога. |
| `_report_queue` | 651-676 | `(self) -> None` | 10 | 2 | 2 | Одноразовая (по изменению) печать размера очереди задач. Считает по ``agent_conversation_messages`` число ожид |
| `_flush_reasoning_loop` | 682-694 | `(self) -> None` | 4 | 1 | 2 | Фоновая задача: каждые ``_flush_interval`` секунд сбрасывает накопленные буферы рассуждений в ``metadata.reaso |
| `_flush_reasoning` | 696-727 | `(self) -> None` | 7 | 2 | 1 | Сбросить все грязные буферы рассуждений в БД одной пачкой. Атомарность гарантируется ``_reasoning_io_lock``: п |
| `_flush_live_context` | 729-768 | `(self) -> None` | 10 | 1 | 2 | Живое обновление занятости контекста в processing-строки. Каждые ``_flush_interval`` секунд читает блок ``cont |
| `poll_priority_inbound` | 774-808 | `(self, exchange: MessageExchange) -> bool` | 3 | 0 | 1 | Priority polling path: забрать priority-кандидат из БД. Семантика: * независим от состояния обычных слотов (`` |
| `_poll_priority_once` | 810-906 | `(self, exchange: MessageExchange) -> bool` | 13 | 1 | 2 | Реализация priority claim + dispatch. Шаги: 1. ``_claim_one(priority_contents=...)`` — атомарный claim (та же  |
| `poll_inbound` | 908-936 | `(self, exchange: MessageExchange) -> bool` | 4 | 0 | 2 | Хук транспорта для ``MessageExchange``: берет новое сообщение из БД. Откат зависших ``processing``: * single-р |
| `_unstick_processing` | 938-1023 | `(self) -> list[str]` | 4 | 1 | 4 | Освободить сообщения, зависшие в ``processing`` дольше таймаута. Используется в single-режиме как замена recla |
| `_unstick_loop` | 1025-1063 | `(self) -> None` | 7 | 1 | 2 | Фоновая задача: периодически откатывает зависшие ``processing``. Используется только в single-режиме (worker_p |
| `_claim_one` | 1065-1146 | `(self, *, priority_contents: tuple[str, ...] | None=None) -> dict | None` | 6 | 2 | 5 | Атомарно захватить одну задачу и перевести её в ``processing``. Режимы (выбираются через ``claim_strategy``):  |
| `_claim_one_single` | 1148-1207 | `(self, *, priority_contents: tuple[str, ...] | None=None) -> dict | None` | 2 | 1 | 6 | Single-режим: захват задачи через ``UPDATE ... RETURNING``. Атомарность обеспечивается подзапросом ``SELECT .. |
| `_poll_once` | 1209-1342 | `(self, exchange: MessageExchange) -> bool` | 15 | 1 | 5 | Забрать самое старое сообщение (через клейм) и отправить агенту. Алгоритм: 1. ``_claim_one`` — атомарный клейм |
| `_insert_assistant_message` | 1344-1372 | `(self, user_msg_id: str, chat_id: str) -> str` | 1 | 1 | 4 | Создать assistant-заглушку (status='processing') и сохранить её id. Зачем: чтобы web-сервер (Streamlit) мог на |
| `_mark_failed` | 1374-1460 | `(self, user_msg_id: str, assistant_msg_id: str | None, reason: str) -> None` | 10 | 4 | 4 | Зафиксировать ошибку обработки пользовательского сообщения. Вызывается при: — ошибке диспетчеризации ("dispatc |
| `_drop_context_bridge` | 1462-1476 | `(self, chat_id: str | None) -> None` | 3 | 4 | 3 | Снять per-iteration мост контекста для чата (анти-stale). Вызывается в финалах оборота (``_finalize_turn``, `` |
| `send_reasoning_delta` | 1492-1523 | `(self, chat_id: str, delta: str, metadata: dict[str, Any] | None=None, *, stream_id: str |` | 9 | 0 | 0 | Получить чанк рассуждений от агента и добавить в буфер. Параметры: chat_id — ID чата (не используется, т.к. бе |
| `send_reasoning_end` | 1525-1537 | `(self, chat_id: str, metadata: dict[str, Any] | None=None, *, stream_id: str | None=None) ` | 1 | 0 | 0 | Сигнал конца рассуждений. Не используется — финализация в send(). Принимает ``stream_id`` для совместимости с  |
| `send` | 1566-1652 | `(self, msg: OutboundMessage) -> None` | 31 | 0 | 5 | — |
| `_merge_tool_delivery` | 1654-1716 | `(self, msg: OutboundMessage, meta: dict[str, Any], msg_id: str | None) -> None` | 22 | 2 | 1 | Дописать промежуточную публикацию тула ``message(...)`` в сроку. Вызывается на outbound, пришедших ДО завершен |
| `_finalize_turn` | 1718-1909 | `(self, msg: OutboundMessage, meta: dict[str, Any], msg_id: str | None) -> None` | 40 | 3 | 2 | Зафинализировать оборот: записать ответ, закрыть claim и слот. Единственное место, где снимается ``_msg_ctx``, |
| `send_delta` | 1911-1971 | `(self, chat_id: str, delta: str, metadata: dict[str, Any] | None=None, *, stream_id: str |` | 14 | 0 | 4 | Получить очередной чанк стримингового ответа от агента. Когда агент использует стриминг (потоковую генерацию), |
| `_release_slot` | 1988-2005 | `(self, user_msg_id: str) -> None` | 3 | 5 | 4 | Освободить слот параллельности для указанного сообщения. Идемпотентен: можно вызывать多次 для одного id — второй |
| `_cleanup_unresolvable_turn` | 2007-2051 | `(self, chat_id: str | None, user_msg_id: str | None, assistant_msg_id: str | None) -> None` | 9 | 1 | 1 | Очистить локальное состояние при нерезолвенном контексте оборота. Используется, когда ``_resolve_turn_context` |
| `_resolve_assistant_msg_id` | 2057-2075 | `(self, metadata: dict[str, Any] | None) -> str | None` | 11 | 3 | 2 | Извлечь ``assistant_msg_id`` из metadata или ``_msg_ctx``. Приоритет: 1. ``metadata["answer_id"]`` — напрямую  |
| `_resolve_turn_context` | 2077-2164 | `(self, meta: dict[str, Any] | None, *, chat_id: str | None=None, explicit_msg_id: str | No` | 34 | 1 | 2 | Единый резолвер контекста оборота: ``user_msg_id`` + ``assistant_msg_id`` + ``chat_id``. Приоритет для ``user_ |
| `default_config` | 2171-2190 | `(cls) -> dict[str, Any]` | 1 | 0 | 2 | Пример конфигурации канала (вставляется в config.json). |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_resolve_sfs_base` | 67-80 | `(media_cache_dir: str | Path) -> Path` | 3 | 2 | Преобразовать ``channels.postgres.media_cache_dir`` в ``base_dir`` для ``SessionFileStore``. ``SessionFileStor |

## `lib/channels/redis_channel.py` — 378 LOC (code 269)
- module: `lib.channels.redis_channel`
- docstring: Redis-канал для nanobot. Канал работает через Redis-списки (блокирующие очереди): • Входящие сообщения читаются BRPOP из inbox-списка • Ответы пишутся LPUSH в per-chat outbox-список ═ ПРИНЦИП РАБОТЫ ═════════════════════
- static importers (2): `lib/services/channel_factory.py`, `tests/test_redis_channel.py`
- string/dynamic refs: 5
- test files touching it: — none —
- classes: 1, module functions: 1

### class `RedisChannel` — lines 113-378 (266 LOC), 9 methods
- bases: BaseChannel
- decorators: —
- docstring: Канал поверх Redis-очередей. Читает входящие сообщения из Redis-списка (BRPOP) и отправляет ответы в per-chat outbox (LPUSH). Формат JSON повторяет поля InboundMessage / OutboundMessage.
- name referenced in 5 file(s); tests: 4

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 128-175 | `(self, config: Any, bus: MessageBus) -> None` | 3 | 0 | 6 | — |
| `file_store` | 182-184 | `(self)` | 1 | 0 | 0 | Хранилище вложений для ``MessageExchange`` (кодек media). |
| `start` | 186-194 | `(self) -> None` | 1 | 0 | 21 | Подключиться к Redis и запустить общий движок обмена. |
| `stop` | 196-202 | `(self) -> None` | 2 | 0 | 16 | Остановить общий движок обмена и закрыть соединение с Redis. |
| `_connect` | 208-216 | `(self)` | 2 | 1 | 1 | Создать асинхронное Redis-соединение. |
| `poll_inbound` | 222-299 | `(self, exchange) -> bool` | 21 | 0 | 2 | Забрать одно сообщение из Redis (BRPOP), распарсить JSON, декодировать вложения и отправить агенту через _hand |
| `send` | 305-349 | `(self, msg: OutboundMessage) -> None` | 8 | 0 | 5 | Записать ответ агента в ``outgoing_prefix:{chat_id}`` (LPUSH). Пропускает служебные сообщения: • _reasoning_de |
| `send_delta` | 351-358 | `(self, chat_id: str, delta: str, metadata: dict[str, Any] | None=None) -> None` | 1 | 0 | 4 | Стриминг не поддерживается — все сообщения приходят целиком в send(). Заглушка нужна для совместимости с BaseC |
| `default_config` | 365-378 | `(cls) -> dict[str, Any]` | 1 | 0 | 2 | — |

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `_resolve_sfs_base` | 104-110 | `(media_cache_dir: str | Path) -> Path` | 3 | 2 | Преобразовать ``channels.redis.media_cache_dir`` в ``base_dir`` для ``SessionFileStore`` (колонка media → cach |

## `lib/channels/message_exchange.py` — 171 LOC (code 131)
- module: `lib.channels.message_exchange`
- docstring: Общий движок обмена сообщениями для каналов. ``postgres_channel`` и ``redis_channel`` кардинально различаются транспортом (SQL-таблица vs Redis-очереди), но разделяют одинаковую оркестрацию: * цикл поллинга входящих сооб
- static importers (3): `lib/channels/postgres_channel.py`, `lib/channels/redis_channel.py`, `tests/test_user_stop_signal_priority.py`
- string/dynamic refs: 1
- test files touching it: — none —
- classes: 1, module functions: 0

### class `MessageExchange` — lines 47-171 (125 LOC), 13 methods
- bases: object
- decorators: —
- docstring: Общая оркестрация обмена сообщениями поверх транспорта-канала.
- name referenced in 3 file(s); tests: 1

| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |
|---|---|---|---:|---:|---:|---|
| `__init__` | 50-66 | `(self, channel: Any, *, max_concurrent: int=1, poll_interval: float=5.0, error_backoff: fl` | 2 | 0 | 6 | — |
| `embed` | 72-74 | `(self, media: list[str] | None) -> list[Any]` | 2 | 0 | 0 | Сериализовать вложения в storage AW-формат для БД/очереди. |
| `decode` | 76-78 | `(self, media: list[Any], session_key: str) -> list[Any]` | 1 | 0 | 8 | Декодировать вложения к runtime-формату (пути для агента). |
| `resolve` | 80-82 | `(self, media: list[Any]) -> tuple[list[str], list[str]]` | 1 | 0 | 12 | Распаковать media в пути для агента и подсказки. |
| `inflight` | 89-90 | `(self) -> set[str]` | 1 | 0 | 0 | — |
| `is_slot_free` | 92-93 | `(self) -> bool` | 1 | 1 | 4 | — |
| `acquire_slot` | 95-96 | `(self) -> None` | 1 | 0 | 6 | — |
| `add_inflight` | 98-99 | `(self, key: str) -> None` | 1 | 0 | 4 | — |
| `discard_inflight` | 101-102 | `(self, key: str) -> None` | 1 | 0 | 1 | — |
| `release_slot` | 104-113 | `(self, key: str | None=None) -> None` | 4 | 0 | 1 | Идемпотентно отпустить слот (защита от двойного release). |
| `start` | 119-125 | `(self) -> None` | 1 | 0 | 21 | — |
| `stop` | 127-133 | `(self) -> None` | 3 | 0 | 16 | — |
| `_poll_loop` | 135-171 | `(self) -> None` | 8 | 1 | 1 | Бесконечный цикл: сначала priority, потом обычные (если есть слот). Priority polling выполняется **всегда**, н |

## `lib/channels/priority_commands.py` — 80 LOC (code 54)
- module: `lib.channels.priority_commands`
- docstring: Единый источник списка priority-команд nanobot для каналов. Каналы (PostgresChannel, RedisChannel) фильтруют priority polling через этот список. Список читается из ``nanobot.command.router.CommandRouter._priority`` — еди
- static importers (3): `lib/channels/postgres_channel.py`, `tests/test_priority_commands.py`, `tests/test_user_stop_signal_priority.py`
- string/dynamic refs: 1
- test files touching it: — none —
- classes: 0, module functions: 1

### module-level functions

| Function | Lines | Sig | Br | Ref files | Doc |
|---|---|---|---:|---:|---|
| `get_priority_commands` | 34-80 | `() -> tuple[str, ...]` | 9 | 3 | Вернуть актуальный список priority-команд nanobot. Шаги: 1. Стартуем с базовых встроенных (``_DEFAULT_PRIORITY |

## `lib/channels/__init__.py` — 0 LOC (code 0)
- module: `lib.channels`
- docstring: — NONE —
- static importers (3): `tests/test_parallel_modes.py`, `tests/test_single_mode_audit.py`, `tests/test_user_stop_signal_priority.py`
- string/dynamic refs: 0
- test files touching it: — none —
- classes: 0, module functions: 0

