# Аудит: 03 — Runtime-патчи и наблюдаемость (`lib/services/`)

## Сводка группы

Файлов: 10 · LOC: 4757 (из них `runtime_patcher.py` — 2252) · классов: 21 (включая 1 вложенный) · методов: 82 · функций уровня модуля: 30 · вложенных `def`: 6

**Ключевые находки** (каждая — с путём и строкой):

1. **ФУНКЦИОНАЛЬНЫЙ БАГ — наблюдатель compaction-событий не подключён нигде.** `CompactionEventSubscriber` (`lib/services/compaction_event_subscriber.py:34`) не создаётся ни в одном продуктовом модуле: единственные ссылки вне тестов — комментарии `lib/channels/postgres_channel.py:123` и `:1581`. `ChannelFactory._add_postgres` (`lib/services/channel_factory.py:179-181`) вызывает `PostgresChannel(ch_cfg, bus, db_logging_service=...)` и **не передаёт** `compaction_event_subscriber=`, поэтому `PostgresChannel._compaction_event_subscriber` всегда `None`, `feed()` не вызывается ни разу. Следствие: `ContextCompactionService.notify_session_compacted` (0 продуктовых вызовов) мёртв, а `ContextCompactionEvent` upstream (`nanobot/events.py:17`, публикуется в `nanobot/agent/context_governance.py:488,506,649` и `nanobot/agent/memory.py:1251-1282`) **никогда** не попадает в `agent_gateway_logs`. Контракт в docstring (`compaction_event_subscriber.py:13-21`, требующий `feed()` из `postgres_channel.send`, `redis_channel.send`, `streamlit_app._render_agent` и `notify_session_compacted` из `console_loop._run_cli_compact`) не выполнен ни в одной из четырёх точек. → `Упростить` (удалить) либо `Оставить` + починить — см. разбор файла.
2. **ФУНКЦИОНАЛЬНЫЙ БАГ — канонический список project tools расходится с кодом, баннер drift горит на каждом старте.** `runtime_inventory.py:153` объявляет `ToolSpec(name="ExampleTool", module="example")`, но `workspace/tools/example.py:117` возвращает `name == "example_tool"`. Tool включается по умолчанию (`example.py:99-101`: `_read_settings_section` возвращает `{}` → `section.get("enable", True) == True`; секции `tools` в `project.json` нет). `diff_project_tools` (`runtime_inventory.py:274-275`) даёт `unexpected=["example_tool"]` → `application_context.py:942-944` печатает жёлтый `PROJECT TOOLS INVENTORY: drift` и `tools/diagnose_startup.py:315-317` завершается с кодом 2 (DRIFT). `ExampleTool` при этом `required=False`, поэтому `missing_required` пуст. → конкретная правка: `runtime_inventory.py:153` → `name="example_tool"`.
3. **ФУНКЦИОНАЛЬНЫЙ БАГ — `schema_validation.py` хардкодит схему `public`, игнорируя `channels.postgres.schema`.** `expected_table_names` (`schema_validation.py:198`) делает `names.append(("public", cur))` для всех 6 таблиц. Имена таблиц действительно резолвятся из merged SETTINGS (ключевое требование AGENTS.md выполнено), но **схема** — нет: при `channels.postgres.schema = "audit"` валидатор проверит `information_schema` в `public` и либо ложно отрапортует о недостающих таблицах, либо пропустит реально отсутствующие. Остальной код проекта (`channel_factory.py:169`, `context_compaction.py:538`) уважает настройку. → `Оставить` + исправить строку 198 на `pg.get("schema", "public")`.
4. **ФУНКЦИОНАЛЬНЫЙ БАГ (порядок патчей) — `patch_session_dir_watch` фактически слепой ровно там, ради чего добавлен.** В `apply_all` (`runtime_patcher.py:621-623`) `async_save` применяется **до** `session_dir_watch`, поэтому `original` в dir-watch обёртке — это `_wrapped_save`, который либо `return None` (внутри event loop, `runtime_patcher.py:1120-1141`), либо синхронно вызывает апстрим. `FileNotFoundError` в штатном async-пути поднимается внутри executor'а и гасится `_log_save_error` (`runtime_patcher.py:1114-1118`), наружу **не выходит**. Значит `except FileNotFoundError: ...` на `runtime_patcher.py:1224-1232` срабатывает только для вне-цикла (flush/shutdown). Диагностический патч с риском `low` не ловит основной сценарий.
5. **ФУНКЦИОНАЛЬНЫЙ БАГ (классификация) — `patch_exec_timeout_cap` при отсутствии модуля shell попадает в `failed`, а не `skipped`.** Он возвращает `"shell module not loaded"` (`runtime_patcher.py:1370`), а `_SKIPPABLE_REASONS` (`runtime_patcher.py:411-426`) содержит только `"exec_session/shell module not loaded"` (строка 422, от `patch_exec_limits:1318`). Строковое сравнение строгое → skip-причина классифицируется как сбой и попадает в баннер `diff_runtime_patches(... )["failed"]` (`application_context.py:486`).
6. **МЁРТВЫЙ ПАРАМЕТР — `bus` проходит двумя слоями и ни разу не читается.** `apply_all(..., bus=None)` (`runtime_patcher.py:584`) передаёт его в `patch_subagent_logging(..., bus=bus)` (`runtime_patcher.py:1816`), но тело `patch_subagent_logging` (1817-2246) не обращается к внешнему `bus` — внутренний `__init__(self, task_id, status=None, bus=None)` (`runtime_patcher.py:1901`) перекрывает имя. Реальный bus приходит через классовый `_default_bus`, который ставит `RuntimeEventsSubscriber.start()` → `_set_subagent_default_bus` (`runtime_events_subscriber.py:58-76`, `:147`). `ApplicationContext.create()` (`application_context.py:467-474`) `bus=` и не передаёт. → удалить оба параметра.
7. **МЁРТВЫЙ КОД — `_resolve_media_path` (`runtime_patcher.py:64-84`) и `_format_workspace_hint` (`runtime_patcher.py:662-681`) не вызываются ни разу.** Проверено `grep` по `lib/`, `workspace/`, `tools/`, `tests/`: у `_resolve_media_path` нет ни одного вызова (совпадения в `postgres_channel.py:267,860,1293` — это другой символ `_resolve_media_paths_and_hints`); `_format_workspace_hint` встречается только в своём `def`, хотя docstring (665) утверждает, что используется в логах `patch_session_dir_watch`.
8. **МЁРТВЫЙ КОД — `record_external_compaction` (`context_compaction.py:390-435`) не имеет продуктовых вызовов.** Grep по всему репозиторию: только `tests/test_context_compaction.py`, `tests/test_unified_event_logging_contract.py`, `openspec/`, `docs/`. Его docstring (403) отсылает к `runtime_patcher.patch_compaction_tracking`, которого в коде нет (AST-скан всех `def patch_*` в файле даёт ровно 12 патчей, `compact_tracking` среди них нет). Вместе с ним мёртв и весь третий «вход» из module docstring.
9. **СТРОКИ, КОТОРЫЕ ЛГУТ (4 независимых места в одном файле).** `context_compaction.py:1-38` описывает 3 входа, ссылаясь на `lib/commands/compact_command.py` (7), `RuntimePatcher.patch_compact_command` (8), `runtime_patcher._wrap_auto_compact_archive` (17), `runtime_patcher._wrap_maybe_consolidate_by_tokens` (23) — **ни одного** из них в репозитории нет. То же в `_record_event_log` (343: «design D8 — `patch_compaction_tracking` остаётся активным»), в `record_external_compaction` (403), в `notify_session_compacted` (449-451). Те же ссылки присутствуют в `AGENTS.md` и `docs/ARCHITECTURE.md`.
10. **ПОВЕДЕНЧЕСКОЕ РАСХОЖДЕНИЕ — override-шаблон меняет смысл, а не язык.** `consolidator_locale.apply_template_overrides` работает корректно и идемпотентно (проверено: `consolidator_locale.py:54-61`), но `workspace/overrides/agent/consolidator_archive.md` — это **не** перевод апстримного `nanobot/templates/agent/consolidator_archive.md`. Апстрим (рендерится в `nanobot/agent/memory.py:855-859`) — промпт «create a compact replacement checkpoint for this session». Override — SNIP-промпт «Extract key facts from this conversation…» на английском. То есть после compaction в контекст уходит не сводка сессии, а список memory-фактов. Файл-заголовок при этом заявляет «Консольный шаблон саммарайзера контекста для русских диалогов» — ни то, ни другое.
11. **НЕИДЕМПОТЕНТНОСТЬ `apply_all` держится только на дисциплине вызова.** Из 12 патчей guard есть только у `patch_session_dir_watch` (`_session_dir_watch_patched`, `runtime_patcher.py:1181-1186`). `patch_context_governor`, `patch_save_turn` (881), `patch_assemble_outbound` (1579), `patch_turn_delivery_fail` (1690), `patch_session_content_cleanup` (1067), `patch_document_text_threshold`, `patch_async_session_saves` (создаёт новый `ThreadPoolExecutor` на каждый вызов), `patch_subagent_logging` (2243) — при повторном `apply_all` обернутся дважды. Сейчас безопасно: единственная точка вызова — `application_context.py:467`, а `ApplicationContext.create()` вызывается один раз на процесс (`gateway.py`, `cli_agent.py`, `streamlit_app.py`). Латентный риск, не текущий баг.
12. **МОЛЧАЛИВЫЙ NO-OP при апгрейде nanobot.** `patch_tool_limits` (`runtime_patcher.py:1417-1421`) и `patch_exec_limits` (`1321-1326`) делают голый `setattr` по атрибутам апстрим-классов. Если nanobot переименует `_MAX_CHARS`/`_DEFAULT_MAX`, `setattr` **создаст** новое поле и патч отрапортует `True, "tool limits patched"`, ни разу не изменив поведение. Для наблюдаемости это хуже явной ошибки: баннер покажет «применён», а лимит останется 128K. Контраст: `_bump_schema_max` (`1254-1282`) сначала проверяет `isinstance(prop, property)` и возвращает `False` — корректно.
13. **Каталог патчей (`docs/architecture/runtime-patcher-inventory.md`) совпадает с кодом 1:1 — расхождений нет.** 12 строк таблицы = 12 ключей `_PATCH_SPECS` (`runtime_patcher.py:255-408`) = 12 вызовов в `apply_all` (`610-627`); каждый вызывается ровно один раз, «висячих» и «дважды вызываемых» патчей нет. `required=True` в спеках проставлены у 4 патчей (`context_governor`, `save_turn`, `assemble_outbound`, `subagent_logging`).
14. **Дрейф `docs/architecture/nanobot-inventory.md` (строки не бьются).** `:117` указывает `agent._save_turn` на строке 381 (фактически 813/881), `:118` — `_assemble_outbound` на 748 (фактически 1481/1579), `:90` — `_SubagentHook` на 892 (фактически 2243). `:121-122` перечисляет `auto_compact._archive` (1389) и `auto_compact._ttl` (1479) — таких обращений в коде нет вообще; `:101` ссылается на удалённый `lib/commands/compact_command.py`; `:92-93` приписывают discover `Tool`/`ToolContext` файлу `runtime_patcher.py` (это теперь `project_tool_loader.py:81,160`).
15. **Дублирование `_usage_to_dict`** — `lib/hooks/database_logging_hook.py:253` (импортируется в `runtime_patcher.py:1856`) и вложенная копия в `runtime_events_subscriber.py:217-239`. Две реализации одного duck-typing'а; при изменении формата `LLMUsage` в 0.3.5 разъедутся. → `Слить`.

