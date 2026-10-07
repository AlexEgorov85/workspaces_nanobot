## Purpose

Нормативный контракт CLI как клиента Gateway: по какому транспорту он
подключается, как аутентифицируется, как адресует сессию, как определяет
завершение turn'а и что делает, когда Gateway недоступен.

Capability `runtime/entrypoints` описывает, кто владеет composition root и
какие компоненты создаются. Эта capability описывает поведение клиента.
Деление принципиально: без него «CLI — клиент» остаётся намерением, а
не проверяемым контрактом, и следующий разработчик вернёт локальный
runtime под видом «удобного отладочного режима».

## Responsibility

CLI client отвечает за:

- подключение к существующему WebSocket-транспорту Gateway и handshake;
- аутентификацию существующими средствами Gateway (без ослабления);
- адресацию сессии (`chat_id`);
- приём и проекцию wire-кадров на объекты рендера;
- определение завершения turn'а по типизированному кадру `turn_end`;
- делегирование slash-команд Gateway;
- диагностику недоступного Gateway и явную индикацию потери соединения.

## Boundary

### Owns

- клиентским подключением и его lifecycle (connect, read-loop, reconnect);
- разбором и валидацией входящих wire-кадров;
- таймаутом handshake и connection watchdog'ом;
- адресом Gateway по умолчанию (сборка из `channels.websocket.*`);
- пользовательской диагностикой клиента.

### Does Not Own

- Agent Runtime, `AgentLoop`, tools, skills, memory, patches — это Gateway
  (`runtime/entrypoints`);
- обработки сообщений, команд и turn'а — это Gateway;
- конфигурации WebSocket-сервера и его аутентификации;
- lifecycle БД, сессий, cron, cache — это Gateway.

### Must Not Depend On

- локального `ApplicationContext`, `AgentLoop` или `MessageBus`;
- прямого подключения к PostgreSQL, DuckDB, FAISS или cache-файла;
- внутренних модулей Gateway (`lib/services/*`, `lib/core/*`) — клиент
  зависит только от wire-контракта;
- конкретного WebSocket-клиента: выбор библиотеки — implementation detail,
  нормативен протокол.

## ADDED Requirements

### Requirement: CLI использует существующий WebSocket-транспорт Gateway

CLI MUST подключаться к Gateway по уже поднятому WebSocket-транспорту
(`WebSocketChannel`, `nanobot/channels/websocket/runtime.py:363`),
регистрируемому `ChannelManager` по умолчанию и поднимаемому при старте
Gateway.

Клиент MUST реализовывать wire-протокол этого транспорта:

- **входящее**: `{"type": "message", "chat_id": ..., "content": ..., "media": ...}`
  (`nanobot/webui/inbound_commands.py:467,476-504`) — после успешного
  handshake кадр публикуется в bus Gateway как
  `InboundMessage(channel="websocket", ...)`
  (`nanobot/channels/base.py:310-321`);
- **исходящее**: `ready`, `delta`, `stream_end`, `message`
  (`kind` = `tool_hint` / `progress`), `reasoning_delta`, `reasoning_end`,
  `turn_end`, `error`.

Второй транспорт для связи CLI с Gateway MUST NOT вводиться: отдельный
HTTP/OpenAPI-эндпойнт означал бы два протокола для одной задачи и вторую
схему аутентификации.

Поллинг-движок `MessageExchange` (`lib/channels/message_exchange.py:135-171`)
MUST NOT использоваться клиентом: он реализует очередь-семафор и слоты с
backoff для транспортов, опрашиваемых по расписанию. WebSocket — push-транспорт
с read-loop. Допустимо переиспользование кодека медиа
(`lib/utils/media.py`) и фильтрации служебного шума
(`lib/utils/outbound_meta.py`).

#### Scenario: клиент использует поднятый Gateway-транспорт

- **WHEN** запускается `cli_agent.py` при работающем Gateway
- **THEN** клиент MUST подключиться к порту из `channels.websocket.port`
- **AND** Gateway MUST принять его сообщения через свой
  `WebSocketChannel` и передать их в bus как `InboundMessage`

#### Scenario: второй транспорт не вводится

- **WHEN** выполняется инвентаризация способов связи CLI с Gateway
- **THEN** MUST быть ровно один транспорт — WebSocket
- **AND** новых HTTP-эндпойнтов для CLI MUST NOT появляться

#### Scenario: поллинг-движок не используется клиентом

- **WHEN** проверяются импорты в модуле клиента
- **THEN** `MessageExchange` MUST NOT импортироваться
- **AND** клиент MUST иметь read-loop поверх WebSocket-соединения

### Requirement: handshake аутентифицирован и не ослабляет конфигурацию

