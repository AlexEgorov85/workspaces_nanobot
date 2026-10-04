> ## Статус: контракт пересобран 2026-10-04 — ожидает `enterprise-mcp-http-transport` (D13)
>
> Пометка NEEDS-REWORK снята 2026-10-04. Ниже сохранено только то, что после
> пересборки ещё верно и что следующему исполнителю не следует выяснять
> заново. **Удалять нельзя: направление не устарело, а стало дороже.**
>
> ### Что проверено на живом коде 2026-10-02 и держится
>
> | Факт change'а | Состояние |
> |---|---|
> | Ф1 `create(role=...)`, `ctx.role` — двойное назначение | держится: `ctx.role` присваивается (`application_context.py:296`) и читается в cron-гейте (`:406`) |
> | Ф2 каналы создаются мимо composition root | держится: `gateway.py` дергает `create_all` сам |
> | Ф3 `AgentLoop` не transport-agnostic | держится: 3 сравнения `msg.channel` в `nanobot/agent/loop.py` |
> | Ф4 CLI завершает turn локально, не по `turn_end` | держится: `_consume_outbound` + `turn_done.set()`, слова `turn_end` в `console_loop.py` нет |
> | Ф5 `TurnEndEvent` отдаётся в wire-кадре `turn_end` | держится: `outbound_wire.py` и `outbound_projection.py` на месте |
> | Ф6 `WebSocketChannel` есть в `nanobot`, но в `config.json` не включён | держится: `default_enabled=True` в манифесте, в нашем конфиге секции нет |
> | Ф7 Gateway блокируется на stdin | держится |
>
> Привязки `file:line` разъехались на единицы строк (`return_file_manager`
> 315→316, `turn_wait_timeout` 233→235). Это следствие сноса кластера кэша, а
> не смены архитектуры.
>
> **Правка привязки от 2026-10-02 была неверной и откатывается.** П. 5.5
> указывал на `application_context.py:1727`, и это было верно. Правка
> 2026-10-02 объявила `:1727` протухшим и подставила `:379`, но `:379` — это
> комментарий про `mcp-platform/platform.json`, а не `_make_cron_service`.
> Функция живёт на `:1584`, её docstring — на `:1585`, и docstring **до сих
> пор** утверждает «нужен для CLI-режима», то есть расхождение с гейтом
> `if ctx.enable_cron and ctx.role == "gateway"` осталось неразобранным.
> Привязка восстановлена в `tasks.md` п. 5.5.
>
> ### Основание остаётся, изменилась его цена (решение Р5)
>
> Change написан до миграции `enterprise-mcp`. С тех пор агент получил
> stdio-клиента платформы, и `ApplicationContext.create()` **безусловно**
> строит его:
>
> ```
> application_context.py:502   ctx.enterprise_mcp = _make_enterprise_mcp(ctx.settings, ctx)
> ```
>
> Гейта по `role` там нет. Значит `cli_agent.py`, поднимая свой
> `ApplicationContext`, поднимает **второй процесс `enterprise-mcp`**:
> второе рукопожатие и вторая сессия capability.
>
> Прежняя формулировка обоснования упиралась в «второго владельца пула
> PostgreSQL» (запрет `AGENTS.md` при обосновании `EnterpriseMcpClient`).
> 2026-10-04 этот аргумент **ослаб**: change
> `2026-10-02-task-queue-into-mcp` снимает второй пул в агенте, перенося
> работу с задачами в операции capability `data`. Изменилась цена, а не
> основание: двойной `AgentLoop`, двойной `SessionManager`, двойные хуки и
> патчи и **двойной процесс `enterprise-mcp`** остаются. Направление
> change'а («CLI — тонкий WebSocket-клиент, `ApplicationContext` в CLI не
> поднимается») по-прежнему убирает и новую проблему, поэтому переделывать
> надо, а не удалять.
>
> ### Решения, принятые при пересборке 2026-10-04
>
> 1. **Сессия `enterprise-mcp` — одна по контракту, а не по стечению
>    обстоятельств.** Дельта фиксирует это нормативно (сценарий «только
>    Gateway вызывает composition root»), а не оставляет следствием того, что
>    CLI перестал звать `create()`.
> 2. **Р1 — добавлена дельта `runtime/context`.** `role` нормативен не только
>    в `runtime/entrypoints`: `openspec/specs/runtime/context/spec.md:61,83`
>    закрепляет «различие только в обязательном kwarg `role`». Дельта только
>    `runtime/entrypoints` оставила бы висящее требование к
>    несуществующему параметру, поэтому `runtime/context` тоже несёт
>    `MODIFIED`.
> 3. **Р2 — долг `data/cache-provider` сюда не втягивается.** Канонизация
>    cache-контракта **не состоялась**: change `cache-architecture-alignment`
>    не существует ни в `changes/`, ни в `archive/`, а его реальные потомки
>    (`2026-10-02-drop-local-cache-read-from-pg`,
>    `2026-10-02-fix-cache-process-boundary`, оба архивированы) не имеют
>    каталога `specs/` вообще. Утверждение прежней версии change'а — «A → B
>    → C решает обе проблемы: B снимает дубли, C удаляет `role`» —
>    **не подтверждается**. Дубликаты cache-контракта в `runtime/entrypoints`
>    остаются; лечатся они отдельным change, потому что здесь уже несутся
>    `runtime/entrypoints` и `runtime/cli-client`. Долг зафиксирован в
>    `## Out of Scope` и в `design.md` (Decision 9).
> 4. **Р3 — три D13-обязательства снимаются фазой 5 этого change**, а не
>    молча. См. `tasks.md` п. 5.9 и `REMOVED`-блок в
>    `specs/runtime/entrypoints/spec.md`.
> 5. **Р4 — примеры канала не ждут `task-queue-into-mcp`, а помечены
>    «после `2026-10-02-task-queue-into-mcp`»** — тем же приёмом «после X»,
>    что уже применён в D13.
> 6. **Р5 — мотивация переписана в ключе «цена изменилась, основание
>    нет»**, обоснование не выброшено.
>
> ### Порядок (D13)
>
> Этот change идёт **после** `2026-10-04-enterprise-mcp-http-transport`:
> тот **добавляет** в `runtime/entrypoints` три требования, держащихся на
> предпосылке «CLI поднимает собственный процесс платформы», и сам помечает
> их «до `unify-runtime-channels`»
> (`2026-10-04-enterprise-mcp-http-transport/design.md:341-361`). Фаза 5
> этого change снимает все три.
>
> Прежний арбитр («не брать, пока `enterprise-mcp-platform` фазы 8 не дойдёт
> до устойчивого состояния») протух: фаза 8 давно позади, а решение принято и
> записано — D13.
>
> ### Известный долг, не входящий в этот change
>
> `runtime/entrypoints` по-прежнему содержит четыре требования про cache
> (ownership, fencing, layered API, `CacheSyncService`), которые дублируют
> `data/cache-provider` и `data/cache-runtime-lifecycle` и расходятся с ними
> по заголовкам. **Требуется отдельный change** — здесь не лечится (Р2).

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
`:406` (гейт CronService), identity claim'а cache снят архивным change
`2026-10-02-drop-local-cache-read-from-pg`.

