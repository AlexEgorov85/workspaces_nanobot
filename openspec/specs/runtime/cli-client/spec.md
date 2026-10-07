# runtime/cli-client Specification

## Purpose

Нормативный контракт CLI как клиента Gateway: по какому транспорту он
подключается, как аутентифицируется, как адресует сессию, как определяет
завершение turn'а и что делает, когда Gateway недоступен.

Capability `runtime/entrypoints` описывает, кто владеет composition root и
какие компоненты создаются. Эта capability описывает поведение клиента.
Деление принципиально: без него «CLI — клиент» остаётся намерением, а
не проверяемым контрактом, и следующий разработчик вернёт локальный
runtime под видом «удобного отладочного режима».

## Scope

`agent` — поведение CLI-клиента и его wire-контракт
Реализация: `cli_agent.py`, `lib/channels/cli_channel.py`, `lib/cli/console_loop.py`

## Requirements

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

## Public Contract

Единственный модуль, который говорит на wire-протоколе, — клиентский канал;
он и есть публичная поверхность этой capability:

- `lib/channels/cli_channel.py::CliChannel` — подключение, handshake,
  read-loop, reconnect. Появление модуля объявлено change
  `unify-runtime-channels` (задача 2.1); до его появления на диске его нет,
  и это состояние дерева, а не дыра в контракте.
- `run_repl(transport, ...)` вместо `run_repl(agent, ...)` — REPL получает
  транспорт, а не `AgentLoop`; `asyncio.create_task(agent.run())` и
  `agent.bus` из текущего тела REPL уходят.

Наблюдаемая поверхность — сам протокол, а не сигнатуры Python: клиент
обязан говорить ровно тем, что понимает штатный `WebSocketChannel`, потому
что на той стороне кода нет.

## Inputs

- `argv` — `--session`; флаги `--storage`/`-S` и `--patched`/`-P` снимаются
  вместе с локальным runtime (change `unify-runtime-channels`, п. 4.3),
  `--profile` CLI не принимает вовсе (фиксирован `test`).
- Адрес Gateway — `cli.gateway_url` либо сборка из `channels.websocket.host`
  и `channels.websocket.port` (`nanobot/channels/websocket/runtime.py:197-198`).
- Токен — JSON-ответ `GET /webui/bootstrap`: `{"token": ..., "expires_in": ...}`.
- Входящие wire-кадры — `ready`, `delta`, `stream_end`, `message`,
  `reasoning_delta`, `reasoning_end`, `turn_end`, `error`.
- Пользовательский ввод REPL — обычный текст и slash-команды.

## Outputs

- Исходящие кадры `{"type": "message", "chat_id": ..., "content": ...}`
  (`nanobot/webui/inbound_commands.py:467,476-504`).
- Проекции кадров в объекты рендера REPL: `StreamDeltaEvent`,
  `StreamEndEvent`, `StreamedResponseEvent`, reasoning-события.
- Итог turn'а на экране: `latency_ms`, `outcome`, `failure_kind`.
- Код выхода процесса: `0` — соединение установлено и сессия завершена
  штатно; ненулевой — handshake отклонён, Gateway недоступен, reconnect
  исчерпан.
- Ни файлов, ни записей в БД, ни сетевых вызовов кроме WebSocket и одного
  `GET /webui/bootstrap` клиент не производит.

## State

Клиент хранит только состояние своей сессии в памяти процесса:

- `chat_id` активной сессии (из `--session` или детерминированный default) —
  единственное, что идентифицирует разговор на проводе;
- счётчик попыток reconnect и момент последнего кадра — по ним работает
  connection watchdog;
- время жизни bootstrap-токена: он короткоживущий, и клиент перезапрашивает
  его на каждое подключение, а не кэширует между процессами;
- read-loop как таковая: push-транспорт держит соединение, а не опрашивает
  по расписанию.

Долговременного состояния нет. Всё, что переживает перезапуск (история
сессии, compaction, cron, логи), принадлежит Gateway — писать в него клиент
не имеет права, иначе он стал бы вторым писателем.

## Dependencies

Разрешено:

- wire-контракт upstream нанобота: `nanobot/channels/websocket/runtime.py`,
  `nanobot/webui/inbound_commands.py`, `nanobot/webui/outbound_wire.py`,
  `nanobot/webui/outbound_projection.py`, `nanobot/webui/ws_http.py`;
- кодек медиа `lib/utils/media.py`;
- фильтрация служебного шума `lib/utils/outbound_meta.py`;
- WebSocket-библиотека — выбор implementation detail, нормативен протокол.

