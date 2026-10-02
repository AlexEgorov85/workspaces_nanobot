> ## ⚠️ NEEDS-REWORK (2026-10-02)
>
> Разбор — в шапке `proposal.md`. Кратко: 7 из 7 проверенных технических
> фактов держатся, `file:line`-привязки разъехались на единицы строк, одна
> ссылка (п. 5.5) была протухшей и исправлена. Change написан **до** миграции
> `enterprise-mcp` и ничего о ней не знает.
>
> Что это меняет в дизайне ниже: сегодня `ApplicationContext.create()`
> безусловно строит `ctx.enterprise_mcp`
> (`application_context.py:502`, без гейта по `role`), поэтому `cli_agent.py`
> поднимает **второй процесс `enterprise-mcp`** — второй держатель пула
> PostgreSQL. Решения D1–D8 ниже написаны в логике «CLI сам — runtime» и
> это допущение надо переформулировать: после change'а `ApplicationContext` в
> CLI не поднимается, и сессия `enterprise-mcp` остаётся одна.
>
> **Не удалять.** Направление (CLI — тонкий WebSocket-клиент) стало не
> устаревшим, а более дорогим по последствиям, чем было написано.

## Context

Проект держит два равноправных Agent Runtime. `gateway.py` и `cli_agent.py`
каждый собирают `ApplicationContext` со своим `AgentLoop`, bus'ом, patch'ами и
хранилищем сессий; их различает единственный параметр `role`. Пока CLI — это
«второй gateway без каналов», параметр `role` вынужден нести смысл, которого
у него нет: он не «роль», а признак способа запуска, который ретроспективно
расползся на cache identity (уже исправлено в
`cache-architecture-alignment`) и на cron.

Проверено, что инфраструктура для «CLI = клиент Gateway» **уже есть** и
поднята: upstream-канал `WebSocketChannel`
(`nanobot/channels/websocket/runtime.py:363`) регистрируется
`ChannelManager` по умолчанию и стартует вместе с Gateway
(`lib/services/channel_factory.py:67-69`), а его wire-протокол Python-видим
(`inbound_commands.py:467,476-504`; `base.py:310-321`;
`runtime.py:810-822,1181-1214,1339-1365`;
`webui/outbound_wire.py:198-231`).

Чего нет: Python-клиента этого протокола, авторитетного сигнала завершения
turn'а на стороне CLI, и внятной диагностики «Gateway не запущен».

## Goals / Non-Goals

**Goals:**

- Agent Runtime создаётся ровно в одном месте — Gateway. CLI не создаёт
  `ApplicationContext` и не владеет `AgentLoop`.
- Параметр `role` удалён из runtime API.
- CLI общается с Gateway по существующему WebSocket-транспорту с
  документированным wire-протоколом.
- Завершение turn'а определяется типизированным wire-кадром `turn_end`, а не
  первым финальным сообщением.
- Cron — свойство Gateway, а не переключатель composition.
- Канонические спеки соответствуют коду.

**Non-Goals:**

- Новый HTTP/OpenAPI-эндпойнт (WebSocket-транспорт уже поднят).
- Новый порт/хост для клиента: адрес берётся из тех же ключей
  `channels.websocket.*`, которые Gateway уже валидирует.
- Правка внутренностей `nanobot` (включая литералы имён каналов в
  `AgentLoop`).
- Внедрение upstream TypeScript TUI.
- Удаление `streamlit_app.py`.
- Cache lifecycle (change `cache-architecture-alignment`).

## Decisions

### Decision 1: `role` удаляется, а не переименовывается

`ApplicationContext.create(..., role: Literal["gateway", "cli"])`
(`application_context.py:193`) удаляется вместе с полем `ctx.role`
(`:156,250`) и чтением (`:385`).

**Почему не «переименовать в `entrypoint`»:** после перехода CLI не создаёт
composition root вообще. Остаётся единственный вызывающий — `gateway.py`.
Параметр с одним допустимым значением и нулём вызывающих не является
контрактом; он — остаток старой модели, и сохранение его означало бы, что
следующий разработчик снова введёт «второй runtime по флагу».

