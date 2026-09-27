## ADDED Requirements

### Requirement: Подписчики runtime-событий детерминированы относительно apply_all и каналов

`ApplicationContext.start()` MUST ДОЛЖЕН вызывать подписчики (`RuntimeEventsSubscriber` и `SubagentLoggingSubscriber`) **после** `RuntimePatcher.apply_all()` и **до** старта каналов (postgres / telegram / redis / streamlit / cli / websocket). Конкретный порядок MUST ДОЛЖЕН быть:

1. `RuntimePatcher.apply_all()` — все monkey-patches установлены.
2. Подписчики `start()` (включая подписки на `TurnRuntimeAdmitted`, `TurnCompleted`, `SubagentTurnCompleted`) — все подписки зарегистрированы через `bus.subscribe(handler, EventType)` и активны до старта каналов.
3. Старт каналов — агенты начинают принимать inbound-сообщения.
4. На shutdown (обратный порядок): каналы останавливаются, затем `MessageBus.drain()` ожидает завершения in-flight handler'ов, затем подписчики `stop()` дерегистрируют подписки через возврат `unsubscribe()`.
5. Fallback `agent._last_usage` в `RuntimePatcher._attach_context_window` MUST НЕ ДОЛЖЕН существовать после старта `RuntimeEventsSubscriber`: bridge MUST ДОЛЖЕН быть засеян `TurnRuntimeAdmitted` подпиской к моменту первого `OutboundMessage` с `_final_turn=True`. Если bridge не засеян — MUST поднимается `ContextWindowNotSeededError` (явная ошибка вместо тихого `used=0`).

#### Scenario: Порядок start детерминирован
- WHEN `ctx.start()` вызывается
- THEN `RuntimePatcher.apply_all()` MUST быть вызван ПЕРЕД `RuntimeEventsSubscriber.start()`
- AND `RuntimeEventsSubscriber.start()` MUST быть вызван ПЕРЕД стартом каналов
- AND порядок MUST НЕ ДОЛЖЕН различаться между запусками (никаких `if channel == "postgres"` рантайм-перестановок)

#### Scenario: Порядок stop не теряет события
- WHEN `ctx.stop()` вызывается
- THEN каналы MUST быть остановлены ПЕРВЫМИ (никаких inbound-сообщений больше не принимается)
- AND `MessageBus.drain()` MUST быть вызван (если ещё не вызывается в текущем коде — change добавляет его)
- AND `RuntimeEventsSubscriber.stop()` MUST быть вызван ПОСЛЕ `bus.drain()` (чтобы in-flight handler'ы завершились)

#### Scenario: First turn без tool-вызовов имеет context_window
- WHEN агент делает user-turn без tool-вызовов
- AND `RuntimeEventsSubscriber.start()` уже выполнен
- THEN `metadata.context_window` MUST быть непустым в финальном `OutboundMessage`
- AND `_attach_context_window` MUST НЕ ДОЛЖЕН использовать fallback `agent._last_usage` (его нет в коде)

#### Scenario: Отсутствие подписки ломает метрику явно
- WHEN `RuntimeEventsSubscriber.start()` НЕ был вызван (например, в тестах без ApplicationContext)
- THEN `_attach_context_window` MUST raise `ContextWindowNotSeededError`
- AND UI/CLI MUST получить явное сообщение об ошибке (не пустую метрику `used=0`)