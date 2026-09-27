# Design — fix-error-fallback-double-outbound

## Контекст

Изменение правит `_wrap_fail` в `RuntimePatcher.patch_turn_delivery_fail`
(`lib/services/runtime_patcher.py:1612-1727`), чтобы привести реализацию в
соответствие с контрактом capability `runtime/error-fallback` v1.0.0. Цель —
устранить пять дефектов, перечисленных в `proposal.md`. Это **не** новый
патч, а правка существующего; поэтому design описывает, что именно меняется
внутри обёртки и в `apply_all`, и какие альтернативы были рассмотрены.

## Решения

### 1. Подавление двойного outbound через per-instance прокси на `self.bus`

**Реализация.** В `_wrap_fail` непосредственно перед вызовом оригинального
`fail()` создаётся локальный класс:

```python
class _OutboundSilencer:
    __slots__ = ("_inner",)
    def __init__(self, inner): self._inner = inner
    def __getattr__(self, name): return getattr(self._inner, name)
    async def publish_outbound(self, msg): return None
```

Затем:

```python
original_bus = self.bus
self.bus = _OutboundSilencer(original_bus)
try:
    result = original_fail(self, publish_completion=publish_completion)
    if asyncio.iscoroutine(result):
        await result
finally:
    self.bus = original_bus
```

**Почему не вызывать `original_fail` вообще и не дублировать `turn_completed`.**
Upstream `turn_completed` (`turn_delivery.py:345-353`) — это 9 строк с
несколькими обязательными полями (`session_key`, `metadata`, `outcome`,
`failure_kind`). Дублирование делает реализацию зависимой от формы сигнатуры
runtime-event-publisher'а: при апгрейде nanobot скопированный код молча
разъезжается с реальным. Прокси позволяет сохранить upstream как
единственный источник истины для `turn_completed`, подавляя только
outbound-публикацию.

**Почему per-instance, а не monkey-patch на класс `MessageBus`.**
`self.bus` — атрибут конкретного экземпляра `TurnDelivery`. Подмена на
уровне атрибута экземпляра не задевает другие обороты (у них свой `bus`).
Monkey-patch класса `MessageBus.publish_outbound` спас бы от двойного
outbound, но не оставил бы способа различать «это fallback-обёртка» и «это
нормальный outbound от бизнес-логики».

**Concurrency-safety.** `TurnDelivery` создаётся на оборот через
`TurnDelivery.create(msg, session_key)` (`turn_delivery.py:85-103`) — один
экземпляр на один in-flight `InboundMessage`. Других потребителей
`self.bus` внутри обёртки нет. Подмена атрибута в `try/finally` локальна.

### 2. Захват исключения через `sys.exception()`

**Реализация.** В первой строке `_wrap_fail`:

```python
import sys
exc = sys.exception()
```

`sys.exception()` возвращает активное исключение, **если** в текущем
async-фрейме есть обрабатываемый `except`-блок. Это верно для нашего
случая: `loop.py:1480-1482` вызывает `await delivery.fail(...)` изнутри
`except Exception as exc:`. Пока корутина `_wrap_fail` исполняется,
`sys.exception()` возвращает то же исключение.

**Почему не передавать `exc` через сигнатуру `fail`.** Сигнатура
`TurnDelivery.fail(self, *, publish_completion: bool)` — часть upstream API.
Её изменение потребовало бы fork'а nanobot. Альтернатива через патч
`AgentLoop._process_message` была бы многословнее (поверхностный метод на
~120 строк) и задела бы не-fallback код-пути.

**Защита от «вызвали вне except».** Если `_wrap_fail` позвали без активного
исключения (юнит-тест, прямой вызов), `sys.exception()` вернёт `None`. В
payload кладётся `exception_available=False`, `exception_type=None`,
`exception_message=None`. Дыра видна в логах, а не молчит.

### 3. Авторитетные источники `session_key` и идентификатора пользователя

