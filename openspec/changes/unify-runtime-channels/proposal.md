## Why

Сейчас в проекте **два полноценных Agent Runtime**: `gateway.py` и
`cli_agent.py` собирают каждый свой `ApplicationContext` со своим `AgentLoop`,
своими патчами, своим bus'ом и своим session manager'ом. CLI при этом не
является «клиентом чего-то» — он второй равноправный владелец рантайма,
отличающийся от Gateway одним параметром `role`.

### Проверенные по коду факты

Все ссылки — `file:line`, проверены чтением репозитория и установленного
пакета `nanobot` (`site-packages/nanobot`).

**Факт 1 — `role` — единственное, что различает composition двух runtime.**
`ApplicationContext.create(..., role: Literal["gateway", "cli"])`
(`lib/core/application_context.py:193`) вызывается из `gateway.py:121`
(`role='gateway'`) и `cli_agent.py:125,151,175` (`role='cli'`). Реальные
чтения `ctx.role` в runtime: `application_context.py:250` (присваивание),
`:385` (гейт CronService), `:1538` (identity claim'а cache — уже устранён
change `cache-architecture-alignment`).

**Факт 2 — таблица composition-сервисов в канонической спеке неверна.**
`openspec/specs/runtime/entrypoints/spec.md:164` утверждает, что
`PostgresChannel` создаётся `ApplicationContext` при `role="gateway"`.
Фактически канал создаёт `ChannelFactory._add_postgres`
(`lib/services/channel_factory.py:129-191`), который вызывается из
`gateway.py:249-258` — то есть **вне** `ApplicationContext`. Спека
приписывает composition тому, кто её не выполняет.

**Факт 3 — `AgentLoop` не transport-agnostic в строгом смысле.**
`openspec/specs/runtime/entrypoints/spec.md:189-199` требует, чтобы
`AgentLoop` «не знал о CLI, WebSocket…». Фактически в
`nanobot/agent/loop.py` есть ветвления по литеральным именам каналов:
`:928` (`msg.channel in {"cli", "system"}`), `:1331`
(`msg.channel == "websocket"`), `:1297`, `:1342`, `:1814`, `:1822`,
`:2340` (`"system"`). Требование не может остаться в текущей формулировке:
оно неверно, а «исправить» его в upstream нельзя в рамках этого change.

**Факт 4 — CLI уже является постоянным потребителем outbound-сообщений,
но определяет завершение turn'а эвристикой.**
`lib/cli/console_loop.py:254-317` (`_consume_outbound`) — постоянный
потребитель: `StreamDeltaEvent` → рендер (`:266-269`),
`StreamEndEvent` → закрыть рендер (`:270-273`), `StreamedResponseEvent` →
**`turn_done.set()`** (`:274-290`). Turn завершается по **первому** стримовому
ответу, а не по фактическому завершению. Страховочный таймаут
`turn_wait_timeout` (`:233-243`, дефолт 300 с, `:395-404`) маскирует
зависание как «нет связи».

**Факт 5 — авторитетный сигнал завершения turn'а существует и уже на проводе.**
Upstream проецирует внутренний `TurnEndEvent` в wire-кадр
`{"event": "turn_end", ...}`: `nanobot/webui/outbound_projection.py:227-244`
→ `encode_turn_end` (`nanobot/webui/outbound_wire.py:198-231`). Поля —
`latency_ms`, `usage`, `round_usages`, `context_window_tokens`, `outcome`,
`failure_kind`, `failure_attempts`, `failure_message`. То есть клиент
получает типизированное завершение turn'а с usage и ошибками, а не должен
угадывать его по первому финальному сообщению.

**Факт 6 — транспорт «клиент → Gateway» в проекте уже существует и поднят.**
Upstream-канал `WebSocketChannel` (`nanobot/channels/websocket/runtime.py:363`)
регистрируется `ChannelManager` по умолчанию
(`nanobot/channels/websocket/manifest.py:13-21`, `default_enabled=True`) и
поднимается в этом проекте через `ChannelFactory.create_all` →
`ChannelManager` (`channel_factory.py:67-69`) при старте Gateway. Бинд по
умолчанию — `127.0.0.1:8765` (`runtime.py:197-198`); в `config.json` секции
`channels.websocket` нет, значит действуют дефолты. Gateway уже проверяет
занятость этого порта перед стартом (`gateway.py:214,460-511`).

