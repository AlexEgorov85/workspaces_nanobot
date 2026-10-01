# ADR: публичная точка расширения для `TurnDelivery.fail` вместо патча

- **Статус:** принято
- **Дата:** 2026-10-01
- **Change:** `enterprise-mcp-platform`, фаза 6 (п. 6.1 и 6.6)
- **Затрагивает:** `lib/services/runtime_patcher.py` (патчи `turn_delivery_fail`,
  `assemble_outbound`), `lib/core/agent_factory.py`

## Контекст

План фазы 6 предписывал:

- **6.1** — `turn_delivery_fail` → хук на `finalize_content` (замена текста)
  + `on_error` (логирование `turn_failed`);
- **6.6** — `assemble_outbound` удалить целиком: `_final_turn` перевести на
  `TurnEndEvent`, `media` и `_tool_audit` — на публикацию из хука через
  `turn_context.events`.

Обе формулировки описывают API, которого в зафиксированной версии
`nanobot-ai==0.3.5` нет. Это выяснено инспекцией установленного пакета, а не
предположением.

## Что показала инспекция nanobot 0.3.5

### 1. `finalize_content` не видит текст ошибки

`AgentHook.finalize_content(context, content) -> str | None` существует и
вызывается — трижды, из `agent/runner.py` (строки 588, 629, 1214). Но все
три вызова находятся на пути ответа модели (`response.content`).

Текст ошибки рождается в другом месте — `agent/turn_delivery.py:341`, внутри
`TurnDelivery.fail()`, который собирает `OutboundMessage` и публикует его
**напрямую в шину**:

```python
async def fail(self, *, publish_completion: bool) -> None:
    await self.bus.publish_outbound(
        OutboundMessage(
            channel=self.lifecycle_message.channel,
            chat_id=self.lifecycle_message.chat_id,
            content="Sorry, I encountered an error.",   # <- turn_delivery.py:341
            metadata=dict(self.lifecycle_message.metadata or {}),
        )
    )
    if publish_completion:
        await self.runtime_event_publisher.turn_completed(...)
```

`TurnDelivery` не имеет доступа к `AgentHook`, и `fail()` не вызывает
`finalize_content`. **Перенести замену текста на `finalize_content` нельзя** —
хук в принципе не увидит эту строку.

### 2. `TurnEndEvent` в 0.3.5 не существует

Пункт 6.6 опирается на `TurnEndEvent` и `turn_context.events`. В
`nanobot.agent.loop` есть `AgentEvent`, `StreamDeltaEvent`, `StreamEndEvent`,
`StreamedResponseEvent`, `TurnContext`, `EventSink`, `TurnRoute`,
`RetryStatusEvent` — но `TurnEndEvent` среди них нет, и модуля
`nanobot.agent.events` тоже нет. Пункт 6.6 в текущей формулировке
нереализуем.

### 3. Но есть публичная точка, которой план не предполагал

`AgentLoop.__init__` принимает параметр

```python
turn_delivery_factory: TurnDeliveryFactory | None = None
```

и проверяет ровно одно: `turn_delivery_factory.bus is bus`. Иначе —
`self.turn_delivery_factory = TurnDeliveryFactory(bus)`.

`AgentLoop.from_config(config, bus, *, tool_registry, **extra)` пробрасывает
`**extra` в конструктор, поэтому параметр достижим из нашего
`AgentFactory` без единого monkey-patch'а.

`TurnDeliveryFactory` — обычный публичный класс; `create()` и `unrouted()`
собирают `TurnDelivery(...)` напрямую, а `_default_route` — статический метод.
Проверено на живом экземпляре:

| проверка | результат |
|---|---|
| `__slots__` у `TurnDelivery` | отсутствует → подмена класса безопасна |
| MRO | `[TurnDelivery, object]` |
| `super().create(...)` + `__class__` = подкласс | работает, `session_key`/`route` сохранены |
| то же для `unrouted(...)` | работает |
| сигнатура `fail` | `(self, *, publish_completion: bool) -> None` |

## Решение

**6.1 переносится на внедрение через конструктор, а не на хук.**

- `lib/services/turn_delivery_factory.py`:
  - `FallbackTurnDelivery(TurnDelivery)` — переопределяет `fail()`: публикует
    `gateway.error_messages.internal_error` вместо upstream-литерала и пишет
    `turn_failed` в `agent_gateway_logs`;
  - `FallbackTurnDeliveryFactory(TurnDeliveryFactory)` — вызывает
    `super().create()/unrouted()` (маршрутизация остаётся upstream) и
    подменяет класс выданного экземпляра;
- `AgentFactory` передаёт фабрику в `AgentLoop.from_config(...)` параметром
  `turn_delivery_factory`;
- патч `turn_delivery_fail` удаляется из `RuntimePatcher` целиком.

Почему лучше патча:

1. Уходит `_OutboundSilencer` — подавление двойного outbound. В
   переопределении `fail()` обе половины (публикация и `turn_completed`)
   реализованы явно, upstream-метод не вызывается, дубликат невозможен
   конструктивно.
2. Нет зависимости от `sys.exception()` внутри `except`-блока вызывающего —
   исключение доступно как обычный аргумент обработчика в `_process_message`.
3. При апгрейде nanobot поломка громкая и в момент инъекции
   (`AgentLoop.__init__` валидирует фабрику), а не тихая на первом же
   внутреннем сбое у пользователя.
4. Инвариант «пользователь получает **один** fallback-ответ» обеспечивается
   структурно.

Ограничение, фиксируемое явно: переопределение дублирует ~10 строк upstream
логики `fail()` (публикация + `turn_completed`). Это осознанный размен:
патч зависел от приватного метода и от порядка вызовов, эти 10 строк — от
публичной сигнатуры.

**6.6 остаётся нереализуемым в текущей формулировке.** `TurnEndEvent` нет.
Требуется отдельное решение: либо принять, что `_final_turn` и `media`
остаются на `_assemble_outbound` (патч живёт), либо спроектировать перенос на
`EventSink`/`RuntimeEventPublisher`, которые в 0.3.5 действительно есть.
Это решение принимается отдельно, а не молча.

## Что проверено и чем

`%TEMP%/verify_delivery_injection.py` — инспекция поверхности и живая подмена
класса на настоящем `TurnDelivery` (слоты, MRO, `create`, `unrouted`,
сигнатура `fail`, приём параметра агентом).

Правило проекта «страж проверяется на заведомо плохих данных» для новой
реализации обязательно: тест обязан падать, если `fail()` начнёт
публиковать две корректные публикации или если фабрика перестанет подменять
класс в `unrouted`.