Запрещено:

- `lib/channels/message_exchange.py` — движок опрашиваемых по расписанию
  транспортов, а не push-транспорта;
- `lib/core/application_context.py`, `lib/core/agent_factory.py`,
  `lib/services/*` — внутренние модули Gateway: клиент зависит только от
  wire-контракта.

## Configuration

- `channels.websocket.host` / `channels.websocket.port` — адрес Gateway;
  дефолты upstream `127.0.0.1` и `8765`
  (`nanobot/channels/websocket/runtime.py:197-198`).
- `channels.websocket.path` — путь WS-upgrade, дефолт `/`
  (`nanobot/channels/websocket/runtime.py:200`).
- `channels.websocket.websocket_requires_token` — дефолт `True`
  (`nanobot/channels/websocket/runtime.py:207`). Клиент MUST NOT выключать
  его: это ослабило бы периметр для всех локальных WebSocket-клиентов, а не
  только для CLI.
- `channels.websocket.token` / `channels.websocket.token_issue_secret` —
  штатный путь выдачи токена, когда bootstrap отвечает только с секретом
  (`Authorization: Bearer <secret>` или `X-Nanobot-Auth`).
- `cli.gateway_url` — явный адрес вместо собранного из `channels.websocket.*`.
- `cli.gateway_connect_timeout_sec` — потолок handshake; без него клиент
  ждал бы ровно столько, сколько решит сеть.
- `cli.turn_wait_timeout_sec` — дефолт `300.0`
  (`lib/cli/console_loop.py:247-252`). Ключ не переименовывается, его
  **смысл** меняется: из предела ожидания финального ответа в connection
  watchdog.
- `gateway.agent.enterprise_mcp`, `tools.mcpServers` и прочие блоки
  клиенту не читаются: это конфигурация Gateway, а не клиента.

## Lifecycle

1. Разбор `argv`, фиксация `chat_id` сессии.
2. Разрешение адреса: `cli.gateway_url` либо сборка из `channels.websocket.*`.
3. `GET /webui/bootstrap` — токен (с секретом — заголовок авторизации).
4. WebSocket-подключение с `?token=<token>`, ожидание `{"event": "ready"}`
   под `cli.gateway_connect_timeout_sec`. Кадр `ready` — момент, после
   которого соединение считается установленным; до него оно не считается.
5. Read-loop: разбор кадров → проекция → рендер; `turn_end` закрывает
   turn, молчание дольше watchdog'а даёт сообщение о watchdog.
6. Потеря соединения после `ready`: явная индикация + ограниченный reconnect
   с backoff, каждый переход — новый handshake (шаги 3–4 повторяются).
7. Завершение: `0` при штатном выходе из REPL, ненулевой — по любому из
   перечисленных выше отказов.

Lifecycle не применяется к Gateway: клиент не поднимает каналы, не мигрирует
cron-хранилище и не собирает composition root.

## Data Ownership

Клиент не владеет никакими долговременными данными. Единственное
производное — токен handshake и `chat_id`, оба живут в памяти процесса до
его завершения.

Всё содержимое сессии — сообщения, reasoning, compaction-заметки, журнал
оборотов — принадлежит Gateway и его хранилищам (`runtime/db-logging`,
`runtime/session-files`). Повторная запись в них из клиента была бы вторым
писателем, и именно поэтому локальное исполнение slash-команд запрещено.

## Error Behavior

- **Handshake отклонён** (невалидный или отсутствующий токен): сообщение об
  отказе аутентификации, ненулевой код выхода. Повтор без токена и повтор с
  ослабленными условиями запрещены — отказ аутентификации это ответ
  сервера, а не повод искать обход.
- **Gateway недоступен**: сообщение с адресом подключения и командой запуска
  Gateway, ненулевой код выхода, автозапуск запрещён.
- **Watchdog сработал**: сообщение о том, что turn не завершён и соединение
  молчит дольше watchdog. Формулировка MUST отличаться от «Gateway
  недоступен», пока соединение живо.
- **Reconnect исчерпан**: сообщение об этом и ненулевой код выхода.
- **Неизвестный `event`**: клиент не знает будущих типов кадров, поэтому
  незнакомый кадр — не повод ронять соединение; он отбрасывается с
  диагностикой.

## Invariants

- Транспорт ровно один: WebSocket. Второго способа связи CLI с Gateway в
  дереве нет, и добавление HTTP-эндпойнта для CLI нарушает инвариант.
