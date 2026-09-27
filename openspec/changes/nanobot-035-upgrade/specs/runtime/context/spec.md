## Purpose

Описывает нормативные требования к runtime-context подсистеме после
апгрейда upstream `nanobot-ai 0.3.5`. Цель — гарантировать совместимость
runtime-patcher с новыми сигнатурами и миграцию compaction pipeline на
upstream EventSink (`CompactionEventSubscriber`), избегая regressions.

## ADDED Requirements

### Requirement: Совместимость с upstream nanobot

`ApplicationContext.start()` MUST ДОЛЖЕН вызывать `RuntimePatcher.apply_all` после инициализации сервисов и до старта каналов; если `apply_all` оставляет непустой `report.failed`, система MUST ДОЛЖНА логировать warning, но MUST NOT НЕ ДОЛЖНА прерывать старт (каждый `failed`-патч явно помечен `DEPRECATED` и не критичен для прод).

#### Scenario: Апгрейд upstream-nanobot без регрессии

- **WHEN** версия `nanobot-ai` в `requirements.txt` меняется
- **THEN** `pytest tests/contract/` MUST ДОЛЖЕН запускаться первым; если есть падения, они MUST ДОЛЖНЫ быть исправлены или явно помечены `xfail` до merge upgrade-изменения

### Requirement: ContextCompactionService через upstream EventSink

`ContextCompactionService` MUST ДОЛЖЕН использовать upstream `EventSink` для получения событий сжатия контекста (`ContextCompactionEvent`) и MUST NOT НЕ ДОЛЖЕН оборачивать внутренние методы `Consolidator` (которых может не быть в следующих версиях upstream).

#### Scenario: Подписка на compaction-события

- **WHEN** upstream публикует `ContextCompactionEvent(phase=...)` через `EventSink.emit` → `bus.publish_event` → `OutboundMessage.event` в `bus.outbound`
- **THEN** `postgres_channel` MUST ДОЛЖЕН распознать `isinstance(msg.event, ContextCompactionEvent)` и вызвать `ContextCompactionService._notify(session_key, report_from_event)`; `_notify` MUST ДОЛЖЕН писать history-notice в `agent_conversation_messages` для фазы `succeeded` и event `context_compacted` в `agent_gateway_logs` для всех фаз (`started`, `succeeded`, `failed`, `cancelled`)

#### Scenario: Принудительное сжатие через tool `/compact`

- **WHEN** агент вызывает `compact_context` tool или пользователь подаёт `/compact` slash-команду
- **THEN** система MUST ДОЛЖЕН вызвать `loop.consolidator.compact_idle_session(ctx.key, runtime=runtime, events=delivery.events)` (upstream API через `cmd_compact`); наш `_notify` срабатывает через фильтр `postgres_channel`, MUST NOT НЕ ДОЛЖЕН через `AgentHook.after_run` (хуки не видят `OutboundMessage.event`)