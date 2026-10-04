> ## Статус: контракт пересобран 2026-10-04 — ожидает `enterprise-mcp-http-transport` (D13)
>
> Пометка NEEDS-REWORK снята 2026-10-04. Разбор, её снявший, — в шапке
> `proposal.md` (решения Р1–Р5); обоснование по дизайну — в шапке
> `design.md`. Коротко: технические факты change'а проверены и держатся,
> направление стало не устаревшим, а более дорогим (CLI поднимает вторую
> сессию `enterprise-mcp`), поэтому **удалять нельзя**.
>
> **Первая задача этой итерации — текст, не код.** Два потерянных сценария
> (`Профиль вне whitelist'а отклоняется`, `Ключ вне allowlist'а — TypeError,
> а не молчание`) добавлены в канон 2026-10-03, уже после написания дельты,
> и были восстановлены 2026-10-04. Критерий готовности текстовой части:
> `openspec validate unify-runtime-channels` — чисто, без ERROR.
>
> **Про `file:line` в этом файле.** Привязки проверены 2026-10-02 и поправлены
> там, где они протухли; оставшиеся разошлись на единицы строк из-за сноса
> кластера кэша и помечены «проверить по месту». Первым делом при
> реализации перепроверить номера строк, а не доверять им. **Правка от
> 2026-10-02 внесла ошибку и откатывается** — см. п. 5.5.
>
> **Про разделы 2–4.** Схлопываются в один: `cli_channel.py` не создан, и всё
> в разделах 2–4 — это его содержимое плюс перевод `console_loop` на клиент.
> Порядок: 5 (снять `role` и cron из composition) → 2+3 (клиент) → 4 (убрать
> composition root из `cli_agent.py`). Начинать с 2, как в текущей нумерации,
> нельзя: пока CLI поднимает свой `ApplicationContext`, снос `role` ломает
> входные точки.

## 1. Contract (Phase 1 — DONE)

- [x] 1.1 Новая capability `runtime/cli-client`: `specs/runtime/cli-client/spec.md`
      (8 требований: транспорт, handshake/auth, адресация сессии, `turn_end`,
      недоступный Gateway, reconnect, запрет локального runtime, slash-команды)
- [x] 1.2 Дельта `runtime/entrypoints`: `specs/runtime/entrypoints/spec.md` —
      5 `REMOVED` (заголовки стали ложными) + 5 `MODIFIED` (все canonical
      scenario titles сохранены) + 5 `ADDED`; итого 15 требований
- [x] 1.3 `**Reason**`/`**Migration**` для каждого `REMOVED`; в
      `runtime/entrypoints` не остаётся требований, ссылающихся на `role`
- [x] 1.4 Дельта `runtime/context`: `specs/runtime/context/spec.md` —
      1 `MODIFIED` (`Единый корень общей инфраструктуры`), решение Р1,
      2026-10-04. Без неё `runtime/context/spec.md:61,83` («различие только в
      обязательном kwarg `role`») осталось бы висящим требованием к
      несуществующему параметру. Все четыре канонических заголовка сценариев
      сохранены, тела скорректированы
- [x] 1.5 Восстановлены два потерянных сценария, добавленных в канон
      2026-10-03 после написания дельты: `Профиль вне whitelist'а
      отклоняется` и `Ключ вне allowlist'а — TypeError, а не молчание`.
      Критерий: `openspec validate unify-runtime-channels` — чисто

## 2. CliChannel — клиент WebSocket-протокола Gateway (Decision 2, 3, 7, 8)

- [ ] 2.1 `lib/channels/cli_channel.py`: чтение адреса из `cli.gateway_url`
      либо сборка из `channels.websocket.host`/`port`
      (`nanobot/channels/websocket/runtime.py:197-198`); второго порта не вводить
- [ ] 2.2 Handshake: `GET /webui/bootstrap` → `ws://...<ws_path>?token=<token>`
      → принять `{"event": "ready"}`; таймаут `cli.gateway_connect_timeout_sec`