- `turn_end` — единственный терминатор turn'а. Ни `message`, ни
  `stream_end`, ни тишина по таймауту turn не завершают.
- `chat_id` присутствует в каждом исходящем кадре: без него Gateway
  разрешает сессию иначе, чем остальные клиенты того же чата.
- Соединение считается установленным только после `ready`.
- Аутентификация Gateway не ослабляется ради клиента: клиент —
  потребитель штатного периметра, а не исключение из него.
- Потеря соединения всегда видна в REPL; тихий retry запрещён.
- Проецированные события не публикуются в bus: у клиента bus нет.
- Ноль кода выхода означает подключение и штатное завершение, а не «не
  разобрались».

## Forbidden Behavior

Клиент НЕ ДОЛЖЕН:

- заводить локальный `AgentLoop`, `MessageBus` или `ApplicationContext`;
- обращаться к PostgreSQL, DuckDB, FAISS и файлу кэша;
- импортировать `MessageExchange` — движок для опрашиваемых транспортов;
- вводить второй транспорт или HTTP-эндпойнт для связи с Gateway;
- выставлять `websocket_requires_token` в `false`, обходить
  `authorize_websocket_handshake` или повторять handshake с ослабленными
  условиями;
- определять завершение turn'а по первому `message`/`StreamedResponseEvent`;
- выполнять slash-команды локально, в том числе `/compact` через
  `ContextCompactionService`;
- публиковать проецированные wire-события в bus;
- автозапускать Gateway, в том числе upstream-механизмом on-demand;
- повторять подключение после отказа аутентификации;
- молча переподключаться без индикации в REPL.

## Consumers

- `cli_agent.py::_run_cli_repl` (`cli_agent.py:207`) — единственная точка,
  собирающая клиент и передающая его в REPL; handshake платформы и REPL
  идут в одном живом loop.
- `lib/cli/console_loop.py::run_repl` — потребитель транспорта вместо
  `AgentLoop`: рисует дельты, reasoning, сообщения и итог turn'а.
- Пользователь в терминале — единственный заказчик поведения: видит
  потерю соединения, watchdog и отказ Gateway.
- Соседние capability — `runtime/entrypoints` (кто владеет composition root
  и составом компонентов); требования этой capability, помеченные D13,
  снимаются вместе с `unify-runtime-channels`.

## Implementation

Существующие на диске пути, на которых держится этот контракт:

- `cli_agent.py` — argv, запуск, передача транспорта в REPL; клиентский
  модуль `lib/channels/cli_channel.py` вводится этим же change и на диске
  пока отсутствует;
- `lib/cli/console_loop.py` — REPL и рендер; `turn_wait_timeout` и его
  сообщение о таймауте переезжают в watchdog (`:247-252`, `:409-413`);
- `lib/utils/media.py`, `lib/utils/outbound_meta.py` — переиспользуемые
  части клиента;
- wire-контракт на стороне Gateway — `nanobot/channels/websocket/runtime.py`,
  `nanobot/webui/inbound_commands.py`, `nanobot/webui/outbound_wire.py`,
  `nanobot/webui/outbound_projection.py`, `nanobot/webui/ws_http.py`.

## Verification

- `tests/test_cli_channel_client.py` — контрактные тесты разбора кадров без
  сети, включая `error` и неизвестный `event` (change
  `unify-runtime-channels`, п. 2.7);
- `tests/test_cli_turn_lifecycle.py` — завершение turn'а по `turn_end` при
  нескольких `message` в одном turn и отсутствие завершения без `turn_end`
  (п. 3.8);
- `python tools/check_spec_citations.py openspec/specs/runtime/cli-client/spec.md`
  — все ссылки `путь:строка` этой спеки ведут в существующие непустые
  строки;
- `python tools/validate_component_specs.py` — полный шаблон: 19 разделов,
  непустые тела, сценарий с маркерами в каждом требовании.

Честно о состоянии: change `unify-runtime-channels` **не применён** — из 51
задачи выполнено 5, модуль `lib/channels/cli_channel.py` на диске
отсутствует, а `cli_agent.py` по-прежнему строит локальный runtime
(`ApplicationContext.create(role='cli')`, `cli_agent.py:189`). Требования
выше — контракт целевого состояния, а не описание работающего кода; стражей
проверки поведения клиента в дереве пока нет, они появляются вместе с п. 2.7
и 3.8. Проверяемо сегодня — структура спеки, разрешимость всех ссылок и
тот факт, что протокол на стороне Gateway (upstream) существует и не
противоречит описанному.