**Факт 2 — таблица composition-сервисов в канонической спеке неверна.**
`openspec/specs/runtime/entrypoints/spec.md:164` утверждает, что
`PostgresChannel` создаётся `ApplicationContext` при `role="gateway"`.
Фактически канал создаёт `ChannelFactory._add_postgres`
(`lib/services/channel_factory.py:129-191`), который вызывается из
`gateway.py:249-258` — то есть **вне** `ApplicationContext`. Спека
приписывает composition тому, кто её не выполняет.

Примеры «канал = `PostgresChannel`, свой пул PostgreSQL, 33 SQL-глагола»,
которые фигурируют в этом change'е, устаревают прямо сейчас: change
`2026-10-02-task-queue-into-mcp` переводит канал на операции capability
`data`. По решению Р4 ждать его не нужно: в дельтах такие примеры помечены
«после `2026-10-02-task-queue-into-mcp»». Само утверждение («канал создаёт
`ChannelFactory`, а не `ApplicationContext`») переживает эту правку — меняется
не то, кто создаёт канал, а то, чем он является.

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
(`application_context.py:406`), при этом docstring `_make_cron_service`
(`:1585`, сама функция на `:1584`) утверждает обратное («нужен для
CLI-режима») — расхождение документации с кодом. Правка привязки от
2026-10-02 (`:1727` → `:379`) была ошибочной: `:379` — комментарий про
`mcp-platform/platform.json`.

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
   делается здесь, а не «после того, как понадобится». `role` нормативен не
   только в `runtime/entrypoints`: его закрепляет и `runtime/context`
   (`openspec/specs/runtime/context/spec.md:61,83`), поэтому дельта
   `runtime/context` входит в этот change (решение Р1) — иначе снос
   параметра оставил бы висящее требование.
3. **Cron MUST стать свойством Gateway, а не entrypoint-флага.**
   `enable_cron` перестаёт быть переключателем composition (`application_context.py:159,253,315,406`);
   `_make_cron_service` docstring (`:1585`; сама функция `:1584`) приводится в
   соответствие с кодом.
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
   остаётся локальной`. Дубликаты
   cache-контракта из `runtime/entrypoints` **не трогаются здесь**: канонизация
   не состоялась (Р2 в шапке), предшественник `cache-architecture-alignment`
   не существует ни в `changes/`, ни в `archive/`, а его потомки не имеют
   каталога `specs/`. Это известный долг, он требует отдельного change.
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