**Вердикты:** Оставить 96 · Упростить 18 · Удалить 12 · Перенести 0 · Слить с `lib/hooks/database_logging_hook.py` 1 · `НЕ РАЗОБРАНО` 0

---

## `lib/services/runtime_patcher.py` — 2252 LOC (code 1834)

**Назначение.** Единственная точка, где проект monkey-patch'ит upstream `nanobot 0.3.5`: 12 патчей над `AgentLoop`, `SessionManager`, `ContextGovernor`, `TurnDelivery`, `_SubagentHook` и модульными константами tool'ов.
**Что делает.** `apply_all` (`572-628`) последовательно вызывает 12 патчей, каждый возвращает `(ok, detail)`; `_record` (`640-659`) раскладывает результат в `PatchReport` (applied/skipped/failed), `_classify_skip` (`429-442`) решает skip-vs-fail по строковым причинам. Патчи работают на трёх разных уровнях: (а) подмена методов экземпляра (`agent._save_turn`, `agent._assemble_outbound`, `agent.sessions.save`), (б) подмена методов класса (`Session.add_message`, `TurnDelivery.fail`, `_SubagentHook`), (в) мутация модульных/классовых констант (`MAX_OUTPUT_CHARS`, `_MAX_CHARS`, `_MAX_TIMEOUT`) и обёртка `property` (JSON-Schema инструментов). Побочные эффекты: создание `ThreadPoolExecutor` (`1090`), запись в `data_store/` (`781-882`, `689-775`), запись в `agent_gateway_logs` через `DbLoggingService` (`1811-2246`), `setattr` на модулях `nanobot.*`.
**Зачем нужен.** У nanobot нет публичных extension-point'ов для: персиста больших tool-результатов, post-процессинга `OutboundMessage`, конфигурируемых лимитов вывода, неблокирующего `sessions.save`, БД-логирования подагентов, конфигурируемого fallback-текста при internal-ошибке. Без этих 12 патчей теряются данные (`save_turn`), аудит tool-вызовов в UI (`assemble_outbound`), логи подагентов (`subagent_logging`) и наблюдаемость сжатия.
**Вердикт.** `Упростить`
**Обоснование.** Функциональность патчей оправдана и покрыта тестами (13 test-файлов), но файл накопил 4 мёртвых приватных функции, 2 мёртвых параметра, 4 мёртвые строки в `_SKIPPABLE_REASONS`, мёртвую ветку в `_classify_skip`, а также несогласованную идемпотентность (guard у 1 из 12). Каталог `runtime-patcher-inventory.md` при этом точен — правки нужны в коде, не в доках.
**Доказательства.** Импортируют: `lib/core/application_context.py`, `lib/services/runtime_inventory.py`, `tools/demo_internal_fallback.py` + 10 test-файлов. Единственный вызов `apply_all` — `application_context.py:467`.

### Каталог 12 патчей (ответ на вопрос 1)