**Совместимость:** это BREAKING-изменение. `cli_agent.py` обновляется в этом
же change, `streamlit_app.py` — в своём (`remove-streamlit-runtime`), а
`tests/test_application_context_role.py` переименовывается и переписывается.

**Что даёт удаление, кроме чистоты:** исчезает класс ошибок «сервис создан
не тем способом запуска». Сегодня это уже выстрелило дважды: cron-gate
(`:385`) против docstring (`:1727`) и `return_file_manager=not
ctx.enable_cron` (`:315`), где флаг, относящийся к расписанию задач,
определяет наличие файлового session manager'а.

### Decision 2: `CliChannel` — клиент протокола, а не абстракция ради симметрии

Критерий, по которому компонент признаётся настоящим, а не искусственным:

> У компонента MUST существовать внешний контракт, который он реализует, и
> MUST быть наблюдаемый провал, если контракт недоступен.

`CliChannel` проходит этот критерий:

- **Внешний контракт** — wire-протокол `WebSocketChannel`: входящее
  `{"type": "message", "chat_id", "content"}` (`inbound_commands.py:467`),
  исходящее `ready` / `delta` / `stream_end` / `message` / `turn_end`
  (`runtime.py:810-822,1181-1214,1339-1365`; `outbound_wire.py:198-231`).
- **Наблюдаемый провал** — handshake без валидного токена получает `401`
  (`nanobot/webui/gateway_endpoint.py:97-100`); недоступный порт даёт
  таймаут соединения. Оба — диагностируемые состояния, а не silent no-op.

Что `CliChannel` **не** делает (антиискусственные правила):

- не является заглушкой «канала для симметрии с `PostgresChannel`»;
- не публикует в локальный `MessageBus` и не создаёт локальный `AgentLoop`;
- не подменяет недоступную функциональность локальной реализацией. Если
  wire-протокол чего-то не умеет (например, произвольных service-вызовов) —
  это либо не требуется, либо требует расширения протокола, а не
  эмуляции;
- не использует `MessageExchange` (`lib/channels/message_exchange.py:135-171`) —
  это движок **поллинг**-транспортов (семафор, слоты, backoff, priority
  poll). WebSocket-клиент — push-транспорт: у него есть read-loop. Перенос
  семафора и поллинг-слотов сюда был бы переносом чужой модели.

Переиспользуются из существующего кода: медиа-кодек
(`workspace/utils/media.py`), фильтрация служебного шума
(`lib/utils/outbound_meta.py`), рендер (`nanobot.cli.stream.StreamRenderer`).

### Decision 3: handshake — bootstrap-токен, без обхода аутентификации

Последовательность подключения:

```text
1. GET  http://<host>:<ws-port>/webui/bootstrap   → { token, ws_path, ... }
2. WS   connect ws://<host>:<ws-port><ws_path>?token=<token>
3. ←    {"event": "ready", "chat_id": ..., "client_id": ...}
4. ⇄    {"type": "message", "chat_id": ..., "content": ...}  /  delta, stream_end, turn_end
```

Bootstrap требует `Authorization: Bearer <secret>`/`X-Nanobot-Auth`, **кроме**
случая, когда секрет не сконфигурирован — тогда он доступен только
localhost-запросу (`nanobot/webui/ws_http.py:668-680`). В этом репозитории
секции `channels.websocket` в `config.json` нет, поэтому секрет пуст и путь
bootstrap доступен с localhost. Handshake при этом всё равно требует токена
(`websocket_requires_token=True` по умолчанию, `runtime.py:207`;
проверка `gateway_endpoint.py:91-100`).

Bootstrap — **единственный** способ аутентификации клиента, и переключателя
для него не вводится: маршрут обслуживается безусловно
(`nanobot/webui/ws_http.py:569`), доступ ограничен доверенным прокси, либо
совпадением секрета, либо локальным запросом при незаданном секрете
(`ws_http.py:668-680`). Флаг `false` означал бы обход аутентификации, что
прямо запрещено требованием `runtime/cli-client`.

**Почему нельзя «просто» выставить `websocket_requires_token=false`:**
это отключило бы аутентификацию не только для CLI, но и для любого
локального WebSocket-клиента проекта (Streamlit-UI, отладочные инструменты).
Упрощение ради одного клиента ослабляет общий периметр.

