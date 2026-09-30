# Каталог переиспользования внутренних механизмов Nanobot

> Цель: **не изобретать то, что Nanobot уже поставляет.** Каждая запись —
> доказанный факт из установленного `nanobot-ai==0.3.5`, а не предположение.
>
> Правило проекта: **перед патчем искать здесь.** Если механизм есть и он
> просто не подключён — подключать, а не переписывать.

Проверено чтением установленного пакета (`nanobot_ai-0.3.5`).

---

## 1. `context_governor` — ПАТЧ НЕ НУЖЕН, ФУНКЦИЯ УЖЕ РАБОТАЕТ

Это главная находка. Патч на ~200 строк переписывает функцию, которую
Nanobot 0.3.5 уже выполняет.

**Цепочка подтверждена чтением:**

```
config/schema.py        max_tool_result_chars: int = 16_000
  └─ AgentLoop.__init__(max_tool_result_chars: int | None = None)   loop.py
       └─ self.max_tool_result_chars
            └─ AgentRunSpec(max_tool_result_chars=..., 
                            workspace=effective_scope.project_path,   loop.py
                            session_key=session.key)                 
                 └─ ContextGovernanceConfig(workspace, session_key, max_tool_result_chars)   runner.py
                      └─ ContextGovernor.normalize_tool_result(...)  context_governance.py
                           └─ maybe_persist_tool_result(config.workspace, config.session_key,
                                            call_id, text, max_chars=config.max_tool_result_chars)
                                └─ utils/helpers.py: пишет
                                     workspace/.nanobot/tool-results/<session>/<call_id>.txt
                                   возвращает ссылку с абсолютным путём,
                                   размером и превью 1200 символов
```

`maybe_persist_tool_result` начинается с
`if workspace is None or max_chars <= 0 or len(content) <= max_chars: return content`.
**Проверено, что `workspace` заполнен в обоих путях:** основной ход —
`workspace = effective_scope.project_path`, субагент — `workspace = root`.
Значит ветка `workspace is None` не срабатывает и persist происходит уже сейчас.

**Проектный патч делает то же самое**, шаг за шагом
(`lib/services/runtime_patcher.py:696-705`): `ensure_nonempty_tool_result` →
исключение `read_file` → сериализация → persist при превышении порога →
короткая ссылка. Это пересказ upstream-функции, включая ту же константу
`exempt_tools = frozenset({"read_file"})`, которую upstream объявляет как
`TOOL_RESULT_OFFLOAD_EXEMPT_TOOLS` с комментарием *«read_file has its own
bound; exempt it to avoid persist->read->persist loops»*.

**Действие:** удалить патч. Поведение не меняется; формат ссылки становится
upstream (абсолютный путь + превью), retention — по бакетам с очисткой
чужих сессий вместо `persist_max_files`.

**Побочный вывод:** `save_turn` тоже не нужен. Результат уже персистится в
момент возврата tool'а, поэтому усечение в `_save_turn` не теряет оригинал.
Upstream там уже санитайзит: `_sanitize_persisted_blocks` заменяет отброшенные
результаты на `[tool result omitted during persistence]`.

---

## 2. `turn_delivery_fail` — точка инъекции публична

`TurnDelivery.fail` содержит **хардкоженный литерал** `content="Sorry, I
encountered an error."` (`turn_delivery.py:341`). Конфига для этой строки нет
ни в `config/schema.py`, ни в `AgentRunSpec` (там есть `error_message`, но он
обслуживает другую ветку — `final_content = clean or spec.error_message or
_DEFAULT_ERROR_MESSAGE` в `runner.py`, и `AgentLoop` его не передаёт).

Но объект подменяем:

| Факт | Место |
|---|---|
| `AgentLoop.__init__(turn_delivery_factory: TurnDeliveryFactory \| None = None)` | `loop.py:296` |
| валидация только по идентичности шины: `factory.bus is not bus` | `loop.py:310-311` |
| все потребители идут через инстанс | `loop.py:913, 1414, 1424, 1622`, `command/builtin.py:353` |
| конструкций `TurnDelivery(` во всём пакете — **две**, обе внутри фабрики | `turn_delivery.py:97, 108` |

**Действие:** подкласс `TurnDeliveryFactory` с переопределёнными
`create` / `unrouted`, инжект через `AgentLoop(turn_delivery_factory=...)`.

**Оговорка про entry points:** gateway собирает свою фабрику с
`route_policy=WebuiTurnRoutePolicy(session_manager)` (`gateway_runtime.py:467-470`),
а CLI не передаёт ничего (`cli/agent.py:174-179`). Инжекить надо **в обоих**,
иначе WebUI-маршрутизация сломается.