| # | Патч | Upstream-цель (строка в `runtime_patcher.py`) | Приватный API? | Риск | Что сломается при апгрейде | Вердикт |
|---|---|---|---|---|---|---|
| 1 | `context_governor` | `ContextGovernor.normalize_tool_result` (`689-775`) | нет (публичный метод) | medium | переименование метода/параметров persist → tool-результаты снова обрезаются; патч вернёт `False`, не упадёт | Оставить |
| 2 | `save_turn` | `AgentLoop._save_turn` (`781-882`, подмена на `881`) | **да** (`_`-метод) | high | переименование метода → `False, "agent._save_turn is missing"`; сигнатура изменится → `TypeError` внутри обёртки, т.к. `*args/**kwargs` не используются | Оставить |
| 3 | `exec_limits` | `exec_session.MAX_OUTPUT_CHARS`, `shell.ExecTool._MAX_OUTPUT` (`1288-1339`) | **да** (приватные константы `_MAX_OUTPUT`) | medium | переименование → `setattr` создаст новое поле, патч отрапортует успех (silent no-op, см. находку 12) | Оставить + добавить проверку `hasattr` |
| 4 | `exec_timeout_cap` | `shell.ExecTool._MAX_TIMEOUT` + схема `timeout.maximum` (`1345-1380`) | **да** | medium | то же silent no-op; плюс skip-строка не в `_SKIPPABLE_REASONS` (находка 5) | Оставить + починить классификацию |
| 5 | `tool_limits` | `ReadFileTool._MAX_CHARS`, `ListDirTool._DEFAULT_MAX`, `search._DEFAULT_HEAD_LIMIT`, `GrepTool._MAX_FILE_BYTES` (`1386-1424`) | **да** | medium | silent no-op | Оставить + добавить проверку `hasattr` |
| 6 | `assemble_outbound` | `AgentLoop._assemble_outbound` (`1430-1580`, подмена на `1579`) | **да** | high | сигнатура уже менялась в 0.3.5 (msg, final_content, stop_reason, streamed_content, *, log_content, turn_latency_ms) — обёртка жёстко завязана на неё; следующее изменение = `TypeError` на каждом финале оборота | Оставить (критический путь) |
| 7 | `turn_delivery_fail` | `TurnDelivery.fail` (`1587-1805`) | **да** | medium | изменение сигнатуры → падение в обработчике internal-ошибки, т.е. в момент, когда система уже деградировала | Оставить |
| 8 | `async_save` | `agent.sessions.save` (`1074-1144`) | нет (публичный) | medium | появление собственного async-API → двойная обёртка, лишний executor | Оставить |
| 9 | `session_dir_watch` | `agent.sessions.save` (`1150-1251`) | нет | low | — | Упростить (см. ниже) |
| 10 | `subagent_logging` | `nanobot.agent.subagent._SubagentHook` (`1811-2246`, подмена класса на `2243`) | **да** (приватный класс) | high | переименование `_SubagentHook` → `patch failed: ...`; перенос в другой модуль → `patch failed` (обработка через `except Exception` есть) | Оставить |
| 11 | `document_text_threshold` | `nanobot.utils.document.reference_non_image_attachments` (`888-1033`) | нет (публичная функция) | medium | возврат к `extract_documents` (удалён в 0.3.5) → патч вернёт `False` | Оставить |
| 12 | `session_content_cleanup` | `Session.add_message` (`1035-1068`, подмена класса) | нет (публичный метод) | low | переименование → silent no-op через `setattr` на классе | Оставить |

**Наложений нет:** патчи 8 и 9 на один и тот же `agent.sessions.save` композируются, а не перетирают (8 применяется первым, 9 оборачивает 8) — но именно этот порядок делает обработчик dir-watch недостижимым в async-пути (находка 4). Патча, дублирующего новое поведение самого nanobot 0.3.5, не найдено: `ContextCompactionEvent`/`EventSink` дают наблюдение, но не персист и не post-processing; `AgentHook.finalize_content` не получает `OutboundMessage` (это зафиксировано в `PatchSpec.alternatives_checked`).

### class `ContextWindowNotSeededError` (строки 102–114, 13 LOC), база `RuntimeError`

Назначение: сигнал «мост `_CONTEXT_BRIDGE` не засеян лимитом окна для этой сессии», поднимается в `_attach_context_window:181`.
Зачем: `_attach_context_window` сознательно **не** делает тихий fallback на `agent._last_usage` (обоснование в docstring 110, 138) — иначе метрика M1 молча показывала бы неверную занятость окна.
Вердикт: `Оставить`. Обоснование: fail-loud в метрике — сознательное проектное решение; удаление потребует заменить `raise` на `return`, что вернёт именно то тихое поведение, которое docstring отвергает. Доказательства: `tests/test_runtime_patcher.py:204-206`, `tests/_patcher_fixtures.py`, `tests/test_runtime_patcher_e2e.py:48`.

### class `PatchSpec` (215–252, 38 LOC), `@dataclass(frozen=True)`

Назначение: метаданные патча (`name`, `purpose`, `nanobot_target`, `reason`, `alternatives_checked`, `risk`, `required`).
Зачем: `PatchReport.render(specs=...)` печатает `purpose` под каждой строкой (490-491), `runtime_inventory.canonical_runtime_patches()` (`runtime_inventory.py:174-184`) выводит из него `required` — единственный источник criticality для баннера.
Вердикт: `Оставить`. Обоснование: поля `reason`/`alternatives_checked` — это и есть audit-trail «почему патч вообще существует»; при апгрейде nanobot именно они отвечают на вопрос «есть ли теперь публичная альтернатива». Доказательства: 12 ключей `_PATCH_SPECS` (`255-408`), `runtime_patcher.py:630-638`.

### class `PatchReport` (445–500, 3 метода)

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 459-463 | 4 списка/словаря отчёта | базовое состояние | `apply_all:609` | Оставить |
| `to_dict` | 465-471 | сериализация в dict | диагностика/дамп | 24 файла-ссылки, продуктовых нет | Оставить (публичный контракт отчёта) |
| `render` | 473-500 | человекочитаемая сводка | startup-лог + `diagnose_startup.py` | `application_context.py:478` | Упростить — docstring 481-482 приводит несуществующие `idle_guard`/`compact_tracking`; заменить пример на реальные имена |

### class `_OutboundSilencer` (503–527, 3 метода)

Назначение: прокси-шина, временно подставляемая в `TurnDelivery.bus`, чтобы подавить outbound оригинального `fail()` и не отдать пользователю две строки (upstream-литерал + наш fallback).
Зачем: без него на internal-ошибке пользователь получил бы «Sorry, I encountered an error.» и следом русский fallback.
Вердикт: `Оставить`. Обоснование: `__slots__` + `__getattr__` — минимальный и корректный прокси; `publish_outbound` (526-527) — единственная точка, которую нужно заглушить, всё остальное пробрасывается прозрачно.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 520-521 | сохранить inner-шину | — | `patch_turn_delivery_fail:1792-1802` | Оставить |
| `__getattr__` | 523-524 | проксирование остальных атрибутов | сохраняет контракт `TurnDelivery.bus` | неявно, при обращении `TurnDelivery` к bus | Оставить |
| `publish_outbound` | 526-527 | no-op | подавление дубля | `TurnDelivery.fail` внутри патча | Оставить |

### class `RuntimePatcher` (569–2246, 17 методов)

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `apply_all` | 572-628 | единый вход, 12 патчей | composition stage | `application_context.py:467` | Упростить — убрать мёртвые `cache_store` (602-604) и `bus` (584) |
| `patch_specs` | 630-638 | `dict(_PATCH_SPECS)` | источник для рендера и inventory | `application_context.py:478`, `runtime_inventory.py:183` | Оставить |
| `_record` | 640-659 | раскладка `(ok, detail)` в 3 списка | единая классификация для всех 12 | `apply_all`, 12 call-sites | Оставить |
| `_format_workspace_hint` | 662-681 | «(workspace=…)» для логов | — | **0 вызовов** | Удалить (docstring 665 врёт) |
| `patch_context_governor` | 689-775 | persist больших tool-результатов | не терять вывод | `apply_all:610` | Оставить |
| `patch_save_turn` | 781-882 | архивирование в `data_store` | не терять вывод при сохранении | `apply_all:612` | Оставить |
| `patch_document_text_threshold` | 888-1033 | встраивание текста документов | не заставлять LLM звать `read_file` на каждый PDF | `apply_all:626` | Оставить |
| `patch_session_content_cleanup` | 1035-1068 | чистка NUL/control-chars | NUL валит запись в PG | `apply_all:627` | Оставить |
| `patch_async_session_saves` | 1074-1144 | `sessions.save` → executor | не блокировать event loop | `apply_all:621` | Оставить |
| `patch_session_dir_watch` | 1150-1251 | диагностика `FileNotFoundError` | контекст при падении save | `apply_all:622` | Упростить — порядок в `apply_all` делает `except FileNotFoundError` (1224) недостижимым в async-пути; либо переставить вызовы, либо снять патч |
| `_bump_schema_max` | 1254-1282 | подъём `maximum` в JSON-Schema | агент может заказать больше лимита | `patch_exec_limits:1328,1334`, `patch_exec_timeout_cap:1377` | Оставить (единственный, кто корректно проверяет `property` перед обёрткой) |
| `patch_exec_limits` | 1288-1339 | лимиты вывода exec | дефолт 50K режет вывод | `apply_all:614` | Оставить + проверки `hasattr` |
| `patch_exec_timeout_cap` | 1345-1380 | потолок таймаута 600→3600 | долгие скиллы | `apply_all:615` | Оставить + добавить skip-строку в `_SKIPPABLE_REASONS` |
| `patch_tool_limits` | 1386-1424 | лимиты read_file/grep/list_dir | конфигурируемость | `apply_all:616` | Оставить + проверки `hasattr` |
| `patch_assemble_outbound` | 1430-1580 | `_tool_audit`, media, `context_window` в outbound | UI-метаданные | `apply_all:617` | Оставить (критический путь) |
| `patch_turn_delivery_fail` | 1587-1805 | fallback-текст + `turn_failed` в лог | UX при internal-ошибке | `apply_all:619` | Оставить |
| `patch_subagent_logging` | 1811-2246 | БД-логирование подагентов | наблюдаемость субагентов | `apply_all:624` | Упростить — убрать мёртвый `bus` (1816) |