**Что MUST NOT делать клиент:** подставлять токен из другого источника,
обходить `authorize_websocket_handshake`, ретраить с ослабленными условиями.

### Decision 4: turn завершается кадром `turn_end`

Текущая логика (`console_loop.py:254-317`) завершает turn на первом
`StreamedResponseEvent` (`:289`). Это эвристика: она срабатывает и на
промежуточном «финале» (например, при early return), и не срабатывает, если
финальный ответ не пришёл отдельным событием. Страховочный
`turn_wait_timeout` (`:233-243`, дефолт 300 с) превращает эту ошибку в
сообщение «нет связи с агентом» (`:395-404`), то есть вводит пользователя в
заблуждение.

Целевая семантика:

| Wire-кадр | Проекция на типы bus | Действие клиента |
|---|---|---|
| `delta` | `StreamDeltaEvent` | `renderer.on_delta(text)` |
| `reasoning_delta` / `reasoning_end` | reasoning-события | буфер рассуждения |
| `stream_end` | `StreamEndEvent` | закрыть рендер потока |
| `message` | `StreamedResponseEvent` | напечатать ответ |
| `turn_end` | — | **завершить turn**, вывести usage/latency/failure |

Проекция wire → типы `nanobot.bus.outbound_events` выполняется **на границе
клиента**, чтобы переиспользовать `StreamRenderer` и печать ответов. Эти
объекты никуда не публикуются в bus и не потребляются `AgentLoop` — это
внутреннее представление рендера, а не имитация локального рантайма.

`turn_wait_timeout` сохраняется, но **меняет смысл**: он watchdog соединения
(«нет ни одного кадра дольше N секунд»), а не сигнал завершения turn'а.

`turn_end` несёт `latency_ms`, `usage`, `round_usages`, `outcome`,
`failure_kind`, `failure_attempts`, `failure_message`, `context_window_tokens`
(`outbound_wire.py:211-228`). Поля `usage`/`context_window_tokens` — источник
данных для клиентских индикаторов; **UNVERIFIED**: заполняется ли
`context_window_tokens` на стороне проекта (текущий `patch_assemble_outbound`
кладёт `metadata.context_window`), поэтому перенос индикатора на `turn_end`
вынесен в отдельную задачу с проверкой, а не заявлен как факт.

### Decision 5: slash-команды делегируются, а не исполняются локально

`/compact` сегодня вызывается локально: `console_loop.py:124-142,359-361`
вызывает `ContextCompactionService.compact(...)` из CLI-процесса. После
перехода CLI не имеет ни `ApplicationContext`, ни сервиса сжатия, ни
`agent_conversation_messages`. Локальный вызов стал бы либо невозможен, либо
(что хуже) молча сжимал бы сессию в БД из второго процесса.

Целевое поведение: `/compact` отправляется как обычное сообщение
(`content="/compact"`). Gateway разбирает команду в своём `CommandRouter`
(`nanobot/agent/loop.py:1342` — команды обрабатываются для любого канала, кроме
`"system"`), где подключён пропатченный `cmd_compact`
(`RuntimePatcher.patch_compact_command`).

**Оговорка (требует проверки, а не утверждения):** команда ставит
`FINAL_TURN_KEY="_final_turn"` в outbound, чтобы **Postgres-канал**
финализировал оборот. Для WebSocket-клиента финализация выражается в
`turn_end`; совпадает ли это с наличием `turn_end` после `/compact` —
проверяется задачей верификации (§Verification), а не предполагается.

Прочие локальные операции CLI также уходят: `_migrate_cron_store`
(`cli_agent.py:232-243`) переносит `jobs.json` на диске клиента — при
Gateway-owned cron это бессмысленно (cron-файлы принадлежат Gateway).

### Decision 6: Cron — свойство Gateway; `enable_cron` не переключает composition

Сейчас `enable_cron` (default `False`, `application_context.py:159`) влияет на
три вещи: создание `CronService` (`:385`), `return_file_manager`
(`:315`) и поведение в уже существующих specs. Последние два — побочные
эффекты, а не намерение.