Клиент MUST выполнять handshake штатными средствами Gateway:

1. получить токен через `GET /webui/bootstrap` (localhost без секрета —
   `nanobot/webui/ws_http.py:668-680`; с секретом — с
   `Authorization`/`X-Nanobot-Auth`);
2. подключиться к `ws://<host>:<port><ws_path>?token=<token>`;
3. дождаться кадра `{"event": "ready"}`.

Клиент MUST NOT отключать аутентификацию (`websocket_requires_token` MUST NOT
выставляться в `false` ради клиента): это ослабило бы периметр для всех
локальных WebSocket-клиентов проекта. Клиент MUST NOT обходить
`authorize_websocket_handshake` и MUST NOT повторять попытку с ослабленными
условиями после отказа.

Таймаут handshake MUST ограничиваться конфигурацией
(`cli.gateway_connect_timeout_sec`).

#### Scenario: bootstrap-токен используется для handshake

- **WHEN** клиент подключается к Gateway
- **THEN** он MUST получить токен через `/webui/bootstrap`
- **AND** передать его в query-параметре `token` при WebSocket-подключении
- **AND** соединение MUST считаться установленным только после кадра `ready`

#### Scenario: отказ аутентификации диагностируется

- **WHEN** handshake отклонён (невалидный или отсутствующий токен)
- **THEN** клиент MUST сообщить об отказе аутентификации
- **AND** клиент MUST NOT повторять попытку без токена
- **AND** процесс MUST завершиться с ненулевым кодом

#### Scenario: аутентификация не ослабляется ради клиента

- **WHEN** проверяется конфигурация `channels.websocket`
- **THEN** `websocket_requires_token` MUST оставаться включённым
- **AND** клиент MUST NOT изменять настройки аутентификации Gateway

### Requirement: сессия адресуется однозначно

Клиент MUST включать идентификатор сессии в каждое исходящее сообщение
(`chat_id`), чтобы Gateway разрешал его в ту же сессию, что и остальные
клиенты этого чата. Источник идентификатора — `--session` или его
конфигурационный эквивалент; при отсутствии — детерминированный default.

#### Scenario: chat_id приходит в каждом сообщении

- **WHEN** клиент отправляет сообщение
- **THEN** кадр MUST содержать `chat_id` сессии
- **AND** Gateway MUST разрешать его в ту же сессию, что и Postgres-канал

#### Scenario: --session меняет идентификатор сессии

- **WHEN** пользователь запускает `python cli_agent.py --session=work`
- **THEN** все исходящие кадры MUST содержать `chat_id` этой сессии

### Requirement: turn завершается кадром turn_end

Завершение turn'а MUST определяться кадром `turn_end`
(`nanobot/webui/outbound_projection.py:227-244` →
`nanobot/webui/outbound_wire.py:198-231`), который проецирует внутренний
`TurnEndEvent` и несёт `latency_ms`, `usage`, `round_usages`,
`context_window_tokens`, `outcome`, `failure_kind`, `failure_attempts`,
`failure_message`.

Клиент MUST NOT определять завершение turn'а по первому
`StreamedResponseEvent`/`message`: внутри одного turn'а может приходить
несколько сообщений, и «первый финал» не равен завершению.

Проекция wire-кадров на объекты рендера MUST выполняться на границе клиента:

| Wire-кадр | Проекция | Действие |
|---|---|---|
| `delta` | `StreamDeltaEvent` | вывод в рендер потока |
| `reasoning_delta` / `reasoning_end` | reasoning-события | буфер рассуждения |
| `stream_end` | `StreamEndEvent` | закрыть рендер потока |
| `message` | `StreamedResponseEvent` | вывести сообщение |
| `turn_end` | — | **завершить turn**, вывести `latency_ms`, `outcome`, `failure_kind` |

Проецированные объекты MUST NOT публиковаться в bus: у клиента нет
`AgentLoop`, а проекция существует только для рендера.

`turn_wait_timeout` MUST трактоваться как **connection watchdog** (долго нет
кадров), а не как сигнал завершения turn'а и не как «агент недоступен».
Watchdog MUST давать отличное от «Gateway недоступен» сообщение.

#### Scenario: turn завершается по turn_end

- **WHEN** Gateway прислал `message` и затем `turn_end`
- **THEN** клиент MUST завершить turn после `turn_end`
- **AND** MUST вывести `latency_ms` и `outcome`

#### Scenario: несколько message в одном turn

- **WHEN** в рамках одного turn'а приходят `message`, `message`, `turn_end`
- **THEN** клиент MUST вывести все сообщения
- **AND** MUST завершить turn только по `turn_end`