#### Вложенный class `_SubagentLoggingHook` (1860–2239, 15 методов) — `вложен в patch_subagent_logging`

Подменяет `nanobot.agent.subagent._SubagentHook` целиком (`2243`). Работает в двух режимах: если `RuntimeEventsSubscriber` активен (`_subscriber_registered`, 2131-2145) — только пишет историю и закрывает question-run; иначе — сам пишет `subagent_run_finished` (2146-2193).

| Метод | Строки | Назначение | Зачем нужен | Вердикт |
|---|---|---|---|---|
| `set_subscriber_registered` | 1874-1883 | классовый флаг «подписчик активен» | сигнал для `_finalize` не дублировать запись | Оставить |
| `set_default_bus` | 1886-1899 | классовый `_default_bus` | новые инстансы автоматически публикуют `SubagentTurnCompleted` | Оставить |
| `__init__` | 1901-1924 | состояние хука, `self._bus` из класса | DI | Оставить |
| `_subagent_session_key` | 1926-1929 | ключ подагента | различение в логах | Оставить |
| `_ensure_request` | 1931-1960 | регистрация `question_run` перед первым tool | симметрия с основным хуком | Оставить |
| `_resolve_parent_user_id` | 1962-1990 | user_id родителя | security boundary для `history_search(session_scope="all")` | Оставить |
| `before_execute_tool` | 1992-2008 | проксирование в `DatabaseLoggingHook` | tool-события подагента | Оставить |
| `after_execute_tool` | 2010-2026 | ditto + подмена `session_key` | ditto | Оставить |
| `on_execute_tool_error` | 2028-2044 | ditto для ошибок | ditto | Оставить |
| `after_run` | 2046-2048 | публикация события + finalize | завершение | Оставить |
| `on_error` | 2050-2054 | ditto с `had_error=True` | ошибка | Оставить |
| `_publish_subagent_turn_completed` | 2056-2116 | `bus.publish(SubagentTurnCompleted)` | замена прямой записи в БД на pub-sub | Оставить (fail-soft, 2072-2077) |
| `_finalize` | 2118-2193 | персист истории + итог | единственная запись `subagent_run_finished` | Оставить; обе ветки (с подписником / без) нужны — подписчик может быть не запущен |
| `_extract_task` | 2196-2210 | первое user-сообщение как «задача» | читаемый `summary` | Оставить |
| `_persist_history` | 2212-2239 | `session.add_message` для всех сообщений подагента | история подагента в JSONL | Оставить |

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_getloaded` | 40-49 | вернуть уже импортированный модуль | не форсировать импорт (экономия старта) | `:1315,1316,1368,1412,1413,1631` | Оставить |
| `_session_key_of` | 52-61 | `session_key` из сообщения | фильтр аудита | `:1519` + 3 test-файла | Оставить |
| `_resolve_media_path` | 64-84 | найти путь по basename | — | **0 вызовов** | Удалить (docstring 67 утверждает использование в `patch_document_text_threshold`, но там вызывается инлайн-логика) |
| `_attach_context_window` | 117-211 | внедрить `metadata["context_window"]` | метрика M1 | `patch_assemble_outbound` | Оставить |
| `_classify_skip` | 429-442 | skip-vs-fail | классификация | `_record:656` | Упростить — удалить 4 мёртвые строки из `_SKIPPABLE_REASONS` (417-420) и мёртвый префикс `"idle compact enabled"` (438); ветка 440-441 эквивалентна `return False` на 442 |
| `_resolve_agent_id` | 530-566 | id активного агента | для `patch_turn_delivery_fail` | `apply_all:620` | Оставить |

### Устаревшие строки внутри `runtime_patcher.py`

| Место | Что написано | Факт |
|---|---|---|
| `_SKIPPABLE_REASONS:417-420` | `agent.auto_compact is missing`, `agent.commands is missing`, `auto_compact is missing`, `auto_compact.check_expired is missing` | ни один патч эти строки не возвращает; `auto_compact` в файле не встречается нигде, кроме них |
| `_SKIPPABLE_REASONS:422` | `exec_session/shell module not loaded` | покрывает `patch_exec_limits:1318`, но **не** `patch_exec_timeout_cap:1370` (`shell module not loaded`) |
| `PatchReport.render:481-482` | пример с `idle_guard` и `compact_tracking` | таких патчей нет |
| `_classify_skip:438` | `detail.startswith("idle compact enabled")` | патч `idle_guard` удалён, префикс недостижим |
| `patch_session_dir_watch:1181-1186` | единственный guard `_session_dir_watch_patched` | корректен; остальные 11 патчей аналогичного guard'а не имеют |

---

## `lib/services/context_compaction.py` — 597 LOC (code 530)

**Назначение.** Единая точка запуска сжатия контекста и записи факта сжатия.
**Что делает.** `compact()` (`88-197`) меряет сессию до/после, зовёт штатный `Consolidator.compact_idle_session`, формирует `report` и при `archived > 0` уходит в `_notify` → loguru + (опц.) Rich + `_record_event_log` (`agent_gateway_logs`, `event_type="context_compacted"`) + `_write_history_notice` (`agent_conversation_messages`, `role='assistant'`, `user_id='agent'`). Последняя работает через `utils.db.configure/execute` в `asyncio.to_thread`.
**Зачем нужен.** Даёт единый формат события сжатия для UI и долговечного журнала, доступного агенту через `history_search`.
**Вердикт.** `Упростить`
**Обоснование.** Реально работающих входов **два** (CLI и tool), а не четыре, как заявлено; оба внешних наблюдательных пути (`record_external_compaction`, `notify_session_compacted`) мертвы; module docstring и три docstring'а метода ссылаются на несуществующие обёртки в `runtime_patcher`.
**Доказательства.** Импортируют: `lib/cli/console_loop.py:135`, `workspace/tools/compact_context.py:136`, 4 test-файла. `ContextCompactionService` **не** создаётся в `ApplicationContext` — gateway-path идёт только через tool.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 66-74 | сохранить `agent`/`settings`/db-log, зафиксировать `_section` | — | `console_loop.py:135`, `compact_context.py:136` | Оставить; **но** `console_loop` передаёт `settings=None` (135) → `_section={}` → все три property берут дефолты (`enabled=True`, `notify_in_history=True`, `print_to_terminal=False`), т.е. `gateway.compact.notify_in_history=false` в CLI не действует |
| `enabled` | 77-78 | фича-флаг | — | `compact:114`, `compact_context.py:167` | Оставить |
| `notify_in_history` | 81-82 | писать ли в UI-таблицу | разделение concerns | `_notify:323`, `notify_session_compacted:491` | Оставить |
| `print_to_terminal` | 85-86 | Rich-вывод в терминал gateway | — | `_notify:316` | Оставить (в gateway дефолт `false`) |
| `compact` | 88-197 | основной сценарий | — | `console_loop`, tool | Упростить — ветка 147-160 (token-budget) недостижима: оба вызывающих места передают `force=True` (см. находку ниже) |
| `format_report` | 200-234 | текст отчёта | UI + `summary` события | `_notify:310`, `_write_history_notice:543` | Оставить |
| `_estimate` | 236-253 | токены через апстрим | метрики | `compact:134,170` | Оставить |
| `_estimate_fallback` | 255-285 | ~4 симв/токен | не врать «0 токенов» | `_estimate:253` | Упростить — три замечания: (а) `except` на 284-285 возвращает ровно `(0, "")`, т.е. противоречит собственному docstring; (б) не учитывает `reasoning_content`/`tool_calls`, поэтому систематически занижает; (в) считает только `content`, игнорируя `tool_calls` в assistant-сообщениях |
| `_current_session_key` | 287-293 | session_key текущего request | — | `compact:117` | Оставить |
| `_empty` | 295-307 | «ничего не сделали» отчёт | единый контракт | 7 call-sites | Оставить |
| `_notify` | 309-324 | loguru + Rich + event_log + history | — | `compact:190`, `record_external_compaction:433` | Оставить |
| `_record_event_log` | 326-388 | `context_compacted` в `agent_gateway_logs` | обещание из `description` tool'а `history_search` | `_notify:322`, `notify_session_compacted:470` | Упростить — docstring 343 ссылается на несуществующий `patch_compaction_tracking` |
| `record_external_compaction` | 390-435 | запись факта чужого сжатия | — | **0 продуктовых вызовов** | Удалить (docstring 403-410 описывает несуществующий `patch_compaction_tracking`; после удаления — 2 вызова теста) |
| `notify_session_compacted` | 437-516 | запись upstream-фазы | — | **только** `CompactionEventSubscriber.feed:91`, а тот никогда не вызывается | Упростить — связать с судьбой подписчика (находка 1) |
| `_write_history_notice` | 518-574 | INSERT в таблицу канала | UI-стикер | `_notify:324`, `notify_session_compacted:497` | Оставить с оговоркой (кросс-подсистемный риск, см. ниже) |

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_get_setting` | 47-60 | чтение из dict-или-объекта | — | `__init__:74`, `_write_history_notice:534` | Оставить |
| `_current_request_sender_id` | 577-597 | `user_id` текущего request | security boundary `history_search(scope="all")` | `_record_event_log:375` | Оставить |