```python
session_key = getattr(self, "session_key", None)
sender_id = getattr(self.lifecycle_message, "sender_id", None) if self.lifecycle_message else None
```

- `TurnDelivery.session_key` — атрибут экземпляра, установленный
  `TurnDelivery.create(msg, session_key)` (`turn_delivery.py:88, 101`).
- `InboundMessage.sender_id` — поле dataclass (`bus/events.py:29`).
  Поле `user_id` в `InboundMessage` отсутствует.

Текущая реализация читала `lifecycle_message.session_key` и
`lifecycle_message.user_id` — оба `None` всегда.

### 4. Передача `agent_id` через `apply_all`

В `apply_all` (`lib/services/runtime_patcher.py:573`) вызов меняется на:

```python
agent_id = _resolve_agent_id(config)
self._record(report, "turn_delivery_fail",
             self.patch_turn_delivery_fail(settings, db_logging_service, agent_id=agent_id))
```

`_resolve_agent_id(config)` — маленький helper, читающий имя активного
агента из runtime-конфига nanobot (по тому же паттерну, что
`patch_assemble_outbound` использует `config` для `session_key`). Точная
форма helper'а уточняется в `tasks.md` (п. 1.4).

**Почему нельзя просто передать `agent=agent` и взять `.name`.**
`apply_all` уже получает `agent: AgentLoop`, но имя агента в runtime
определяется через `config.agents[name].name` или
`config.default_agent`. Helper инкапсулирует эту логику, чтобы не дублировать
её в каждом патче.

## Альтернативы (отвергнутые)

- **Скопировать `turn_completed` inline и не вызывать `original_fail` вообще.**
  Отвергнуто: 9 строк upstream-API, которые при апгрейде nanobot молча
  дрейфуют. Прокси даёт ту же гарантию корректности без дублирования.

- **Патчить `AgentLoop._process_message` вместо `TurnDelivery.fail`.**
  Отвергнуто: метод ~120 строк с разветвлённой логикой
  (cancellation, continuation, automation, restore-checkpoint). Больше
  поверхности для регрессий; тот же `sys.exception()` всё равно нужен для
  исключения.

- **Подавлять outbound на уровне каналов (`PostgresChannel.publish_outbound`).**
  Отвергнуто: каналы — это «sink», а не policy-точка. Условие «не публиковать
  upstream-ошибку» принадлежит runtime-слою, не delivery-слою.

## Что НЕ делается этим change'ем

- Не вводятся новые ключи конфига. `ErrorMessagesSettings` остаётся как
  есть.
- Не меняется upstream-контракт `TurnDelivery.fail`.
- Не правятся `lib/core/project_settings.py`, `tests/test_project_settings.py`,
  `project.json`, `AGENTS.md`, `docs/TARGET_ARCHITECTURE.md`. Они уже в
  согласии со спекой — этот change правит только отстающий код и тесты.

## Тестовая стратегия

1. **Переписать `TestPatchTurnDeliveryFail`** на `AsyncMock` для
   `bus.publish_outbound` и `runtime_event_publisher.turn_completed`.
2. **Добавить сценарии:**
   - ровно один outbound;
   - `content` не содержит upstream-литерала;
   - `turn_completed(outcome="failed", failure_kind="internal")` вызван
     ровно один раз при `publish_completion=True`;
   - `turn_completed` НЕ вызван при `publish_completion=False`;
   - payload содержит `exception_type`, `exception_message`,
     `exception_available=true` при вызове из `except`-блока теста;
   - `exception_available=false` при прямом вызове без `sys.exception()`;
   - `session_key` берётся из `self.session_key`, не из `lifecycle_message`;
   - `sender_id` берётся из `lifecycle_message.sender_id`;
   - `agent_id` из config попадает в payload.
3. **Удалить или инвертировать существующие assertions**, которые закрепляли
   «два outbound'а» — это была кодификация бага.