- `runtime/context` — дельта из 1 требования (решение Р1):
  `Единый корень общей инфраструктуры` перестаёт требовать `role` и
  `storage_override` от CLI; все четыре канонических заголовка сценариев
  сохранены, тела скорректированы.
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

**Порядок (D13).** Этот change идёт **после**
`2026-10-04-enterprise-mcp-http-transport` — того, который **добавляет** три
требования `runtime/entrypoints` на предпосылке «CLI поднимает собственную
платформу» и сам помечает их «до `unify-runtime-channels`»
(`2026-10-04-enterprise-mcp-http-transport/design.md:341-361`). Фаза 5
этого change снимает все три явно (п. 5.9 в `tasks.md`).

Оба archive-предшественника по кэшу уже выполнены и стоят **перед** этим
change. Прежние имена `cache-architecture-alignment` и
`fix-cache-process-boundary` в репозитории **не существуют**; ниже — их
реальные преемники:

- **Закрыт (архив) `2026-10-02-drop-local-cache-read-from-pg`** — преемник
  `cache-architecture-alignment` в части identity: локальное чтение кэша из
  PostgreSQL снято, и удаление `role` больше не ломает идентичность claim'а
  (`worker_id` без `role`). Канонизации cache-контракта он **не** выполнил:
  каталога `specs/` у него нет вовсе, поэтому обещания «B снимает дубли» этот
  change не выполняет.
- **Закрыт (архив) `2026-10-02-fix-cache-process-boundary`** — CLI больше не
  второй владелец cache-файла, так что в момент удаления `role` не остаётся
  пути, где CLI открывает файл, который Gateway держит.
- **Уже удалён 2026-10-02 `streamlit_app.py`** (change
  `remove-streamlit-runtime`, которого в репозитории нет). Правок он не
  требовал и не требует: `create()` он не вызывал, только
  `config._initialize_settings` (`:95`). Сопряжение по конфигурации
  (`--profile`) сохраняется: CLI-клиент наследует тот же lifecycle-gate.
- **Не существует: `remove-deprecated-enable-kwargs`.** Замена кандидата не
  выполнена, исполнитель не назначен. Снятие deprecated kwargs — отдельный
  предмет; этот change фиксирует текущее состояние границы, а не заменяет
  отсутствующий change.

## Impact

**Runtime-код:**

- `lib/core/application_context.py` — удалить `role` (`:156,193,250`) и его
  чтение (`:406`); убрать `enable_cron` из composition (`:159,253,315`);
  исправить docstring `_make_cron_service` (`:1585`; сама функция `:1584`).
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

- Cache lifecycle, ownership, heartbeat, fencing, shutdown ordering — снято
  архивным `2026-10-02-drop-local-cache-read-from-pg`.
- Процессная граница skill ↔ cache — архивный
  `2026-10-02-fix-cache-process-boundary`.
- **Канонизация дубликатов cache-контракта в `runtime/entrypoints` под
  `data/cache-provider` / `data/cache-runtime-lifecycle`** — требует
  отдельного change (решение Р2). Это известный долг: `cache-architecture-alignment`
  не существует ни в `changes/`, ни в `archive/`, а его архивные потомки не
  имеют каталога `specs/`.
- Снятие deprecated kwargs (`remove-deprecated-enable-kwargs`) — отдельный
  change; замена кандидата не выполнена, исполнитель не назначен.
- Удаление `streamlit_app.py` — **выполнено 2026-10-02**, отдельного change
  не требует.
- Новый HTTP/OpenAPI-эндпойнт для CLI: WebSocket-транспорт уже поднят и
  документирован; второй транспорт означал бы два протокола для одной
  задачи.
- Внедрение upstream TypeScript TUI (`nanobot/cli/tui_launcher.py`): он
  требует отдельного бинарника и дублировал бы `lib/cli/console_loop.py`.
- Правка внутренностей `nanobot` (включая литералы имён каналов в
  `AgentLoop`): это ответственность upstream; здесь фиксируется
  проверяемая формулировка вместо невыполнимого требования.