**Ответ на вопрос 6 (4 входа и `force=True`).** Из четырёх заявленных входов:
- `/compact` upstream (`nanobot/command/builtin.py::cmd_compact`) — **не проходит** через `ContextCompactionService` вообще: локального `patch_compact_command` не существует; факт сжатия по этому пути не пишется.
- CLI (`console_loop.py::_run_cli_compact`) — `force=True` ✔.
- tool `compact_context` — `force: bool = True` по умолчанию (`workspace/tools/compact_context.py:164`) ✔.
- авто-сжатие (`AutoCompact._archive`, `maybe_consolidate_by_tokens`) — обёрток нет, `record_external_compaction` не вызывается ✘.

Дублирования записи события нет: `_notify` — единственная точка, и она вызывается ровно из двух мест, каждое из которых недостижимо из второго.

---

## `lib/services/runtime_inventory.py` — 370 LOC (code 308)

**Назначение.** Single source of truth для ожидаемых хуков, project tools и runtime-патчей + функции сравнения с фактическим рантаймом.
**Что делает.** Пять `canonical_*()` возвращают захардкоженные списки (патчи — единственное исключение, они строятся из `RuntimePatcher.patch_specs()`); три `diff_*` считают `missing_required` / `missing_optional` / `unexpected` / `failed`; `parse_project_tools_detail` парсит строку `detail` регулярками.
**Зачем нужен.** Ловит расхождение «канон ↔ рантайм» в баннерах `ApplicationContext` и в `tools/diagnose_startup.py` (exit 1/2).
**Вердикт.** `Упростить`
**Обоснование.** Механизм работает и вскрывает реальный дрейф, но содержит одну ошибочную запись в каноне (блокирующий баг №2), мёртвую обёртку и мёртвое поле с устаревшими значениями.
**Доказательства.** Импортируют: `lib/core/application_context.py`, `tools/diagnose_startup.py`, 3 test-файла.

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `canonical_framework_hooks` | 68-85 | ToolAuditHook, TerminalToolPrintHook | сверка `ctx.hooks` | `diff_hooks:213` | Оставить — совпадает с `lib/hooks/` (3 файла, `DatabaseLoggingHook` идёт через factories) |
| `canonical_plugin_hooks` | 88-112 | SessionFileRedirectHook, RecentFilesHook, StreamDiagnosisHook | сверка плагинов | `diff_hooks:214` | Оставить — совпадает с `workspace/hooks/` (3 файла). `StreamDiagnosisHook` помечен `required=False` + «REMOVED» в description — честно, но `diff_hooks` не различает removed и optional |
| `canonical_hook_factories` | 115-125 | DatabaseLoggingHook | сверка числа factories | `diff_hooks:215` | Оставить |
| `canonical_project_tools` | 128-159 | 4 tool'а | сверка | `diff_project_tools:261` | **Исправить** — `name="ExampleTool"` (153) ≠ `example_tool` (`example.py:117`). Плюс `config_key` (136, 143) не соответствует реальным `config_key` классов (`compact`, `history_search`) |
| `canonical_runtime_patches` | 162-184 | 12 патчей из `patch_specs()` | сверка | `diff_runtime_patches:302` | Оставить — единственная функция, генерирующая канон из кода; дрейф невозможен по построению |
| `collect_actual_hook_names` | 187-195 | `(имена, число factories)` из ctx | — | `application_context.py` (`_log_connected_hooks`) | Оставить |
| `diff_hooks` | 198-237 | 4 категории расхождений | баннер | `application_context.py`, `diagnose_startup.py:199` | Оставить. Замечание: `missing_factory` (228-230) считает по **индексу** (`factories[actual_factory_count:]`), а не по именам — при `factory_count=1` отсутствующей всегда будет `factories[1:]` = `[]`, т.е. категория почти не срабатывает |
| `diff_project_tools` | 240-282 | 4 категории | баннер + exit-code | `application_context.py:936`, `diagnose_startup.py:203` | Оставить (после правки канона) |
| `diff_runtime_patches` | 285-324 | 3 категории | баннер | `application_context.py`, `diagnose_startup.py:208` | Оставить |
| `parse_project_tools_detail` | 327-360 | regex-парс `detail` | для `diagnose_startup.py`-совместимости | 3 test-файла + docstring-ссылки | Оставить; regex `r"registered(?::\s*([^;]*))?"` (348) жадный до `;` — совместимо с форматом, но хрупко к любой новой секции в `detail` |
| `diff_project_tools_from_detail` | 363-370 | обёртка parse+diff | — | **0 ссылок** (в т.ч. в тестах) | Удалить — заменён на `diff_project_tools` по структурным полям (`application_context.py:936`); `tests/test_tools_project_loader.py:503` фиксирует, что баннер раньше вызывал именно её |

| Класс | Строки | Назначение | Вердикт |
|---|---|---|---|
| `HookSpec` | 37-44 | `name`/`kind`/`required`/`description`/`source` | Оставить — `source` читается только человеком, но это канон-документация |
| `ToolSpec` | 48-55 | `name`/`module`/`required`/`description`/`config_key` | Упростить — поле `config_key` **не читается нигде** (проверено: `config_key` встречается только в определении поля и в 4 литералах), а его значения (`tools.compact_context.enable`, `tools.history_search.enable`) не соответствуют реальным `config_key="compact"`/`"history_search"` и чтению из `settings.gateway.compact` (`compact_context.py:114`) |
| `RuntimePatchSpec` | 59-65 | проекция `PatchSpec` | Оставить |

---

## `lib/services/project_tool_loader.py` — 349 LOC (code 304)