Целевое: cron включается конфигурацией Gateway
(`gateway.enable_cron`), поднимается вместе с Gateway, `ctx` не хранит флаг,
а `return_file_manager` определяется режимом хранилища, а не расписанием.
Заодно правится docstring `_make_cron_service` (`:1727`), который сегодня
утверждает обратное фактическому гейту.

**Не входит:** смена default `enable_cron` (`False → True`) требует
user-approval, как зафиксировано в `cache-architecture-alignment`.

### Decision 7: без автозапуска Gateway; отказ диагностируется

Upstream умеет поднимать Gateway по требованию
(`GatewayClientLease.ensure_on_demand_gateway`,
`nanobot/cli/tui_launcher.py:482`), но команда, которую он выполняет, —
`nanobot gateway <sub> --config <path>`
(`nanobot/cli/webui_support.py:665-678`). Это **upstream**-gateway: без
`ChannelFactory`-каналов проекта, без `RuntimePatcher`, без project tools.
Запуск такого процесса дал бы пользователю CLI, который выглядит рабочим, но
обслуживается другим рантаймом. Это хуже явной ошибки.

Целевое поведение клиента:

- попытка подключения → таймаут/ошибка → одно понятное сообщение с адресом,
  который он пытался использовать, и с командой запуска
  (`python gateway.py --profile=<profile>`);
- ненулевой код выхода; ноль — только при успешном подключении;
- никаких фоновых retry-циклов, маскирующих состояние «соединения нет».

**Про reconnect после успешного подключения:** допускается ограниченный
retry с backoff и явной индикацией «соединение потеряно / переподключение» в
REPL. Тихий retry без индикации запрещён: он выглядит как зависание.

### Decision 8: адрес клиента — те же ключи, что валидирует Gateway

`cli.gateway_url` (опционально) переопределяет адрес; по умолчанию он
собирается из `channels.websocket.host`/`port`
(`runtime.py:197-198`) — тех же ключей, по которым `gateway.py:460-511`
проверяет занятость порта. Второй порт для клиента не вводится: два адреса
для одной пары процессов — это ровно тот класс расхождения, который уже
возникал с `gateway.cache.local_path` (см. `cache-architecture-alignment`).

### Decision 9: исправление канонических спек вместо already-false утверждений

Два утверждения канонической спеки фактически неверны, и их нельзя оставить
после того, как модель поменяется:

1. `entrypoints/spec.md:164` — `PostgresChannel` создаётся `ApplicationContext`.
   Фактически — `ChannelFactory._add_postgres` (`channel_factory.py:129-191`)
   из `gateway.py:249-258`.
2. `entrypoints/spec.md:189-199` — «`AgentLoop` MUST NOT знать о CLI,
   WebSocket…». Фактически в `nanobot/agent/loop.py` есть ветвления по
   литеральным именам каналов (`:928`, `:1331`, `:1297`, `:1342`, `:1814`,
   `:1822`, `:2340`).

Правка формулировки, а не кода: upstream не меняем. Новое требование
фиксирует проверяемый инвариант — «`AgentLoop` взаимодействует с миром только
через `MessageBus`; ветвления по именам каналов являются перечнем известных
исключений и не должны расширяться проектом».

**Заголовки требований, ставшие ложными, удаляются, а не переписываются.**
Пять требований перестают быть верными в самом названии
(`... create с role`, `role определяет composition ...`, `WebSocket port check
остаётся server-only`, `CLI-специфичные runtime-параметры`,
`/compact в CLI остаётся локальной`). Переписывание на месте оставило бы
историю правки внутри заголовка, а `REMOVED` + `ADDED` с явными `**Reason**`
и `**Migration**` делает видно, что контракт заменён, а не уточнён.

**Дубликаты cache-контракта удаляются в `cache-architecture-alignment`, не
здесь.** `runtime/entrypoints` содержит четыре требования про cache
(ownership, fencing, layered API, `CacheSyncService`), которые дублируют
`data/cache-provider` и `data/cache-runtime-lifecycle`, расходятся с ними по
заголовкам и ссылаются на `role="cli"`. Это канонизация кэша — работа B, где
и создаются канонические capabilities. Выполнение её в C означало бы, что
change про entrypoints владеет cache-спеками; при обратном порядке удаление
`role` оставило бы в спеке требования, ссылающиеся на несуществующий
параметр. A → B → C решает обе проблемы: B снимает дубли, C удаляет `role`.