#### Scenario: отсутствие turn_end — watchdog, а не завершение

- **WHEN** `turn_end` не пришёл в течение `turn_wait_timeout`
- **THEN** клиент MUST сообщить, что turn не завершён, и что соединение
  молчит дольше watchdog
- **AND** клиент MUST NOT сообщать «Gateway недоступен», если соединение
  активно

#### Scenario: turn завершился ошибкой

- **WHEN** `turn_end` содержит `outcome` с признаком отказа
- **THEN** клиент MUST отобразить это иначе, чем обычный ответ
- **AND** MUST NOT выдавать отказ за успешный результат

### Requirement: недоступный Gateway диагностируется

Если подключиться к Gateway не удалось, клиент MUST завершиться с ненулевым
кодом выхода и сообщением, содержащим:

- адрес, к которому он пытался подключиться;
- команду запуска Gateway (`python gateway.py --profile=<profile>`).

Клиент MUST NOT автозапускать Gateway. Upstream-механизм on-demand
(`GatewayClientLease.ensure_on_demand_gateway`,
`nanobot/cli/tui_launcher.py:482`) запускает `nanobot gateway <sub> --config
<path>` (`nanobot/cli/webui_support.py:665-678`) — это **другой** runtime без
проектных каналов, патчей и tool'ов; молчаливый запуск чужого процесса
хуже явной ошибки.

#### Scenario: Gateway не запущен — явная ошибка

- **WHEN** клиент не может подключиться к адресу Gateway
- **THEN** он MUST вывести сообщение с адресом подключения и командой
  запуска Gateway
- **AND** MUST завершиться с ненулевым кодом выхода
- **AND** MUST NOT запускать Gateway автоматически

#### Scenario: нулевой код выхода означает подключение

- **WHEN** клиент завершает работу
- **THEN** код выхода `0` MUST соответствовать успешному подключению и
  штатному завершению сессии

### Requirement: reconnect видим, а не тихий

Потеря соединения после успешного handshake MUST отображаться в REPL явной
индикацией, а reconnect — выполняться ограниченным числом попыток с
backoff. Тихий retry без индикации запрещён: он неотличим от зависания и
продлевает ожидание пользователя.

#### Scenario: потеря соединения индицируется

- **WHEN** соединение с Gateway разрывается во время работы
- **THEN** REPL MUST показать, что соединение потеряно
- **AND** MUST NOT выглядеть как процесс, который «думает»

#### Scenario: reconnect ограничен

- **WHEN** reconnect не удаётся после исчерпания попыток
- **THEN** клиент MUST сообщить об этом и завершиться с ненулевым кодом

### Requirement: CLI не эмулирует локальный runtime

Модуль клиента MUST NOT содержать локального `AgentLoop`, локального
`MessageBus`, локального `ApplicationContext` или прямого доступа к
хранилищам (PostgreSQL, DuckDB, FAISS). Если функция недоступна по
wire-протоколу, это либо не требуется, либо требует расширения протокола —
но не локальной реализации «на всякий случай».

#### Scenario: в клиенте нет локального runtime

- **WHEN** выполняется поиск `AgentLoop`, `MessageBus`, `ApplicationContext`,
  `duckdb`, `psycopg` в модулях клиента
- **THEN** совпадений MUST NOT быть

#### Scenario: проекция событий не публикуется в bus

- **WHEN** клиент проецирует wire-кадр на объект рендера
- **THEN** объект MUST использоваться только для вывода
- **AND** публикации в bus MUST NOT происходить, так как у клиента bus
  отсутствует

### Requirement: slash-команды исполняются на Gateway

Клиент MUST отправлять slash-команды как обычное сообщение (`content`),
и MUST NOT исполнять их локально. `AgentLoop` Gateway обрабатывает команды
для любого канала, кроме `"system"`
(`nanobot/agent/loop.py:1342`), поэтому `/compact`, `/help` и прочие
обрабатываются в Gateway, включая проектные патчи команд.

Локальное исполнение команд, требующих сервисов Gateway
(`ContextCompactionService`, cron-хранилище), запрещено: у клиента этих
сервисов нет, а локальная запись в сессию означала бы второй писатель.

#### Scenario: команда отправляется в Gateway

- **WHEN** пользователь вводит slash-команду
- **THEN** клиент MUST отправить её как `content` без локальной обработки
- **AND** Gateway MUST обработать команду своим command router'ом

#### Scenario: локальное исполнение команд запрещено

- **WHEN** проверяется код клиента на вызовы Gateway-сервисов
- **THEN** вызовов `ContextCompactionService`, `CronService` и прочих
  Gateway-сервисов MUST NOT быть