**Назначение.** Статeless-loader кастомных tool'ов из `workspace/tools/*.py` — отдельный stage composition root'а.
**Что делает.** `_discover` импортирует каждый `*.py` из каталога через `importlib.util.spec_from_file_location` под именем `workspace.tools.<name>`, затем собирает не-абстрактные `Tool`-подклассы из **всего** `sys.modules` с префиксом `workspace.tools.` (123-141) и дедуплицирует по `id(cls)`. `register_project_tools` для каждого класса: `_canonical_name` → `enabled(ctx)` → `create(ctx)` → проверка дубликата в `agent.tools` → `set_provider`/`set_connection_factory` → `agent.tools.register`. Возвращает `ProjectToolsLoadResult` со структурными полями.
**Зачем нужен.** Вынесен из `RuntimePatcher` (change `runtime-patcher-composition-cleanup`), чтобы загрузка tool'ов не была побочным эффектом патчера; даёт `ApplicationContext` структурный результат для баннера.
**Вердикт.** `Оставить` (с одной локальной правкой)
**Обоснование.** Границы модуля корректны — дублирования/legacy-пути в `runtime_patcher.py` не осталось (вопрос 7: ни `patch_project_tools`, ни `_discover`, ни импортов `Tool`/`ToolContext` в `runtime_patcher.py` нет; `tests/test_runtime_patcher_no_project_tools_boundary.py` это фиксирует). Проблема одна — в `_discover`: сканирование `sys.modules` вместо списка только что импортированных модулей означает, что любой модуль, положенный в `sys.modules` под `workspace.tools.*` кем угодно, попадёт в кандидаты.
**Доказательства.** Импортируют: `lib/core/application_context.py:493-501`, 5 test-файлов.

| Элемент | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `ProjectToolsLoadResult` | 39-78 | `registered`/`disabled`/`duplicate`/`failed`/`detail`/`error` | вход для баннера | `application_context.py:502`, `_emit_project_tools_inventory_banner` | Оставить |
| `_discover` | 81-141 | импорт + сбор классов | — | `register_project_tools:263` | Оставить; сузить до возврата `(mod_name, module)` из первого цикла вместо `sys.modules` |
| `_build_tool_context` | 144-193 | `ToolContext` + 4 DI-`setattr` | доставить `agent`/`settings`/`cache`/`db_logging` в tool'ы | `register_project_tools:267` | Оставить; docstring 148-149 ссылается на `runtime_patcher.py:2392-2417` pre-change — ссылка мертва |
| `register_project_tools` | 196-349 | публичный контракт | — | `application_context.py:495` | Оставить |
| `_canonical_name` (вложена в 196) | 238-249 | `cls().name` → fallback `cls.__name__` | имя для disabled/failed | `register_project_tools:275` | Упростить — у **всех 4** tool'ов `__init__` требует kwarg (`compact_context.py:143` `service`, `example.py:112` `config`, `history_search_tool.py:199` `config`, `legal_summarizer_query.py:168` `config`), поэтому `cls()` всегда бросает `TypeError` и функция **всегда** возвращает `cls.__name__`. Т.е. в `disabled`/`failed` попадают `CompactContextTool`/`ExampleTool`, а не канонические `compact_context`/`example_tool` — и `diff_project_tools` их не сматчит |

---

## `lib/services/runtime_events_subscriber.py` — 334 LOC (code 277)

**Назначение.** Подписчик на публичный pub-sub `nanobot.bus.queue.MessageBus` для трёх runtime-событий.
**Что делает.** `start()` (`117-156`) регистрирует три `bus.subscribe`, затем через две module-функции прокидывает `bus` и флаг подписчика в подменённый `_SubagentLoggingHook`. `stop()` (`158-176`) дерегистрирует в LIFO и откатывает флаг. Хендлеры пишут `turn_completed` (метрики оборота) и `subagent_run_finished` (метрики подагента) в `agent_gateway_logs`.
**Зачем нужен.** `_handle_turn_runtime_admitted` (`178-198`) сеет `_CONTEXT_BRIDGE` — без него `patch_assemble_outbound` → `_attach_context_window` поднимает `ContextWindowNotSeededError` на каждом финале оборота. Это критическая связка: подписчик — не «наблюдаемость», а предусловие работы метрики M1.
**Вердикт.** `Оставить`
**Обоснование.** Единственный безопасный (публичный API) источник turn-метрик; `start()` идемпотентен (128-133). Единственная проблема — дублирование `_usage_to_dict`.
**Доказательства.** `lib/core/application_context.py:560-574` (start) и `:646-653` (stop, после `bus.drain()`), `tests/test_runtime_events_subscriber.py`, `tests/test_application_context_role.py`.

| Метод | Строки | Назначение | Зачем нужен | Вердикт |
|---|---|---|---|---|
| `__init__` | 107-115 | состояние + `_unsubscribers` | — | Оставить |
| `start` | 117-156 | 3 подписки + wiring подагента | — | Оставить |
| `stop` | 158-176 | LIFO-отписка + откат флага | in-flight handlers | Оставить |
| `_handle_turn_runtime_admitted` | 178-198 | `seed_context_window` | предусловие M1 | Оставить |
| `_handle_turn_completed` | 200-281 | `turn_completed` в лог | метрики оборота | Оставить |
| `_handle_subagent_turn_completed` | 283-331 | `subagent_run_finished` в лог | метрики подагента | Оставить; docstring 288-289 указывает строки `runtime_patcher.py:1712-1737` — фактически 2158-2183 (ссылка устарела) |
| `_usage_to_dict` (вложена в 200) | 217-239 | duck-typing usage | — | **Слить с `lib/hooks/database_logging_hook.py:253`** — идентичная логика, уже импортируется в `runtime_patcher.py:1856` |

| Функция | Строки | Назначение | Кто вызывает | Вердикт |
|---|---|---|---|---|
| `_set_subagent_default_bus` | 58-76 | `set_default_bus(bus)` на подменённом классе | `start:147` | Оставить |
| `_set_subagent_subscriber_registered` | 79-95 | флаг «подписчик активен» | `start:151`, `stop:175` | Оставить |

---

## `lib/services/compaction_event_subscriber.py` — 106 LOC (code 90)

**Назначение.** Фильтр `OutboundMessage.event` на `ContextCompactionEvent` → `ContextCompactionService.notify_session_compacted`.
**Что делает.** `feed` (`64-106`) достаёт `event`, `session_key`, `compaction_id`, `phase`; при совпадении типа зовёт сервис. Полностью fail-soft (каждый шаг в `try`).
**Зачем нужен.** Upstream `ContextCompactionEvent` (`nanobot/events.py:17`) роутится на `"channel"` (`nanobot/bus/notification_delivery.py:21`) и превращается в `OutboundMessage` (`nanobot/bus/outbound_events.py:115-126`); без наблюдателя фазы `started/succeeded/failed/cancelled` не попадают в журнал. Архитектурно место вызова (канал) выбрано верно — канал не знает о бизнес-логике.
**Вердикт.** `Удалить` (либо `Оставить` + выполнить контракт — решение зависит от продуктового намерения)
**Обоснование.** **Функциональный баг №1.** Единственный продуктовый конструктор отсутствует. Три из четырёх требуемых docstring'ом точек вызова отсутствуют полностью, четвёртая (`PostgresChannel.__init__` принимает `compaction_event_subscriber`) не заполняется фабрикой. В результате 73 LOC продуктивного кода + 80 LOC тестов (`tests/test_compaction_event_subscriber.py`) обслуживают путь, который не выполняется ни разу. Если наблюдение upstream-компакции нужно — чинить (`channel_factory.py:179-181` + `ApplicationContext` + `postgres_channel.send`/`redis_channel.send`/`streamlit_app._render_agent`/`console_loop._run_cli_compact`); если не нужно — удалить файл, `notify_session_compacted` и `record_external_compaction`, и убрать соответствующие обещания из docstring'ов.
**Доказательства.** `grep` по `lib/`, `workspace/`, `tools/`, `*.py` → 0 конструкторов; единственные упоминания — `postgres_channel.py:123` (комментарий), `postgres_channel.py:1581` (комментарий), `context_compaction.py:159` (docstring), `tests/test_compaction_event_subscriber.py`.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 48-53 | `compaction_service=None` | DI | **никто** | Удалить вместе с классом (или `Оставить` при починке) |
| `set_service` | 55-62 | ленивая привязка сервиса | docstring 58-60 обещает «`RuntimePatcher.apply_all` создаёт сервис позже» — неверно, `apply_all` не создаёт сервис вовсе | **никто** | Удалить |
| `feed` | 64-106 | фильтр + вызов | — | `postgres_channel.py` только при наличии инстанса (всегда `None`) | Удалить / Оставить после починки |