**Факт 7 — wire-протокол этого транспорта Python-видим и пригоден для CLI.**
Входящее: `{"type": "message", "chat_id": ..., "content": ...}`
(`nanobot/webui/inbound_commands.py:467,476-504`) → `_handle_message` →
`InboundMessage(channel="websocket", ...)` → `bus.publish_inbound`
(`nanobot/channels/base.py:310-321`). Исходящее: `ready`, `delta`,
`stream_end`, `message`, `reasoning_delta`, `reasoning_end`, `turn_end`
(`runtime.py:810-822,1339-1365,1181-1214,1247-1284`; `outbound_wire.py:198-231`).
Slash-команды обрабатываются: `AgentLoop` диспетчеризует команды для любого
канала, кроме `"system"` (`nanobot/agent/loop.py:1342`), поэтому `/compact`,
отправленный как `content`, обрабатывается командным роутером Gateway
(включая пропатченный `cmd_compact`, `RuntimePatcher.patch_compact_command`).

**Факт 8 — Python-клиента этого транспорта нет ни в репозитории, ни в
пакете.** Поиск по `websockets`/`ws://`/`aiohttp.ClientSession`/`httpx` по
`lib/`, `workspace/`, `tools/`, корневым `*.py`: клиентских подключений к
Gateway нет; `httpx` встречается только для LLM и embedding
(`lib/services/llm_client.py:156`, `cache_provider_impl.py:265`). Upstream
имеет WS-клиент только для NapCat/QQ (`nanobot/channels/napcat/runtime.py:111`).
Upstream TUI — отдельный TypeScript-клиент в бинарнике
(`nanobot/cli/tui_launcher.py:78-131`), Python-часть только формирует env и
health-probe (`:99-129`).

**Факт 9 — локальный `/compact` в CLI после перехода станет неверным по
существу.** `console_loop.py:124-142,359-361` вызывает
`ContextCompactionService.compact(session_key="cli:<session>", force=True)`
локально, а `cli_agent.py:232-243` (`_migrate_cron_store`) переносит
`jobs.json` на диске CLI. Обе операции требуют локального runtime'а и
локального доступа к БД.

**Факт 10 — `enable_cron` — entrypoint-флаг, от которого зависит storage.**
`application_context.py:159,253,315`: `return_file_manager=not ctx.enable_cron`
(`SessionStorageService.create`, `lib/services/session_storage.py:42-51`).
Cron-гейт — `if ctx.enable_cron and ctx.role == "gateway"`
(`application_context.py:385`), при этом docstring `_make_cron_service`
(`:1727`) утверждает обратное («нужен для CLI-режима») — расхождение
документации с кодом.

**Факт 11 — `/health` в этом проекте не поднимается.**
Upstream health-сервер живёт в `nanobot/cli/gateway_runtime.py:761-816` и
в этом проекте **не вызывается**: grep по `nanobot.cli.gateway_runtime` в
репозитории — 0 совпадений. `RuntimeHealth`/`RuntimeReadiness`
(`lib/services/runtime_health.py:81-99,175-205`) — in-process-объекты; их
единственный production-caller — `ApplicationContext.start()`
(`application_context.py:598-611`), результат уходит только в лог. Утверждение
`AGENTS.md:30` про «`gateway /health`» — неверно. Endpoint `/health`
отсутствует и в HTTP-приложении WebSocket-порта
(`nanobot/webui/ws_http.py`, 0 совпадений по `health`).

## What Changes

1. **Agent Runtime MUST создаваться только Gateway.** `cli_agent.py` перестаёт
   вызывать `ApplicationContext.create()` и перестаёт быть вторым владельцем
   рантайма.
2. **Параметр `role` MUST быть удалён** из `ApplicationContext.create()` и из
   поля `ctx.role` (`application_context.py:156,193,250`). Это BREAKING и
   делается здесь, а не «после того, как понадобится».
3. **Cron MUST стать свойством Gateway, а не entrypoint-флага.**
   `enable_cron` перестаёт быть переключателем composition (`application_context.py:159,253,315,385`);
   `_make_cron_service` docstring (`:1727`) приводится в соответствие с кодом.
4. **`return_file_manager` MUST NOT зависеть от cron-флага** (`application_context.py:315`).
   Это сессионное решение, а не cron-решение; `session_storage.py:13-17`
   (docstring с перепутанным порядком возврата) правится заодно.
5. **CLI MUST быть клиентом Gateway по существующему WebSocket-транспорту.**
   Новый `CliChannel` — **клиент** этого wire-протокола, а не абстракция ради
   симметрии (см. Decision 2 в design).
