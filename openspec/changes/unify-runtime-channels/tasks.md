> ## ⚠️ NEEDS-REWORK (2026-10-02) — не начат (3/48)
>
> Разбор и список того, что обязательно пере-decide, — в шапке
> `proposal.md`; обоснование по дизайну — в шапке `design.md`. Коротко:
> технические факты change'а проверены и держатся, направление стало не
> устаревшим, а более дорогим (CLI поднимает вторую сессию `enterprise-mcp`),
> поэтому **удалять нельзя**.
>
> **Про `file:line` в этом файле.** Привязки проверены 2026-10-02 и поправлены
> там, где они протухли; оставшиеся разошлись на единицы строк из-за сноса
> кластера кэша и помечены «проверить по месту». Первым делом при
> реализации перепроверить номера строк, а не доверять им.
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
- [ ] 5.5 Исправить docstring `_make_cron_service` (`:379`; **было `:1727` — ссылка
      протухла**: файл сократился с ~1730 до 1502 строк после сноса кластера
      кэша, определение переехало) — он противоречит фактическому гейту
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

- Cache lifecycle, ownership, heartbeat, fencing, shutdown ordering —
  `cache-architecture-alignment`
- Процессная граница skill ↔ cache — `fix-cache-process-boundary`
- Удаление `streamlit_app.py` — `remove-streamlit-runtime`
- Новый HTTP/OpenAPI-эндпойнт; второй порт для клиента
- Внедрение upstream TypeScript TUI
- Правка внутренностей `nanobot` (в т.ч. литералы имён каналов в `AgentLoop`)
- Смена default `enable_cron` (требует user-approval)