---

## `lib/services/consolidator_locale.py` — 64 LOC (code 52)

**Назначение.** Подложить `workspace/overrides/` в Jinja2-loader шаблонов nanobot, чтобы переопределить системный промпт консолидатора.
**Что делает.** Берёт `lru_cache`-нутую `prompt_templates._environment()` (единственный объект `Environment` на процесс) и заменяет её `loader` на `ChoiceLoader([FileSystemLoader(overrides), current])`.
**Зачем нужен.** Публичного способа переопределить `agent/consolidator_archive.md` через конфиг nanobot нет.
**Вердикт.** `Оставить` (с содержательной оговоркой)
**Обоснование.** Механизм корректен и идемпотентен: повторный вызов находит свой `FileSystemLoader` по `searchpath[0]` и возвращает `True` без новой обёртки (`54-61`), поэтому `ChoiceLoader` не вкладывается дважды. Риск кэша Jinja2 (`auto_reload=True`, `FileSystemLoader` даёт `uptodate`) снят корректно: после подмены loader'а `Template.is_up_to_date` переоценивается против **нового** loader'а, поэтому ранее закэшированный апстрим-шаблон будет перезагружен. `apply_template_overrides()` вызывается в `ApplicationContext.start()` (`application_context.py:530-535`) — до первых оборотов. **Но** содержимое override'а не соответствует назначению (находка 10), и docstring 5 указывает `nanobot/agent/memory.py:946` вместо фактической `:855`.
**Доказательства.** `lib/core/application_context.py:530-535`, `tests/test_consolidator_locale.py`, `tests/test_tools_and_context.py`.

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_overrides_dir` | 30-33 | `<repo>/workspace/overrides` | — | `apply_template_overrides:47` | Оставить |
| `apply_template_overrides` | 36-64 | идемпотентная подмена loader'а | — | `application_context.py:532` | Оставить; требует решения по содержимому override'а |

---

## `lib/services/schema_validation.py` — 284 LOC (code 237)

**Назначение.** Pre-startup проверка наличия 6 runtime-таблиц одним SELECT'ом к `information_schema.tables`.
**Что делает.** `expected_table_names` (`172-202`) обходит `_EXPECTED_KEYS` (6 путей в merged SETTINGS: 4 из `channels.postgres`, 2 из `logging.db`), `_unwrap_settings` разворачивает `_LazySettings` proxy. `check_tables` (`204-243`) строит один `SELECT … WHERE table_schema IN (…) AND table_type='BASE TABLE' AND table_name IN (…)` и возвращает `MissingTable` для отсутствующих. `validate` (`246-275`) — верхний уровень, бросает `SchemaValidationError`.
**Зачем нужен.** Блокирует старт (`exit 2` + stderr в `gateway.main()`/`cli_agent.main()`) с actionable-подсказкой вместо того, чтобы падать позже на первом же запросе.
**Вердикт.** `Оставить` (с обязательной правкой схемы)
**Обоснование.** Ответ на вопрос 8: **имена таблиц** действительно резолвятся из merged SETTINGS, ничего не зашито — 6 путей в `_EXPECTED_KEYS` (`37`-область) покрывают `channels.postgres.{table_name, messages_table, meta_table, claims_table}` + `logging.db.{table_name, question_runs_table}`. **Схема** зашита как `"public"` (строка 198) — функциональный баг №3. Плюс `check_tables` явно игнорирует `timeout_sec` (`del timeout_sec`, 223) — таймаут обещан сигнатурой и docstring, но реализован «на уровне пула», что не гарантировано.
**Доказательства.** `lib/core/application_context.py:705-730` (с конфиг-гейтом `gateway.startup.schema_validation.enabled`), 3 test-файла.

| Класс / метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `MissingTable` | 85-99 | `(schema, name, hint)` | сообщение об ошибке | `check_tables:240`, `_hint_for_profile` | Оставить |
| `MissingTable.full_name` | 97-99 | `schema.table` | текст ошибки | `_build_message` | Оставить |
| `SchemaValidationError` | 102-123 | наследник `ConfigurationError` | единый startup-boundary | `application_context.py`, entrypoints | Оставить |
| `SchemaValidationError.__init__` | 109-112 | принять `missing` + `profile` | — | `_MissingConfigKeys.__init__:137` | Оставить |
| `SchemaValidationError._build_message` | 114-123 | «нет таблиц» + хинты | actionable | `__init__` | Оставить |
| `_MissingConfigKeys` | 126-153 | «нет ключей в settings» | тот же класс, чтобы entrypoint не различал | `expected_table_names:201` | Оставить |
| `_MissingConfigKeys.__init__` | 135-139 | принять `missing_keys` | — | `:201` | Оставить |
| `_MissingConfigKeys._build_config_message` | 141-153 | отдельный формат сообщения | — | `__init__` | Оставить |
| `SchemaValidationService` | 156-275 | сервис | — | `application_context.py:726` | Оставить |
| `expected_table_names` | 172-202 | `(schema, name)[]` из settings | — | `validate:255` | **Исправить** строку 198 |
| `check_tables` | 204-243 | один SELECT | дёшево | `validate:262` | Оставить; убрать `timeout_sec` из сигнатуры или задокументировать, что он игнорируется (сейчас docstring 217-218 это признаёт) |
| `validate` | 246-275 | оркестратор | — | `application_context.py:726` | Оставить |

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_hint_for_profile` | 38-58 | CLI-подсказка по профилю | actionable | `_build_message` | Оставить |
| `_unwrap_settings` | 61-81 | `_LazySettings` → dict | — | `expected_table_names:184` | Оставить |

---

## `lib/services/runtime_health.py` — 210 LOC (code 169)

**Назначение.** Liveness (`RuntimeHealth`) и readiness по зависимостям (`RuntimeReadiness`).
**Что делает.** `RuntimeHealth` — 3 флага (`_started_at`, `_stopped`) + uptime. `RuntimeReadiness.register/check` (`163-205`) — список предикатов, каждый в своём `try`; `compute_overall_status` сводит в `READY`/`DEGRADED`/`NOT_READY`.
**Зачем нужен.** `/health` gateway'а и readiness-лог в `ApplicationContext.start()`.
**Вердикт.** `Оставить`
**Обоснование.** Модуль честно разделён на два понятия и не делает вид, что `is_alive()` ловит зависание (docstring 105-107 это признаёт). Мёртвого кода нет; единственная мелочь — неиспользуемый импорт.
**Доказательства.** `lib/core/application_context.py:439-450` (создание), `:509`/`:524`/`:675` (`mark_started`/`mark_stopped`), `:998-1055+` (`_register_readiness_checks`), `tests/test_runtime_health.py`, `tests/test_application_context_role.py`, `tests/test_cache_readiness_and_skill_role.py`.

| Класс / метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `ComponentStatus` | 36-52 | `(name, required, status, detail)` | результат одного чека | `_register_readiness_checks`, `check:186-198` | Оставить |
| `ReadinessReport` | 55-78 | агрегат | выдача наружу | `check:205` | Оставить |
| `ReadinessReport.to_dict` | 66-78 | сериализация | `/health` | `application_context.py` | Оставить |
| `compute_overall_status` | 81-99 | сводка статусов | — | `check:204` | Оставить |
| `RuntimeHealth` | 102-145 | liveness | — | `application_context.py` | Оставить |
| `RuntimeHealth.__init__` | 110-112 | 2 поля | — | `application_context.py:451` | Оставить |
| `mark_started` | 114-115 | `_started_at = now` | — | `application_context.py:509,524` | Оставить |
| `mark_stopped` | 117-118 | `_stopped = True` | `/health` после shutdown | `application_context.py:675` | Оставить |
| `is_alive` | 120-125 | «не остановлен и стартовал» | — | `status:128` | Оставить; при `_started_at is None` (до `mark_started`) возвращает `False` — т.е. `/health` до `start()` отдаёт `DEAD`. Проверено: `create()` вызывает `mark_started` на 509, но `RuntimeHealth()` создаётся на 451 — окно между ними узкое и не проверялось в тестах |
| `status` | 127-128 | ALIVE/DEAD | — | `application_context.py` | Оставить |
| `get_stats` | 130-145 | started_at/uptime/stopped | `/health` | `application_context.py` | Оставить; docstring 134-136 ссылается на «design D-Pool.6 и D20» — в openspec не проверял |
| `RuntimeReadiness` | 148-205 | реестр чеков | — | `application_context.py:450` | Оставить |
| `RuntimeReadiness.__init__` | 160-161 | `_checks: list` | — | `:450` | Оставить |
| `register` | 163-173 | добавить предикат | — | `_register_readiness_checks` | Оставить |
| `check` | 175-205 | прогон + агрегация | — | `application_context.py` | Оставить; `raise TypeError` на 200-203 вне `try` — неверный тип чека уронит весь `/health`, а не один компонент (в отличие от исключения в самом чеке). Низкий риск, т.к. все чеки в `_register_readiness_checks` возвращают `ComponentStatus | None` |
| `_now` | 208-210 | `time.time()` | — | `mark_started`, `get_stats` | Оставить |