6. **Завершение turn'а в CLI MUST определяться кадром `turn_end`**
   (`outbound_wire.py:198-231`), а не первым `StreamedResponseEvent`.
   `turn_wait_timeout` перестаёт быть сигналом завершения и остаётся только
   watchdog'ом соединения.
7. **`/compact` в CLI MUST делегироваться Gateway** как обычное сообщение
   (`content="/compact"`), а не выполняться локальным вызовом
   `ContextCompactionService` (`console_loop.py:124-142,359-361`).
8. **Каноническая спека MUST быть приведена в соответствие с кодом.**
   Исправляются два уже неверных утверждения: создание `PostgresChannel`
   силами `ApplicationContext` (`entrypoints/spec.md:164`; фактически —
   `ChannelFactory` из `gateway.py`) и формулировка про
   transport-agnostic `AgentLoop` (`:189-199`, в upstream есть ветвления по
   литеральным именам каналов). Пять требований, чьи заголовки после
   изменения модели становятся ложными, удаляются с `**Reason**`/
   `**Migration**`, а не переписываются на месте: `Единая typed signature
   ApplicationContext.create с role`, `role определяет composition
   инфраструктуры, не AgentLoop`, `WebSocket port check остаётся server-only`,
   `CLI-специфичные runtime-параметры`, `Slash-команда /compact в CLI
   остаётся локальной`. Дубликаты cache-контракта из `runtime/entrypoints`
   удаляются не здесь, а в `cache-architecture-alignment` — это его
   канонизация.
9. **CLI-флаги MUST соответствовать роли клиента:** `--session` сохраняется
   (идентификатор чата/сессии на Gateway), `--storage` и `--patched`
   удаляются (выбор локального хранилища и vanilla/patched-режим перестают
   иметь смысл), добавляется адрес Gateway.
10. **Отсутствие Gateway MUST диагностироваться явно и без автозапуска.**
    Upstream-механизм on-demand (`GatewayClientLease.ensure_on_demand_gateway`,
    `tui_launcher.py:482`) непригоден: он запускает команду
    `nanobot gateway <sub> --config <path>`
    (`nanobot/cli/webui_support.py:665-678`), то есть **upstream**-gateway
    без проектных каналов, патчей и tool'ов — runtime, не эквивалентный
    этому проекту.

## Capabilities

### New Capabilities

- `runtime/cli-client` — нормативный контракт CLI как клиента Gateway:
  транспорт, handshake, аутентификация, адресация сессии, жизненный цикл
  turn'а по wire-кадрам, поведение при недоступном Gateway, делегирование
  slash-команд.

### Modified Capabilities

- `runtime/entrypoints` — дельта из 15 требований:

  **REMOVED** (заголовок содержал утверждение, ставшее ложным; нормативный
  текст переносится в ADDED, обоснование — `**Reason**`/`**Migration**`):

  - `Единая typed signature ApplicationContext.create с role` → `ApplicationContext.create без параметра role`;
  - `role определяет composition инфраструктуры, не AgentLoop` → `composition принадлежит Gateway` (таблица «сервис × `role`» заменена таблицей «компонент → кто создаёт»);
  - `WebSocket port check остаётся server-only` → `WebSocket port check принадлежит Gateway, а тот же порт — адрес клиента`;
  - `CLI-специфичные runtime-параметры` → `CLI-клиент принимает параметры подключения`;
  - `Slash-команда /compact в CLI остаётся локальной` → `Slash-команда /compact в CLI делегируется Gateway`.

  **MODIFIED** (заголовки сохраняются, все canonical scenario titles
  сохранены):

  - `AgentLoop MUST быть transport-agnostic` — инвариант «мир только через bus»
    плюс явный перечень upstream-исключений и запрет их расширения проектом;
  - `Cron = gateway-only` — cron по конфигурации Gateway, без entrypoint-флага; `return_file_manager` больше не зависит от `enable_cron`;
  - `CLI имеет фиксированный профиль test` — профиль `test` определяет только локальное разрешение конфигурации клиента, а не состав runtime;
  - `Production entrypoints MUST NOT передавать profile в composition root` — CLI разрешает конфигурацию, не вызывая `create()`;
  - `Deprecated kwargs с явной compatibility boundary` — `enable_cron` удалён из перечня (cron больше не параметр composition), передача → `TypeError`.

  **ADDED:**

  - `ApplicationContext.create без параметра role`;
  - `composition принадлежит Gateway` (каналы и port check — вне `ApplicationContext`);
  - `CLI-клиент принимает параметры подключения`;
  - `Slash-команда /compact в CLI делегируется Gateway`;
  - `WebSocket port check принадлежит Gateway, а тот же порт — адрес клиента`.