- [ ] 2.3 Аутентификация: использовать токен bootstrap; при заданном
      `channels.websocket.token` / `token_issue_secret` — штатный путь
      (`nanobot/webui/gateway_endpoint.py:77-104`). `websocket_requires_token`
      MUST NOT отключаться ради клиента
- [ ] 2.4 Отправка: `{"type": "message", "chat_id": ..., "content": ...}`
      (`nanobot/webui/inbound_commands.py:467,476-504`)
- [ ] 2.5 Приём и разбор кадров: `ready`, `delta`, `stream_end`, `message`
      (`kind` = `tool_hint`/`progress`), `reasoning_delta`, `reasoning_end`,
      `turn_end`, `error`
- [ ] 2.6 Reconnect: ограниченный retry с backoff + явная индикация
      «соединение потеряно / переподключение» в REPL; тихий retry запрещён
- [ ] 2.7 Contract-тесты парсинга кадров без сети:
      `tests/test_cli_channel_client.py` (включая `error` и неизвестный `event`)
- [ ] 2.8 Не использовать `MessageExchange` (движок поллинг-транспортов);
      переиспользовать только медиа-кодек и `lib/utils/outbound_meta.py`

## 3. console_loop как клиент (Decision 4, 5)

- [ ] 3.1 `run_repl(...)` принимает transport вместо `agent`; `bus_task =
      asyncio.create_task(agent.run())` (`:319`) и `agent.bus` (`:208`) уходят
- [ ] 3.2 Проекция wire → bus-события на границе клиента: `delta` →
      `StreamDeltaEvent`, `stream_end` → `StreamEndEvent`, `message` →
      `StreamedResponseEvent`; объекты не публикуются в bus
- [ ] 3.3 Завершение turn — по кадру `turn_end`
      (`nanobot/webui/outbound_wire.py:198-231`); убрать `turn_done.set()` на
      первом `StreamedResponseEvent` (`:289`)
- [ ] 3.4 `turn_wait_timeout` (`:233-243`) переименовать в connection
      watchdog; сообщение `:395-404` MUST различать «нет кадров» и
      «Gateway недоступен»
- [ ] 3.5 `/compact` (`:124-142,359-361`) делегировать Gateway сообщением
      `content="/compact"`; локальный `ContextCompactionService` из CLI убрать
- [ ] 3.6 Отображать `latency_ms` / `outcome` / `failure_kind` из `turn_end`
- [ ] 3.7 Проверить, заполняется ли `context_window_tokens` в `turn_end`;
      если нет — оставить текущий источник `metadata.context_window` и
      зафиксировать в задаче (UNVERIFIED, не переносить вслепую)
- [ ] 3.8 `tests/test_cli_turn_lifecycle.py`: завершение по `turn_end` при
      нескольких `message` в одном turn'е; отсутствие завершения без `turn_end`

## 4. cli_agent.py без composition root (Decision 1, 5, 7, 8)

- [ ] 4.1 Удалить `ApplicationContext.create` из smoke (`:125`), `_run_vanilla`
      (`:151`), `_run_patched` (`:175`)
- [ ] 4.2 Удалить `_run_patched_repl` (`:194-209`), `_get_cron` (`:212-215`),
      `_migrate_cron_store` (`:232-243`)
- [ ] 4.3 Удалить флаги `--storage`/`-S` (`:54-55`) и `--patched`/`-P`
      (`:53`); их передача → `ConfigurationError` + exit 2 (как у `--profile`,
      `:74-81`)
- [ ] 4.4 Добавить `--gateway`/`-g` (override `cli.gateway_url`); сохранить
      `--session`/`-s` (идентификатор чата/сессии на Gateway)
- [ ] 4.5 Недоступность Gateway → сообщение с адресом и командой
      `python gateway.py --profile=<profile>` + ненулевой код выхода
- [ ] 4.6 `tests/test_cli_agent_args.py`: принятые/отклонённые флаги
- [ ] 4.7 Smoke-режим CLI (`:57-63`, `:121-137`) перевести на проверку
      конфигурации клиента без подключения