Косметика: `from dataclasses import dataclass, field` (`29`) — `field` не используется ни разу.

---

## `lib/services/channel_factory.py` — 191 LOC (code 155)

**Назначение.** Создание и настройка каналов: штатный `ChannelManager` + наш Redis + наш Postgres.
**Что делает.** `create_all` (`44-78`) конструирует `ChannelManager(config, bus, session_manager=...)`, добавляет Redis и Postgres, печатает список включённых. `_add_redis`/`_add_postgres` читают `settings.channels.*`, собирают конфиг-дикты и кладут каналы в `channels.channels[<name>]`. Транскрипцию пробрасывает **присваиванием атрибутов** после конструктора (`182-186`).
**Зачем нужен.** Postgres-канал — основной транспорт агент↔бизнес-процессы↔Streamlit; без фабрики его не было бы в реестре.
**Вердикт.** `Оставить` (с обязательной правкой — см. обоснование)
**Обоснование.** Найден один функциональный баг №1 (не передан `compaction_event_subscriber`, строки 179-181) и одна архитектурная шероховатость: `transcription_provider`/`transcription_api_key`/`transcription_api_base`/`transcription_language` ставятся на инстанс **после** создания, тогда как у `PostgresChannel` есть для этого параметр конструктора (тот же паттерн, что у `db_logging_service`) — хрупко, но работает.
**Доказательства.** `gateway.py`, `tests/test_channel_factory.py`, `tests/test_gateway.py`, `tests/test_gateway_live_media_e2e.py`, `tests/test_parallel_modes.py`.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 34-42 | 3 поля DI | — | `ApplicationContext`, `gateway.py` | Оставить |
| `create_all` | 44-78 | оркестратор | — | `ApplicationContext`, `gateway.py` | Оставить; `session_manager` передаётся в `ChannelManager` и больше нигде не используется — оправданно |
| `_add_redis` | 84-123 | регистрация Redis-канала | — | `create_all:72` | Оставить |
| `_add_postgres` | 129-191 | регистрация Postgres-канала | — | `create_all:73` | **Исправить** — добавить `compaction_event_subscriber=` (баг №1) либо удалить соответствующий параметр из `PostgresChannel.__init__`, если наблюдатель не планируется |

---

## Кросс-подсистемные находки

1. **`ContextCompactionService` не в composition root** (`lib/services/context_compaction.py`). Его конструируют только `lib/cli/console_loop.py:135` (с `settings=None`!) и `workspace/tools/compact_context.py:136`. `ApplicationContext` его не создаёт, поэтому в gateway вся запись факта сжатия зависит от того, вызвал ли агент tool. Это же объясняет, почему `record_external_compaction`/`notify_session_compacted` остались «нужными, но не подключёнными».
2. **Общее глобальное состояние БД из threadpool-потока.** `context_compaction._write_history_notice` (`554-570`) вызывает `utils.db.configure(dsn)` + `execute(...)` — модульные глобалы — из `asyncio.to_thread`, одновременно с `lib/channels/postgres_channel.py`, который использует тот же `utils.db`. Гонка состояния соединения между каналом и записью history-notice не защищена. Риск реальный, но низкочастотный (только при сжатии).
3. **Дублирование `_usage_to_dict`**: `lib/hooks/database_logging_hook.py:253` и `lib/services/runtime_events_subscriber.py:217-239`. Логика duck-typing'а по `LLMUsage` (`to_dict` → `to_turn_dict` → атрибут `total_tokens`) идентична. При формальном изменении API в 0.3.5 разъедутся незаметно, т.к. обе деградируют в `None`/`{"total_tokens": ...}` без ошибки.
4. **Дрейф документации по всей подсистеме.** `AGENTS.md` (разделы про `runtime_patcher.py`, `context_compaction.py`), `docs/ARCHITECTURE.md`, `docs/architecture/nanobot-inventory.md` (строки 90, 101, 117, 118, 121, 122, 92, 93) и docstring'и `context_compaction.py` (7, 8, 17, 23, 343, 403, 449) описывают несуществующие `patch_compaction_tracking`, `patch_compact_command`, `_wrap_auto_compact_archive`, `_wrap_maybe_consolidate_by_tokens`, `lib/commands/compact_command.py`, `auto_compact._archive`, `auto_compact._ttl`. Каталог `docs/architecture/runtime-patcher-inventory.md` — единственный, который точен.
5. **Скрытый контракт, не enforced нигде:** порядок `async_save` → `session_dir_watch` в `apply_all` определяет достижимость обработчика исключений (находка 4). Он зафиксирован только порядком строк; ни тест, ни комментарий его не защищает.
6. **Скрытый контракт, не enforced:** `ContextWindowNotSeededError` требует, чтобы `RuntimeEventsSubscriber.start()` отработал **раньше** первого оборота. Порядок в `application_context.py:467` (apply_all) → `:530` (overrides) → `:560-574` (subscriber.start) соблюдён, и `_attach_context_window` падает наружу, если seed не произошёл. При вызове `ApplicationContext.create()` без `start()` (например, в `tests/test_application_context.py`) первый же финал оборота упадёт — это проверено `tests/_patcher_fixtures.py`, но production-путь полагается на порядок, а не на контракт.

---

## Порядок предлагаемых действий (по убыванию влияния)

1. `lib/services/channel_factory.py:179-181` — передать `compaction_event_subscriber` (или удалить его из `PostgresChannel`). Разблокирует наблюдаемость upstream-компакции.
2. `lib/services/runtime_inventory.py:153` — `ExampleTool` → `example_tool`. Гасит ложный DRIFT-баннер и ненулевой exit-code `diagnose_startup.py`.
3. `lib/services/schema_validation.py:198` — использовать схему из `channels.postgres.schema`.
4. `lib/services/runtime_patcher.py:621-623` — переставить `session_dir_watch` перед `async_save` (или снять патч как диагностический).
5. `lib/services/runtime_patcher.py:411-426` — удалить 4 мёртвые skip-строки, добавить `"shell module not loaded"`.
6. `lib/services/runtime_patcher.py:64-84, 662-681` — удалить `_resolve_media_path` и `_format_workspace_hint`; `:584, 1816` — убрать мёртвый `bus`; `:602-604` — убрать `cache_store`.
7. `lib/services/runtime_inventory.py:363-370` — удалить `diff_project_tools_from_detail`; `:48-55` — удалить поле `config_key` из `ToolSpec`.
8. Решить судьбу `compaction_event_subscriber.py` + `context_compaction.record_external_compaction`/`notify_session_compacted` вместе с `context_compaction.py:1-38, 343, 403` и соответствующими разделами `AGENTS.md` / `docs/ARCHITECTURE.md`.
9. `lib/services/project_tool_loader.py:238-249` — использовать каноническое имя без создания инстанса (например, classmethod-декоратор или маппинг `module → name`).
10. `lib/services/runtime_patcher.py:1417-1421, 1321-1326` — добавить `hasattr`-проверки, чтобы патч не рапортовал успех при silent no-op.