## Risks / Trade-offs

**[Risk]** CLI перестаёт работать без запущенного Gateway. → Это осознанная
цена целевой модели (Gateway-owned runtime). Снижение риска: сообщение с
адресом и командой запуска, ненулевой код выхода, отсутствие «тихого»
поведения. Что НЕ делается: автозапуск другого рантайма (Decision 7).

**[Risk]** Локальный дебаг через CLI (правки tool'ов без рестарта Gateway)
перестаёт работать. → Mitigation: это осознанное следствие; для разработки
остаётся `gateway.py` + логи в БД. Отдельный dev-режим с in-process рантаймом
не вводится — это и есть та абстракция, которую change удаляет.

**[Risk]** `turn_end` может не приходить для некоторых исходов (например,
ошибка до `TurnEndEvent`). → Mitigation: клиент различает «`turn_end`
пришёл» и «соединение молчит дольше watchdog» — это разные сообщения;
плюс интеграционный тест на каждый исход.

**[Risk]** Wire-протокол — часть upstream и может измениться при обновлении
пакета. → Mitigation: инкапсуляция протокола в одном модуле
(`lib/channels/cli_channel.py`) + contract-тесты на парсинг кадров; при
обновлении пакета падает тест, а не CLI в проде. Дополнительно — учёт
`tests/contract/` (upgrade-readiness).

**[Risk]** Bootstrap на localhost без секрета — слабое место при
`token=""`. → Mitigation: клиент не меняет конфигурацию безопасности; при
`channels.websocket.token` заданном — используется штатный
`token_issue_secret`/static token. Изменение дефолтов безопасности — вне
scope.

**[Risk]** Конфликт с `remove-streamlit-runtime` по
`application_context.py`. → Mitigation: не трогать streamlit-специфичный код;
координация через `git`.

## Migration Plan

1. **Phase 1 — Contract.** Spec-дельты: новая capability
   `runtime/cli-client` + модификация `runtime/entrypoints` (8 требований).
   Кода не трогаем.
2. **Phase 2 — `CliChannel`.** Клиент WebSocket-протокола: bootstrap, handshake,
   отправка `message`, приём `ready`/`delta`/`stream_end`/`message`/`turn_end`,
   reconnect с индикацией. Contract-тесты на парсинг кадров — без сети.
3. **Phase 3 — `console_loop` как клиент.** `run_repl` принимает transport;
   проекция wire → bus-события; завершение turn по `turn_end`;
   `turn_wait_timeout` → connection watchdog; `/compact` делегируется.
4. **Phase 4 — `cli_agent.py` без composition.** Удаление
   `ApplicationContext.create`, `_run_vanilla`/`_run_patched`, `_migrate_cron_store`;
   флаги `--session` / `--gateway`; диагностика недоступности Gateway.
5. **Phase 5 — Удаление `role` и cron-флага.** `ApplicationContext.create`
   без `role`; удаление `ctx.role`, `enable_cron` из composition;
   `return_file_manager` по режиму хранилища; docstring'и.
6. **Phase 6 — Верификация.** Полный `pytest`, `tools/architecture_guard.py`,
   smoke CLI против запущенного Gateway, CHANGELOG, документация, архивация.

**Rollback:** фазы обратимы независимо, каждая — отдельный коммит. Фаза 4
без фазы 5 оставляет неиспользуемый параметр `role` (безопасно); фаза 5 без
фазы 4 ломает `cli_agent.py` (необратимо без отката) — поэтому порядок
обязателен.

## Open Questions

- Нужен ли CLI явный `--gateway` как отдельный флаг или достаточно
  `cli.gateway_url` в конфиге? Решение: поддержать оба, флаг — override
  конфига; проверяется в Phase 4.
- Что показывать при `outcome != "completed"` в `turn_end` (например
  `failure_kind`)? Текст пользователю должен отличаться от обычного ответа
  и напоминать про `/status`. Требует продуктового решения; в Phase 3 —
  минимальное отображение, в Phase 6 — финальный текст.
- Покрывает ли `turn_end` все интересующие исходы Gateway (например,
  `/compact`)? Проверяется интеграционным тестом Phase 6, а не предполагается.