`RecoveryCoordinator` (940 строк) заменой **не является**: он WebUI-ориентирован
(обе публикации жёстко `channel="websocket"`), пользовательского текста ошибки
не производит, а его контракт к `AgentLoop` — трёхметодный протокол
`RecoveryAdmission` без колбэка на пути отказа.

---

## 3. `exec_timeout_cap` — конфиг уже есть

`ExecToolConfig.timeout` (`shell.py:97`, `ge=0`, `0 = без лимила`,
комментарий *«Not capped by the per-call max»*) **уже прокинут в проекте**:
`application_context.py:286` читает `gateway.exec_timeout` →
`config_service.py:277` пишет `config.tools.exec.timeout`.

Оставшаяся часть — `_MAX_TIMEOUT = 600` и хардкоженный `maximum=600` в схеме
(`shell.py:127`, литерал текстуально независим от константы на `shell.py:250`).
Оба читаются как `self.` / через переопределяемое свойство `parameters`
(`base.py:338-342`), поэтому лечатся **подклассом `ExecTool`, зарегистрированным
по имени**, без патча.

Плюс к тому: обоснование патча было «legal 7–10 минут». После переноса legal в
MCP LLM-вызов происходит в отдельном процессе, а не через exec, — то есть
патч становится не нужен и по существу.

**Действие:** удалить.

---

## 4. `tool_limits` — 3 из 5 целей берутся подклассом

`ToolRegistry.register` — безусловная перезапись `self._tools[tool.name] = tool`,
защиты встроенных нет. Проект уже ходит этим путём: `project_tool_loader.py:303`
→ `application_context.py:495`, то есть **после** сборки инструментов на
`application_context.py:414`.

| Цель | Читается как | Подкласс `Tool` |
|---|---|---|
| `ReadFileTool._MAX_CHARS` | `self._MAX_CHARS` | ✅ да |
| `ListDirTool._DEFAULT_MAX` | `self._DEFAULT_MAX` | ✅ да |
| `GrepTool._MAX_FILE_BYTES` | `self._MAX_FILE_BYTES` | ✅ да |
| `search._DEFAULT_HEAD_LIMIT` | **голый глобал модуля** внутри метода | ❌ нет |
| `search._DEFAULT_FILE_HEAD_LIMIT` | **голый глобал модуля** внутри метода | ❌ нет |

Последние два — только **значения по умолчанию**: per-call `head_limit` уже
существует (`search.py:377, 704`). Варианты: оставить патчем два глобала,
переписать два пути execute, или принять дефолт.

Оговорка: путь через entry-point плагины (`loader.py:108-113`) коллизию со
встроенным именем **пропускает**. Работает только прямая регистрация в
`ToolRegistry` — то есть наш `project_tool_loader`.

---

## 5. `exec_limits` — патч необходим

Единственная цель, которую нельзя снять ни конфигом, ни подклассом:

* эффективный потолок задаётся **глобалом модуля** `MAX_OUTPUT_CHARS = 50_000`
  (`exec_session.py:29`) внутри `clamp_session_int` (`shell.py:339, 374-378`),
  плюс граница буфера сессии (`exec_session.py:139-140`);
* подкласс может поднять `ExecTool._MAX_OUTPUT` (`shell.py:251`), но значение
  всё равно будет зажато 50 000;
* `maximum` в JSON-схеме берётся из того же глобала (`shell.py:151, 157`), но
  `tool_parameters` **замораживает** схему через `deepcopy` на этапе декорации
  (`base.py:336`), поэтому поздняя мутация константы в схему не доходит.

Поля в `ToolsConfig` / `FileToolsConfig`, влияющего на размер вывода, **нет** —
проверен весь `config/schema.py`.

**Действие:** оставить патч. Добавить контрактный тест на инварианты
`shell.MAX_OUTPUT_CHARS` и буфера `ExecSession`, чтобы поломка апгрейда была
громкой.

---

## 6. `subagent_logging` — блокер структурный

Три независимых препятствия:

1. `SubagentManager.__init__` (`subagent.py:96-108`) — **нет** параметра
   hook / observer / factory. Хук зашит в единственном месте вызова:
   `hook=_SubagentHook(task_id, status)` (`subagent.py:433`).
2. Субагент вызывает `self.runner.run(AgentRunSpec(...))` напрямую
   (`subagent.py:427`), минуя `build_agent_turn_hook`. `AgentProgressHook`,
   наши `hooks` и `hook_factories` в цепочку субагента **не входят**.