## 5. Удаление role и cron-флага из composition (Decision 1, 6)

- [ ] 5.1 `ApplicationContext.create` (`:194`, проверить по месту) без `role`;
      удалить поле `ctx.role` (`:154`, чтения `:251`, `:378` — проверить)
- [ ] 5.2 Удалить чтение `ctx.role` в cron-гейте (`:378`; было указано `:385` —
      строка уехала при сносе кластера кэша, `ctx.role` читается здесь)
- [ ] 5.3 `enable_cron` убрать из composition (`:157,254`; было `:159,253` —
      проверить); cron создаётся Gateway по `gateway.enable_cron`
- [ ] 5.4 `return_file_manager=not ctx.enable_cron` (`:316`; было `:315`) заменить
      на решение по режиму хранилища
- [ ] 5.5 Исправить docstring `_make_cron_service` — **docstring `:1585`, сама
      функция `:1584`**. Он до сих пор утверждает «нужен для CLI-режима (только
      там он нужен)», что противоречит фактическому гейту
      `if ctx.enable_cron and ctx.role == "gateway"`.
      **Правка 2026-10-02 была неверной и откатывается:** она объявила `:1727`
      протухшим и подставила `:379`, но `:379` — комментарий про
      `mcp-platform/platform.json`, а не `_make_cron_service`; «файл сократился
      до 1502 строк» тоже неверно (сейчас 1976). Исходное `:1727` было
      правильным
- [ ] 5.6 Исправить `lib/services/session_storage.py:13-17` (docstring
      перепутывает порядок возврата; фактически `(mode, manager)`) — привязка
      проверена 2026-10-02, совпадает
- [ ] 5.7 Обновить все вызовы `create(...)` вне CLI: `gateway.py:122`
      (`role='gateway'` убрать; было указано `:121`), `benchmarks/runner.py`
      (проверить по месту), тесты
      (`tests/test_application_context_single_application_point.py`,
      `tests/test_application_context_logging.py`,
      `tests/test_application_context.py`,
      `tests/test_gateway_live_media_e2e.py`, `tests/test_gateway.py`,
      `tests/test_profile_lifecycle.py`). `streamlit_app.py` — НЕ caller
      (`create()` отсутствует, только `config._initialize_settings:95`) —
      правок не требует. **Поправка 2026-10-02:** `streamlit_app.py` снят
      2026-10-02 (`docs+chore` da06830), пункт про него больше неактуален.
- [ ] 5.8 `tests/test_application_context_role.py` →
      `tests/test_application_context_composition.py`: нет параметра `role`,
      нет поля `ctx.role`, нет cron в composition CLI
- [ ] 5.9 **Снять три D13-обязательства**, которые
      `2026-10-04-enterprise-mcp-http-transport` **добавляет** в
      `runtime/entrypoints`
      (`2026-10-04-enterprise-mcp-http-transport/design.md:354-361`). Все три
      держатся на предпосылке «CLI поднимает собственный процесс платформы»
      и все три помечены в нём «до `unify-runtime-channels`»:
      1. **сужение «server-only» до порта канала** WebSocket — требование
         разделяет объект «порт канала» и объект «порт платформы»;
      2. **проверка закреплённого порта в обоих входах** — требование
         «Закреплённый порт проверяется до запуска, занятый — отказ запуска»
         требует проверки в gateway **и** в CLI;
      3. **сценарий «Проверки портов не смешиваются»** — CLI проверяет порт
         платформы и MUST NOT выполнять `_check_websocket_port_available()`.

      Порядок D13 (http-transport идёт первым, этот — вторым) делает снятие
      разрешимым, но молчаливым оно быть не должно. Правка спеки — в
      `REMOVED`-блоке «WebSocket port check остаётся server-only»
      (`specs/runtime/entrypoints/spec.md`); он обязан совпасть с текстом,
      который добавит http-transport. Правок кода кроме сноса самой
      предпосылки не требуется: п. 4.1–4.2 убирают `ApplicationContext.create`
      из CLI целиком, а вместе с ним и проверку порта платформы