## Dependencies Between Changes

Порядок реализации: `fix-cache-process-boundary` →
`cache-architecture-alignment` → `unify-runtime-channels`.

- **Зависит от `cache-architecture-alignment`:** удаление `role` возможно
  только после того, как `role` перестал участвовать в выборе cache owner
  (`worker_id` без `role`). Иначе удаление параметра сломает идентичность
  claim'а.
- **Зависит от `fix-cache-process-boundary`:** CLI перестаёт быть вторым
  владельцем cache-файла в этом же изменении — иначе в момент удаления
  `role` останется путь, где CLI открывает файл, который Gateway держит.
- **Независим от `remove-streamlit-runtime`:** `streamlit_app.py` вызывает
  только `config._initialize_settings` (`:95`) и НЕ вызывает
  `ApplicationContext.create()` — удаление `role` его не затрагивает.
  Сопряжение по конфигурации (`--profile`) сохраняется: CLI-клиент наследует
  тот же lifecycle-gate.

## Impact

**Runtime-код:**

- `lib/core/application_context.py` — удалить `role` (`:156,193,250`) и его
  чтение (`:385`); убрать `enable_cron` из composition (`:159,253,315`);
  исправить docstring `_make_cron_service` (`:1727`).
- `cli_agent.py` — удалить `_run_vanilla`/`_run_patched`/`_run_patched_repl`
  и вызовы `ApplicationContext.create` (`:125,151,175`); удалить
  `_migrate_cron_store` (`:232-243`) и `_get_cron` (`:212-215`); убрать флаги
  `--storage`/`--patched`.
- `lib/channels/cli_channel.py` — **новый** клиент Gateway-транспорта.
- `lib/cli/console_loop.py` — принимать transport вместо `agent`; завершать
  turn по `turn_end`; делегировать `/compact`; убрать локальный
  `ContextCompactionService` (`:124-142,359-361`).
- `lib/services/channel_factory.py` — без изменений в протоколе; остаётся
  единственным местом создания каналов (Gateway).
- `lib/utils/outbound_meta.py` — без изменений: используется на стороне
  Gateway, у клиента свои wire-кадры.

**Конфигурация (новые опциональные ключи, все с дефолтами):**

- `cli.gateway_url` — адрес Gateway для клиента; по умолчанию
  `ws://<channels.websocket.host>:<channels.websocket.port>` (тот же порт,
  что проверяет `gateway.py:460-511`; второго порта не вводится).
- `cli.gateway_connect_timeout_sec` (дефолт 10.0) — таймаут handshake.

Переключателя bootstrap НЕ вводится: `GET /webui/bootstrap` поднимается
upstream безусловно (`nanobot/webui/ws_http.py:569,665-680`), а флаг
`false` означал бы недокументированный обход аутентификации, что запрещено
требованием `runtime/cli-client`.

**Тесты:** новые `tests/test_cli_channel_client.py`,
`tests/test_cli_turn_lifecycle.py`; обновление
`tests/test_application_context_role.py` (переименование в
`tests/test_application_context_composition.py`), `tests/test_gateway_live_media_e2e.py`,
`tests/test_runtime_health.py`, тестов, импортирующих `console_loop`.

**Документация:** `docs/ARCHITECTURE.md`, `AGENTS.md` (секции «Project
Layout» и «Configuration», исправление устаревшего «`gateway /health`»),
`README.md`, `CHANGELOG.md`, `docs/PROFILES.md` (роль CLI).

## Out of Scope

- Cache lifecycle, ownership, heartbeat, fencing, shutdown ordering —
  change `cache-architecture-alignment`.
- Процессная граница skill ↔ cache — change `fix-cache-process-boundary`.
- Удаление `streamlit_app.py` — change `remove-streamlit-runtime`.
- Новый HTTP/OpenAPI-эндпойнт для CLI: WebSocket-транспорт уже поднят и
  документирован; второй транспорт означал бы два протокола для одной
  задачи.
- Внедрение upstream TypeScript TUI (`nanobot/cli/tui_launcher.py`): он
  требует отдельного бинарника и дублировал бы `lib/cli/console_loop.py`.
- Правка внутренностей `nanobot` (включая литералы имён каналов в
  `AgentLoop`): это ответственность upstream; здесь фиксируется
  проверяемая формулировка вместо невыполнимого требования.