3. `AgentRunSpec` субагента **не передаёт `events`** → значение по умолчанию
   `NO_EVENTS` (`runner.py:115`). Субагент публикует **ноль** `AgentEvent` и
   не касается `TurnDelivery`. Единственный трафик — `_announce_result`
   → `publish_inbound` с `metadata["injected_event"] = "subagent_result"`,
   который начинает новый родительский ход.

Подменить сам `SubagentManager` тоже нельзя: `_register_default_tools` —
последний оператор `__init__` (`loop.py:453`), а `SpawnTool` навсегда
захватывает инстанс (`spawn.py:51-59`).

**Частичная замена возможна:** `SubagentManager.runtime_statuses()` —
публичный, и там surfaced `tool_events` (`runtime_control.py:266-287`),
которые наполняет сам `_SubagentHook.after_iteration`. Polling-подписчик
заменил бы часть с записью в БД, потеряв inline-сигнал и per-call latency.

**Действие:** оставить патч. Проверить, действительно ли субагенты запускаются
в деплое: `config.json` содержит `maxConcurrentSubagents: 1`, но
`logs/gateway.log` — 780 байт, эмпирики нет. Если не запускаются — патч
удаляется целиком.

---

## 7. `assemble_outbound` — порядок благоприятен

Подтверждено: **ни один хук не вызывается после `_assemble_outbound`.**
Последний вызов хука — на стадии `"run"`; `_assemble_outbound` вызывается
один раз на стадии `"respond"` (`loop.py:2079`), а `publish_outbound` — ещё
позже (`turn_delivery.py:309`).

Но альтернатива через события **безопасна по порядку**: хук на стадии `"run"`
вызывает `await bus.publish(...)`, который ждёт локальных подписчиков
инлайн (`queue.py:118-126`). Это происходит строго раньше стадии `"respond"`,
а значит раньше постановки финального outbound в очередь. Очередь outbound —
единая FIFO (`queue.py:33`), поэтому порядок сохраняется.

Ограничение: **`bus.publish_nowait` использовать нельзя** — он порождает
отсоединённую задачу и по документации не является глобальным FIFO
(`queue.py:128-143`).

`OutboundMessage.media` **никогда не заполняется самим Nanobot** — ни в одной
из пяти конструкций. Это поле, которое заполняет потребитель. Штатный
структурированный слот — `OUTBOUND_META_AGENT_UI = "_agent_ui"`
(`bus/events.py:13`), но он записывается только на этапе сборки, то есть
именно в той точке, которая после возврата хука недоступна.

**Действие:** `_final_turn` → `TurnEndEvent`. `_tool_audit` и `media` требуют
решения: событие + чтение на стороне канала, либо сохранение узкого патча.

---

## 8. Прочее, что уже поставляется и не используется

| Модуль | строк | Что умеет | Наш аналог |
|---|---:|---|---|
| `agent/context_governance.py` | 1018 | persist результатов, бюджет контекста, compaction по давлению, `ContextWindowExceededError` | `context_governor` |
| `session/recovery.py` | 940 | `scan` / `admit` / `turn_completed` / `handle_action`, `RecoveryStateEvent` | — |
| `agent/memory.py` | 1297 | память агента | — |
| `triggers/` | ~1000 | локальный стор триггеров и раннер | — |
| `llm_usage/` | 775 | метрики вызовов в SQLite, `source_from_request` | частично `agent_question_runs` |
| `agent/autocompact.py` | 134 | авто-сжатие | — |

---

## 9. Итог

| # | Патч | Итог |
|---|---|---|
| 1 | `context_governor` | **удалить** — upstream уже работает |
| 2 | `save_turn` | **удалить** — следствие (1) |
| 7 | `async_save` | **удалить** — наш класс `PGSessionManager` |
| 11 | `session_content_cleanup` | **удалить** — наш класс |
| 8 | `session_dir_watch` | **удалить** — гейт выключен, тестов нет |
| 10 | `turn_delivery_fail` | **удалить** — подкласс `TurnDeliveryFactory` |
| 4 | `exec_timeout_cap` | **удалить** — `tools.exec.timeout` уже прокинут + подкласс |
| 5 | `tool_limits` | **частично** — 3 из 5 подклассами, 2 глобала |
| 3 | `exec_limits` | **оставить** — глобалы + import-frozen схема |
| 6 | `assemble_outbound` | **частично** — события для `_final_turn` |
| 9 | `subagent_logging` | **оставить** — блокер структурный |
| 12 | `document_text_threshold` | уходит с документами |

**12 → 2 полностью необходимых + 2 частичных.** Семь патчей удаляются
без замены или подклассом, и двое из них (`context_governor`, `save_turn`)
оказались переписыванием работающей upstream-функции.