## 6. Согласованность документации и конфигурации (Decision 8)

- [ ] 6.1 `project.json`: `cli.gateway_url`, `cli.gateway_connect_timeout_sec`
      (оба опциональные, с дефолтами); переключатель bootstrap НЕ вводить
      (эндпойнт поднимается всегда — `ws_http.py:569,665-680`);
      проверить `lib/core/project_settings.py`
- [ ] 6.2 `tests/test_config_keys.py` — новые ключи опциональны;
      `REQUIRED_KEYS` не меняется
- [ ] 6.3 `AGENTS.md:127` — переписать описание
      `ApplicationContext.create(role=...)` (параметр удаляется);
      `AGENTS.md:30` — убрать несуществующий `gateway /health`
- [ ] 6.4 `docs/ARCHITECTURE.md:62,72` — CLI больше не владеет
      `ApplicationContext`: убрать `CLI --> CTX` из mermaid-диаграммы и
      переписать текст «единая точка инициализации … gateway и cli_agent»
- [ ] 6.5 `docs/DATABASE.md:238-240` — убрать несуществующий «унаследованный
      резерв» CLI (`load_cache_from_postgres` / `check_cache_stale` в
      `cli_agent.py` отсутствуют) и «Навык (CLI) открывает снимок на
      чтение» (`:235-236`); ownership снимка — только Gateway
- [ ] 6.6 `benchmarks/README.md:42` — в диаграмме устаревшая сигнатура
      `ApplicationContext.create(enable_db_logging=True, enable_audit=True)`
      (без `role`); привести к новому контракту
- [ ] 6.7 `docs/TARGET_ARCHITECTURE.md:1211` — сверить ссылку на запрет
      второго runtime; `README.md` — роль CLI

## 7. Верификация (Phase 6)

- [ ] 7.1 `python -m pytest tests/ -q` — без новых падений относительно baseline
- [ ] 7.2 `python tools/architecture_guard.py` — без новых нарушений
- [ ] 7.3 Smoke против запущенного Gateway: `python gateway.py --profile=test`
      + `python cli_agent.py`, один turn до `turn_end`; проверить, что `/compact`
      порождает `turn_end` (UNVERIFIED до проверки)
- [ ] 7.4 `python gateway.py --profile=test --smoke` — exit 0
- [ ] 7.5 `tests/contract/` — клиентские contract-тесты парсинга wire-кадров
      (upgrade-readiness)
- [ ] 7.6 `openspec.cmd validate unify-runtime-channels`
- [ ] 7.7 `openspec.cmd archive unify-runtime-channels`

## Вне scope этого change

- Cache lifecycle, ownership, heartbeat, fencing, shutdown ordering — снято
  архивным `2026-10-02-drop-local-cache-read-from-pg` (реальный преемник
  несуществовавшего `cache-architecture-alignment`)
- Процессная граница skill ↔ cache — архивный
  `2026-10-02-fix-cache-process-boundary`
- **Канонизация дубликатов cache-контракта в `runtime/entrypoints` под
  `data/cache-provider` / `data/cache-runtime-lifecycle`** — отдельный change
  (решение Р2). `cache-architecture-alignment` не существует ни в
  `changes/`, ни в `archive/`, и его архивные потомки не имеют каталога
  `specs/`, то есть канонизации не состоялось
- Снятие deprecated kwargs — отдельный change: `remove-deprecated-enable-kwargs`
  не существует, замена кандидата **не выполнена**, исполнитель не назначен
- Удаление `streamlit_app.py` — **выполнено 2026-10-02**, change
  `remove-streamlit-runtime` не существует и не требуется
- Новый HTTP/OpenAPI-эндпойнт; второй порт для клиента
- Внедрение upstream TypeScript TUI
- Правка внутренностей `nanobot` (в т.ч. литералы имён каналов в `AgentLoop`)
- Смена default `enable_cron` (требует user-approval)
