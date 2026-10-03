# Аудит: services — runtime и data

## Сводка группы

Файлов: 19 · LOC: 8936 · классов: 47 · методов: 181 · функций: 56 · всего символов: 284

**НЕ РАЗОБРАНО: 0.** Каждый класс, метод и функция из `docs/audit/_data/inventory.json`
получили строку в таблицах ниже.

> Отчёт написан по свежему `inventory.json` и текущему дереву на коммите
> `f9310da` (ветка `refactor/mcp-platform`). Обратите внимание: в репозитории
> лежат более ранние отчёты `docs/audit/reports/03-services-runtime.md` и
> `04-services-data.md` с **устаревшими номерами строк** (например, `_worker`
> журналирования там `:725–796`, фактически `:1463–1534`). Для действий
> ориентируйтесь на номера строк настоящего отчёта.

### Ключевые находки

1. `lib/services/runtime_patcher.py:249-331` — `_PATCH_SPECS` содержит ровно **6** патчей, имена и порядок совпадают с каноном AGENTS.md (`exec_limits`, `exec_timeout_cap`, `tool_limits`, `assemble_outbound`, `subagent_logging`, `repeat_guard_block`). Дрейфа нет. Для всех шести `docs/architecture/runtime-patcher-inventory.md:49-54` содержит заполненное «Условие удаления». **Расхождений с каноном не найдено.**
2. `lib/services/runtime_patcher.py:422-458` — `_resolve_agent_id()` **мёртвая**: 0 вызовов во всём репо. Единственный потребитель, `patch_turn_delivery_fail`, удалён; осталась копия той же функции в `lib/core/application_context.py:1269`. **Удалить, 37 LOC.**
3. `lib/services/runtime_patcher.py:464-512` — `apply_all()` принимает `config` и `workspace_dir`, но **не использует ни один из двух** в теле (строки 503-512). Оба остались от снятого `patch_turn_delivery_fail`/`patch_project_tools`. **Упростить сигнатуру.**
4. `lib/services/runtime_patcher.py:264-265, 643, 649` — обоснование патча `exec_timeout_cap` построено на навыке `legal_summarizer` («7–10 мин», «см. SKILL.md legal_summarizer»), который `AGENTS.md:58` объявляет **удалённым из репозитория целиком**. «Условие удаления» в `runtime-patcher-inventory.md:50` отсылает к уже выполненной фазе 11. Обоснование патча — **строка, которая лжёт**. Код, вероятно, ещё нужен (другие долгие `exec`), но обоснование надо переписать.
5. `lib/services/runtime_patcher.py:10` — докстринг говорит «Фаза 6 вынесла **пять** патчей», а перечисляет **шесть** (`save_turn`, `document_text_threshold`, `session_content_cleanup`, `async_save`, `session_dir_watch`, `turn_delivery_fail`) + `assemble_outbound`. Арифметика не сходится.
6. `lib/services/runtime_inventory.py:387-394` — `diff_project_tools_from_detail()` 0 ссылок; `lib/services/runtime_inventory.py:351-384` — `parse_project_tools_detail()` жива **только из тестов** (`tests/test_runtime_inventory.py:365,380,391`). Продакшн давно читает структурные поля (`lib/core/application_context.py:1055`, `tools/diagnose_startup.py:212`). **Удалить оба, 42 LOC.**
7. `lib/services/runtime_inventory.py:146` — `ToolSpec.config_key="tools.compact_context.enable"` **фактически неверен**: tool читает `gateway.compact.*` (`workspace/tools/compact_context.py:24,101`), о чём честно сказано в соседнем `description` на строке 145. Поле читается только одним тестом, который проверяет лишь префикс `tools.` (`tests/test_runtime_inventory.py:122-125`). Поле-обман → **Удалить**.
8. `lib/services/context_compaction.py:153-165` — ветка token-budget **мёртвая**: `summarize_fn` найден, проверен на `None` и **ни разу не вызван**; обе ветви `return self._empty(...)`. То есть `compact()` с дефолтным `idle=False` **всегда** возвращает неуспешный отчёт. Докстринг `:108` при этом обещает «token-budget сжатие (`maybe_consolidate_by_tokens`)».
9. `lib/services/db_logging_service.py:1177-1205` — `log_sync_event()` 0 ссылок в коде; остался только в `openspec/specs/logging-db/spec.md` и `PENDING-DELETIONS.md`. Его докстринг `:1189` описывает «PG→DuckDB sync-путь», а `pg_duckdb_sync_service.py` удалён. **Удалить, 29 LOC.**
10. `lib/services/db_logging_bus.py:143` — вызов `getattr(service, "record_registration_failure", None)`. Из-за рефлексии AST-инвентарь показывает `db_logging_service.py:1207` как ref=0, а метод **жив**. Ложный ноль примера из брифа: рефлексия даёт ложные нули в обе стороны.
11. `lib/services/session_cold_sync_service.py:675-680` — `resolve_default_sqlite_path()` 0 ссылок; её же docstring говорит «Хранилище создаёт библиотека». Противоречие внутри одной функции. **Удалить, 6 LOC.**
12. `lib/services/schema_validation.py:244` — `names.append(("public", cur))`: схема **захардкожена**, хотя `config.json → channels.postgres.schema` настраивается и `channel_factory` её читает. При непубличной схеме валидатор отрапортует «таблицы нет» о существующих таблицах.
13. `lib/services/config_service.py:84-104` — AST даёт `get_int`/`get_str` ref=0, но они живые: `lib/core/application_context.py:278-280,312`. Ещё один ложный ноль (вызов через атрибут `ctx.config_service.get_int`).
14. `lib/services/enterprise_mcp_client.py:485-513` — `_child_env()` **чист**: наружу уходит только `PYTHONIOENCODING`, `ENTERPRISE_*` не экспортируются. Контракт из AGENTS.md соблюдён, страж `tests/test_enterprise_mcp_settings_contract.py` это фиксирует. Находок нет.
15. `lib/services/runtime_events_subscriber.py:167-174` — `unidentified_turn_events()` 0 вызовов, включая `streamlit_app.py`. **Удалить кандидат, 8 LOC** (см. таблицу — оставлен как диагностический публичный метод с оговоркой).

**Вердикты (по файлам, 19):** Оставить 13 · Упростить 6 (правка докстрингов и точечное
упрощение) · Удалить 0 (целиком не удаляется ни один файл) · Слить 0.
Считается по строке `**Вердикт.**` каждой файловой секции.

**Вердикты (по символам — методы и функции, 238 строк таблиц, проверено скриптом):**
Оставить 227 · Упростить 5 · Удалить 5 (114 LOC) · Слить 1.
Вердикты 47 классов вынесены в прозу над их таблицами методов.

**Разобрано символов: 284 из 284** (47 классов + 181 метод + 56 функций).
**НЕ РАЗОБРАНО: 0.**

---

## `lib/services/__init__.py` — 8 LOC

**Назначение.** Пакетный маркер `lib.services`.
**Что делает.** Ничего: исполняемого кода нет, только докстринг из 7 строк.
**Зачем нужен.** Существует, чтобы `lib.services` был пакетом; 7 тестов импортируют его ради
канонических списков (`tests/test_runtime_inventory.py` и др. тянут `lib.services.runtime_inventory`).
**Вердикт.** `Упростить`
**Обоснование.** Файл нужен как пакет, но его докстринг **описывает архитектуру, которой нет**:
«внешние зависимости агента (PostgreSQL, DuckDB-кеш, векторные индексы)» — локальный кэш
и векторные индексы уехали в `mcp-platform`; «Навыки и другой код общаются с ними через
интерфейсы, не зная деталей реализации» — слой интерфейсов (`CacheProvider`, `CacheStore`)
тоже удалён, навыки ходят к данным через `enterprise_mcp_client`.
**Доказательства.** `imported_by`: 7 файлов, все тесты; исполняемых символов — 0,
таблица методов пуста (нечего разбирать). Гард `tests/test_no_cache_api_remains.py:80-82`
исключает файлы, начинающиеся с `_`, поэтому этот докстринг **не проверяется ни одним стражем**.

---

## `lib/services/runtime_patcher.py` — 1427 LOC

**Назначение.** Каталог monkey-patch'ей приватных API библиотеки `nanobot 0.3.5`.
**Что делает.** `RuntimePatcher.apply_all()` вызывает шесть независимых патчей и возвращает
`PatchReport`; каждый патч — `(True, str)` или `(False, причина)`, отказ **не** роняет старт.
Побочные эффекты — все глобальные: правка атрибутов модулей `nanobot`
(`exec_session.MAX_OUTPUT_CHARS`, `shell.ExecTool._MAX_TIMEOUT`), подмена класса
`_SubagentHook`, подмена `_execute_tool_call`, перехват `AgentLoop._assemble_outbound`.
Отдельный патч (`assemble_outbound`) ставит в `OutboundMessage.metadata` ключ окна контекста,
который потом читает `RuntimeEventsSubscriber`. Никаких потоков и сетевых вызовов.
**Зачем нужен.** В upstream нет ни конфигурируемых лимитов вывода, ни способа **отклонить**
tool-вызов из хука, ни событий подагента. Удаление ломает лимиты, аудит tool-вызовов,
журналирование субагентов и режим `repeat_guard=block`.
**Вердикт.** `Оставить`
**Обоснование.** Канон AGENTS.md соблюдён точно; для всех шести патчей есть заполненное
«Условие удаления» (`docs/architecture/runtime-patcher-inventory.md:49-54`);
83 гвард-теста проходят. Оставшиеся проблемы — мёртвый код, неиспользуемые параметры и
одна ложная строка обоснования (см. ниже).
**Доказательства.** `imported_by`: 13 файлов. Продакшн-потребитель ровно один —
`lib/core/application_context.py` (stage composition root) + `runtime_inventory.py:198`
для канона. Тесты: `test_runtime_patcher.py`, `test_runtime_patcher_e2e.py`,
`test_patch_spec_consistency.py`, `test_subagent_logging.py`, `test_application_context_single_application_point.py`.
**Риск апгрейда (не дефект, а факт).** Все шесть патчей работают с приватными именами upstream
(`_MAX_TIMEOUT`, `_assemble_outbound`, `_SubagentHook`, `_execute_tool_call`, `exec_session.MAX_OUTPUT_CHARS`).
`test_patch_spec_consistency.py` проверяет наличие целей, но не семантику — при смене
внутреннего устройства upstream патч применится «успешно» и будет делать не то.
`runtime-patcher-inventory.md:49-54` это осознаёт (колонка Risk).

#### class `ContextWindowNotSeededError` (строки 96–108, 0 методов)
Наследник `RuntimeError`. Сигнал «lim контекста не засеян в первый оборот».
Зачем: `RuntimeEventsSubscriber` ловит его и seed'ит лимит из фактического размера запроса —
иначе метрика `context_window` первого оборота была бы выдуманной.
Вердикт `Оставить`. Потребители: `lib/services/runtime_events_subscriber.py` (перехватывает),
`tests/test_turn_identity.py`, `tests/test_runtime_events_subscriber.py`.

#### class `PatchSpec` (строки 209–246, 0 методов)
Dataclass-дескриптор патча: `name`, `purpose`, `nanobot_target`, `reason`,
`alternatives_checked`, `risk`, `nanobot_version`, `required`.
Зачем: единственный источник истины о критичности — `runtime_inventory.canonical_runtime_patches()`
(`runtime_inventory.py:186-208`) читает `required`/`risk`/`purpose` отсюда, а не дублирует.
Вердикт `Оставить`. Потребитель: `runtime_patcher.py:515-522`, `runtime_inventory.py:198-208`.

#### class `PatchReport` (строки 364–419, 3 метода)
Отчёт о применении патчей. `required` в нём — **только** metadata для баннера
(`runtime_inventory.py:194-196` явно это фиксирует), control flow от него не зависит.
Вердикт `Оставить`.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 378–382 | Пустой отчёт (applied/skipped/failed) | Стартовое состояние | `RuntimePatcher.apply_all:476` | Оставить |
| `to_dict` | 384–390 | Отчёт → dict для JSON-баннера | `tools/diagnose_startup.py --json` | `application_context.py` (баннер), `tools/diagnose_startup.py` | Оставить |
| `render` | 392–419 | Человекочитаемый блок `✓/⚠/✗` | DRIFT-баннер в старт-логе | `application_context.py`, `cli_agent.py` | Оставить |

#### class `RuntimePatcher` (строки 461–1421, 10 методов)
Единственный владелец всех шести патчей. Вердикт `Оставить` с оговорками по `apply_all`.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `apply_all` | 464–512 | Прогнать все 6 патчей, собрать отчёт | Единственная точка подключения | `lib/core/application_context.py:1050` | **Упростить** — `config` и `workspace_dir` не используются в теле (503–512) |
| `patch_specs` | 515–522 | Канон `_PATCH_SPECS` | Питает `runtime_inventory` | `runtime_inventory.py:207`, `tests/test_runtime_inventory.py` | Оставить |
| `_record` | 525–543 | Разложить `(ok, причина)` в applied/skipped/failed | Единая классификация отказа | `apply_all` (6×) | Оставить |
| `_bump_schema_max` | 546–574 | Поднять `maximum` в JSON-схеме параметра tool'а | Схема заморожена `deepcopy` в `nanobot/agent/tools/base.py:336` | `patch_exec_limits`, `patch_exec_timeout_cap` | Оставить |
| `patch_exec_limits` | 580–632 | Конфигурируемые лимиты вывода `exec` | В upstream константа в модуле | `apply_all:519` | Оставить |
| `patch_exec_timeout_cap` | 638–673 | Поднять потолок таймаута `exec` с 600 до `gateway.exec_timeout_cap_sec` | `TOOLS.md` учит агента передавать явный `timeout`, иначе снятие лимита не работает | `apply_all:520` | Оставить — **но переписать docstring**: обоснование ссылается на удалённый `legal_summarizer` (см. находку 4) |
| `patch_tool_limits` | 679–717 | Лимиты `read_file`/`grep`/`list_dir` | У двух из пяти целей — голые глобалы, подкласс не перехватывает | `apply_all:521` | Оставить |
| `patch_assemble_outbound` | 723–873 | Внедрить `tool_audit` + `recent_files` в финальный outbound | Единственная точка, где можно подменить значение доставки; **ставит seed окна контекста** | `apply_all:522`, `runtime_events_subscriber` (через seed) | Оставить — риск `high`, `required=True` |
| `patch_subagent_logging` | 879–1313 | Проксировать tool-события подагентов в журнал | `events` у субагента — `NO_EVENTS`, иначе событий ноль | `apply_all:523` | Оставить — риск `high`, `required=True` |
| `patch_repeat_guard_block` | 1315–1421 | Превратить отказ защитника в синтетический tool-результат | Hook-API не умеет возвращать «отказ»; с `reraise=True` обрывается весь батч | `apply_all:524` | Оставить |

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_getloaded` | 64–73 | `importlib.import_module` с пометкой в `sys.modules` | Безопасный ленивый импорт в середине патча | `patch_exec_limits:596`, `patch_tool_limits:695` | Оставить |
| `_session_key_of` | 76–85 | Достать `session_key` из объекта outbound | Атрибут или ключ, в зависимости от формы сообщения | `patch_assemble_outbound` | Оставить |
| `_attach_context_window` | 111–205 | Засеять лимит окна в `metadata` первого оборота | Метрика `context_window` обязана быть фактом, а не выдумкой | `patch_assemble_outbound` | Оставить |
| `_classify_skip` | 350–361 | Отличить «пропущен» от «упал» по тексту причины | `skipped` ≠ `failed` в баннере | `apply_all` | Оставить |
| `_resolve_agent_id` | 422–458 | Вычислить `agent_id` из `config` | **Потребитель (`patch_turn_delivery_fail`) удалён** | **0 ссылок в репо** | **Удалить** |

**Замечания по файлу.**
- `:10` — «Фаза 6 вынесла **пять** патчей» при шести перечисленных. Арифметика.
- `:422-458` — копия той же логики живёт в `lib/core/application_context.py:1269`; после удаления
  из патчера останется одна.
- `tests/test_runtime_patcher.py:607,621,716` читают `getattr(RuntimePatcher, "_PATCH_SPECS", {})`,
  а `_PATCH_SPECS` — **переменная уровня модуля** (`runtime_patcher.py:249`), не атрибут класса.
  `getattr` возвращает `{}`, и три гварда (`test_spec_removed_from_patch_specs`,
  `test_specs_absent_from_patch_specs`, `test_inventory_is_exact` в этой части) **проходят всегда**.
  Настоящий гвард есть — `tests/test_patch_spec_consistency.py` разбирает модуль через AST, —
  поэтому на безопасность кода это не влияет, но три теста в `test_runtime_patcher.py` —
  ложно-зелёные.

---

## `lib/services/runtime_inventory.py` — 394 LOC

**Назначение.** Single source of truth для стартового инвентаря: канонические списки хуков,
project-tools и runtime-патчей + функции diff.
**Что делает.** Ничего побочного — только чтение и сравнение. `canonical_runtime_patches()`
импортирует `RuntimePatcher` лениво (внутри функции, `runtime_patcher.py:198`), чтобы
инвентарь не тянул патчер при импорте.
**Зачем нужен.** Сравнивается в `ApplicationContext.start()` (prominent-баннеры missing/unexpected)
и в `tools/diagnose_startup.py`. Без него расхождение между ожиданием и фактом негде увидеть.
**Вердикт.** `Оставить`
**Обоснование.** Канон совпадает с фактической регистрацией — проверено вручную:
5 файлов в `workspace/tools/`, 5 записей; 3 файла в `workspace/hooks/`, 3 записи;
6 патчей, 6 записей. Имена tool'ов сверены со свойством `name` каждого класса
(`compact_context`, `history_search`, `legal_summarizer_query`, `audit_analyzer_query`,
`document_read`) — расхождений нет. Известный баг «`ExampleTool` против `example_tool`»
**уже исправлен** (`CHANGELOG.md:197`, guard в `tests/test_runtime_inventory.py`).
**Доказательства.** `imported_by`: 7 файлов + динамический импорт в `tools/diagnose_startup.py:202`.
Тесты: `test_runtime_inventory.py` (весь канон), `test_tools_project_loader.py`.

#### class `HookSpec` (строки 37–44, 0 методов)
`name`, `kind`, `required`, `description`, `source`. `source` — путь файла-источника;
читается `tests/test_runtime_inventory.py:49-50` для проверки существования файла.
Вердикт `Оставить` — поле `source` **живое**, это полезный guard.

#### class `ToolSpec` (строки 48–55, 0 методов)
`name`, `module`, `required`, `description`, `config_key`. Вердикт `Упростить` — **убрать `config_key`**.
Обоснование: единственный читатель — `tests/test_runtime_inventory.py:122-125`, который
проверяет лишь префикс `tools.`. Само значение на `:146` **неверно** (tool читает
`gateway.compact.*`, `workspace/tools/compact_context.py:24,101`), а на `:160` — `None`,
хотя tool объявляет `config_key = "legal_summarizer_query"`. Строка 145 в соседнем
`description` прямо говорит «читает `gateway.compact.*`», что противоречит `config_key`
на следующей строке. Поле не участвует ни в diff, ни в баннере, ни в `diagnose_startup`.

#### class `RuntimePatchSpec` (строки 59–65, 0 методов)
`name`, `required`, `risk`, `purpose` — проекция `PatchSpec` для diff-баннера.
Вердикт `Оставить`.

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `canonical_framework_hooks` | 68–95 | 4 хука `lib/hooks/` | Эталон для diff | `application_context.py`, `tools/diagnose_startup.py:202` | Оставить |
| `canonical_plugin_hooks` | 98–122 | 3 хука `workspace/hooks/` | Эталон для diff | там же | Оставить — **но**: описание `StreamDiagnosisHook` содержит «помечен REMOVED в `post-0.3.5-hook-migration`», при том что `workspace/hooks/debug_stream_diag.py` жив и подключён; описание `SessionFileRedirectHook` повторяет путь `data_store/cache/sessions/<key>/`, который достался от снятого кэш-кластера |
| `canonical_hook_factories` | 125–135 | per-turn фабрики (`DatabaseLoggingHook`) | Эталон для diff + счётчик | `diff_hooks:247` | Оставить |
| `canonical_project_tools` | 138–183 | 5 project-tools | Эталон для diff | `application_context.py`, `tools/diagnose_startup.py:212` | Оставить (см. `ToolSpec`) |
| `canonical_runtime_patches` | 186–208 | 6 патчей из `RuntimePatcher.patch_specs()` | Единственный источник `required` | `diff_runtime_patches`, баннеры | Оставить |
| `collect_actual_hook_names` | 211–219 | Собрать фактические имена хуков из `ctx.hooks` | Сторона diff «факт» | `application_context.py` (после `_log_connected_hooks`) | Оставить |
| `diff_hooks` | 222–261 | missing/unexpected + сверка числа фабрик | Баннер + `diagnose_startup` | `application_context.py`, `tools/diagnose_startup.py:209` | Оставить |
| `diff_project_tools` | 264–306 | missing/unexpected/disabled/failed | Баннер + `diagnose_startup` | `application_context.py`, `tools/diagnose_startup.py:212` | Оставить — докстринг ссылается на `patch_project_tools` (патч удалён) и на несуществующее поле `disabled_required` |
| `diff_runtime_patches` | 309–348 | missing/skipped/failed с criticality | Баннер + `diagnose_startup` | `application_context.py`, `tools/diagnose_startup.py:222` | Оставить |
| `parse_project_tools_detail` | 351–384 | Разобрать текстовый `detail` баннера regex'ом | **Продакшн-потребителя нет** | только `diff_project_tools_from_detail` + `tests/test_runtime_inventory.py:365,380,391` | **Удалить** |
| `diff_project_tools_from_detail` | 387–394 | Diff по текстовому `detail` | **0 ссылок**; `tests/test_tools_project_loader.py:503` фиксирует, что баннер раньше звал именно его | **Удалить** |

---

## `lib/services/runtime_health.py` — 211 LOC

**Назначение.** Компонентные проверки готовности рантайма и их сводный вердикт.
**Что делает.** Никаких побочных эффектов: `RuntimeReadiness.register()` кладёт
`(name, callable, required)`, `check()` вызывает их и сводит в `ReadinessReport`.
`RuntimeHealth` отдельно считает аптайм процесса. HTTP-эндпойнта нет, состояние
пересчитывается по запросу (это осознанное решение, зафиксированное в AGENTS.md).
**Зачем нужен.** `application_context.py:708-709` вызывает `check()` в старте и логирует
READY/DEGRADED/NOT_READY. Required-ness компонента выводится из конфига, а не из имени класса.
**Вердикт.** `Оставить` (докстринги — `Упростить`)
**Обоснование.** Механизм нужен и используется; проблема только в документации, которая
описывает **снятые** компоненты.
**Доказательства.** `imported_by`: `lib/core/application_context.py`,
`tests/test_runtime_health.py` (динамический импорт).

#### class `ComponentStatus` (строки 37–52, 0 методов)
`name`, `required`, `status`, `detail`. Вердикт `Оставить`.

#### class `ReadinessReport` (строки 56–78, 1 метод)
Вердикт `Оставить`.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `to_dict` | 66–78 | Отчёт → dict | Машиночитаемый вывод | `application_context.py:709` | Оставить |

#### class `RuntimeHealth` (строки 102–146, 6 методов)
Счётчик аптайма. Вердикт `Оставить`.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 110–112 | Запомнить `started_at` | База аптайма | `RuntimeHealth()` | Оставить |
| `mark_started` | 114–115 | Сбросить отметку старта | Старт рантайма | `application_context.py` | Оставить |
| `mark_stopped` | 117–118 | Отметить остановку | `status` начинает отдавать STOPPED | `application_context.py` | Оставить |
| `is_alive` | 120–125 | Жив ли процесс | Диагностика | `application_context.py`, `tests/test_runtime_health.py` | Оставить |
| `status` | 127–128 | Текущий статус | Публичный доступ | `application_context.py`, `ctx.runtime_health` | Оставить |
| `get_stats` | 130–146 | Аптайм и статус в dict | Health-вывод | `application_context.py` | Оставить |

#### class `RuntimeReadiness` (строки 149–206, 3 метода)
Реестр проверок. Вердикт `Оставить`.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 161–162 | Пустой реестр | Старт | `RuntimeReadiness()` | Оставить |
| `register` | 164–174 | Добавить проверку + флаг required | Единая точка регистрации компонентов | `application_context.py` (PG, enterprise-mcp, …) | Оставить |
| `check` | 176–206 | Выполнить всё, свести в `ReadinessReport` | Стартовый вердикт | `lib/core/application_context.py:709` | Оставить |

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `compute_overall_status` | 81–99 | NOT_READY / DEGRADED / READY по набору статусов | Правило деградации | `check:196` | Оставить |
| `_now` | 209–211 | `time.monotonic()` | Аптайм | `RuntimeHealth` | Оставить |

**Замечания по файлу.** `:8`, `:41`, `:168` перечисляют `DuckDB cache` и `vector search`
как компоненты/примеры. `application_context.py:1160-1162` прямо фиксирует, что эти проверки
сняты в фазе 5, а данные теперь у платформы. Докстринги **лгут**; код жив.

---

## `lib/services/runtime_events_subscriber.py` — 461 LOC

**Назначение.** Подписка на runtime-события `nanobot` с превращением их в turn-метрики журнала.
**Что делает.** `start()` подписывается на шину: `TurnCompleted`, `TurnRuntimeAdmitted`,
`SubagentTurnCompleted`. Ключевая связка — `_handle_turn_runtime_admitted`
(`:274-299`): если лимит окна не засеян, а агент **не** бросил `ContextWindowNotSeededError`,
подписчик **дозасеивает** его сам. Побочные эффекты: запись событий в `DbLoggingService`
(очередь), глобальный флаг `_SUBAGENT_SUBSCRIBER_REGISTERED`, `setdefault`-правка
`SubagentManager` для проброса шины подагенту. Потоков нет.
**Зачем нужен.** Без него seed окна контекста не засеивается → метрика `context_window`
недостоверна, а `subagent_logging`-патч (435 LOC) получает ноль событий.
**Вердикт.** `Оставить`
**Обоснование.** Критическая связка между `runtime_patcher` и журналом; `decision` зафиксирован
в `docs/architecture/decisions/runtime-events-subscriber.md`.
**Доказательства.** `imported_by`: `lib/core/application_context.py`,
`tests/test_runtime_events_subscriber.py`, `tests/test_turn_identity.py`.

#### class `_TurnIdentity` (строки 86–97, 0 методов)
`user_id`, `request_id` — снимок личности оборота. Вердикт `Оставить` (private, но это
единственный переносчик состояния между `_capture_identity` и `_take_identity`).

#### class `RuntimeEventsSubscriber` (строки 140–458, 9 методов)
Вердикт `Оставить`.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 149–165 | Запомнить шину и сервис журнала | DI | `application_context.py` | Оставить |
| `unidentified_turn_events` | 167–174 | Счётчик `agent.completed` без полной личности | Диагностика «журнал потерял личность» | **0 вызовов в репо** (включая `streamlit_app.py`) | **Упростить** — оставить как диагностический публичный метод или удалить 8 LOC; `duplicates.md:52` помечает его как «дубликат `GatewayRunner.reset_backoff`», что **ложное срабатывание** детектора (совпадение по форме, не по имени) |
| `_capture_identity` | 176–200 | Запомнить личность оборота, пока доступна | Непустой `sender_id` отбрасывается, подставляется заглушка | `_handle_turn_completed` | Оставить |
| `_take_identity` | 202–207 | Забрать и снять личность | Одноразовость | `_handle_turn_completed` | Оставить |
| `start` | 209–248 | Подписка на 3 события + проброс шины субагенту | Точка подключения | `application_context.py.start()` | Оставить |
| `stop` | 250–272 | Отписка | Shutdown | `ctx.stop()` | Оставить |
| `_handle_turn_runtime_admitted` | 274–299 | Метрика окна + дозасев лимита | **Критическая страховка** seed'а | шина (async) | Оставить |
| `_handle_turn_completed` | 301–408 | Полный набор turn-метрик | Главный источник телеметрии оборота | шина (async) | Оставить |
| `_handle_subagent_turn_completed` | 410–458 | Метрики оборота подагента | Иначе субагенты невидимы | шина (async) | Оставить |

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_current_sender_id` | 61–82 | `sender_id` текущего request | Источник личности оборота | `_capture_identity:180` | Оставить |
| `_set_subagent_default_bus` | 100–118 | `setdefault` шины в `SubagentManager` | Субагент должен увидеть ту же шину | `__init__:158` | Оставить — скрытый контракт: порядок инициализации не enforced |
| `_set_subagent_subscriber_registered` | 121–137 | Флаг «подписчик уже подключён» | Идемпотентность при двойном `start` | `start` | Оставить |

---

## `lib/services/context_compaction.py` — 603 LOC

**Назначение.** Единая точка записи **факта** сжатия контекста.
**Что делает.** Четыре входа (slash `/compact` → событие → subscriber → `notify_session_compacted`;
CLI `/compact`; tool `compact_context`; авто-сжатие). Одна функция записи — `_notify`
(`:315-330`), которая делает ровно две вещи: пишет human-readable заметку в
`agent_conversation_messages` (видно в UI, **но не в контексте промпта** — то есть агент
о своём сжатии не узнаёт) и событие `context_compacted` в долговечный `agent_gateway_logs`
(тут агент узнаёт, потому что читает журнал через `history_search`).
**Зачем нужен.** Единственная точка, где факт сжатия попадает в наблюдаемость;
`docs/ARCHITECTURE.md` § «Управление сжатием контекста» описывает её как контракт.
**Вердикт.** `Оставить` (метод `compact` — `Упростить`)
**Обоснование.** Связка живая и покрыта тестами; проблема — мёртвая ветка внутри `compact`
и докстринг, обещающий несуществующий режим.
**Доказательства.** `imported_by`: 8 файлов — `lib/cli/console_loop.py`,
`lib/core/application_context.py`, `workspace/tools/compact_context.py` + 5 тестов
(`test_context_compaction.py`, `test_compaction_observation_wiring.py`,
`test_compaction_event_subscriber.py`, `test_context_compaction_user_id.py`,
`test_unified_event_logging_contract.py`).

#### class `ContextCompactionService` (строки 61–580, 15 методов)
Вердикт `Оставить`.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 64–80 | Принять agent, settings, журнал, MCP-клиент | DI | `application_context.py` | Оставить |
| `enabled` | 83–84 | `gateway.compact.enabled` | Ранний выход | `compact:120`, `workspace/tools/compact_context.py:167` | Оставить |
| `notify_in_history` | 87–88 | Показывать ли заметку в истории | Настройка | `_notify` | Оставить |
| `print_to_terminal` | 91–92 | Печатать ли в терминал | Настройка | CLI | Оставить |
| `compact` | 94–203 | Сжать и вернуть отчёт | Ручной путь (`/compact`, tool) | `workspace/tools/compact_context.py:170`, `console_loop.py` | **Упростить** — см. ниже |
| `format_report` | 206–240 | Отчёт → текст для пользователя | CLI/tool-вывод | `console_loop.py`, `workspace/tools/compact_context.py` | Оставить |
| `_estimate` | 242–259 | Оценка токенов prompt'а + fallback | Метрики до/после | `compact:140` | Оставить |
| `_estimate_fallback` | 262–291 | Оценка по символам, если upstream упал | Надёжность | `_estimate:259` | Оставить |
| `_current_session_key` | 294–299 | Ключ сессии из request context | `session_key=None` | `compact:123` | Оставить |
| `_empty` | 301–313 | Отчёт-неуспех | Единый формат отказа | `compact` (5×) | Оставить |
| `_notify` | 315–330 | **Единственная** точка записи факта | Инвариант «один путь» | `compact`, `record_external_compaction`, `notify_session_compacted` | Оставить — ядро модуля |
| `_record_event_log` | 332–394 | Событие `context_compacted` в журнал | Агент узнаёт о сжатии через `history_search` | `_notify` | Оставить |
| `record_external_compaction` | 396–440 | Записать сжатие, сделанное вне сервиса | `/compact` минует `ContextCompactionService.compact` | `compaction_event_subscriber.feed` | Оставить |
| `notify_session_compacted` | 442–521 | Публичный API подписчика | Единственная точка входа наблюдаемости | `lib/services/compaction_event_subscriber.py` | Оставить |
| `_write_history_notice` | 523–580 | Заметка в `agent_conversation_messages` | Видно в UI, но **не** агенту | `_notify` | Оставить |

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_get_setting` | 45–58 | Чтение `gateway.compact.*` с резолвом `${VAR}` | Настройки | `__init__`, `notify_in_history` | Оставить |
| `_current_request_sender_id` | 583–603 | `sender_id` текущего request | Подпись события | `_record_event_log:381` | Оставить — **но см. кросс-дублирование ниже** |

**Находка по методу `compact` (`:94-203`).**
Строка 142: `use_idle = bool(idle or force)`. Единственный продакшн-вызов —
`workspace/tools/compact_context.py:170-174`, передаёт `idle=bool(idle)`, `force=bool(force)`.
Дефолт агента — вызвать tool без аргументов, то есть `use_idle == False`, и тогда
исполняется `else` на строках 153-165:
- `summarize_fn = getattr(consolidator, "summarize_provider_compaction", None)` — **присвоено**;
- `if summarize_fn is None: return self._empty(...)` — единственное «использование», проверка на `None`;
- `return self._empty("token-budget компакция ... не поддержана в nanobot 0.3.5")`.

Итог: `summarize_fn` **не вызывается ни разу**, обе ветви возвращают один и тот же отчёт-неуспех.
Докстринг `:108` при этом обещает «`False` — token-budget сжатие (`maybe_consolidate_by_tokens`)» —
то есть описывает режим, которого в коде нет.
**Вердикт `Упростить`:** убрать `summarize_fn`-lookup целиком (7 строк) и оставить один
`return self._empty(...)`; в докстринге заменить обещание на честное «token-budget режим
не поддерживается, используйте `force=True`». Поведение потребителя не изменится —
сегодня оно точно такое же.
**Кросс-подсистемный дубль:** `_current_request_sender_id` существует в двух копиях —
здесь (`context_compaction.py:583-603`) и в `lib/hooks/database_logging_hook.py:252`.
Это подсистема хуков; в рамках моей группы отмечено как находка для владельца хуков.

---

## `lib/services/compaction_event_subscriber.py` — 105 LOC

**Назначение.** Мост «канал увидел `ContextCompactionEvent` → сервис записал факт».
**Что делает.** Ничего побочного: `feed(outbound)` фильтрует `ContextCompactionEvent`
по типу и фазе, достаёт `session_key`/`compaction_id`, вызывает
`ContextCompactionService.notify_session_compacted()` и ставит на outbound
`FINAL_TURN_KEY="_final_turn"` — без него постгресс-канал не финализирует оборот
и задача **зависает в `processing`**. Если сервис не привязан — событие уходит в лог
предупреждения, чтобы потеря была видна.
**Зачем нужен.** Инвариант `FINAL_TURN_KEY` — скрытый контракт между каналом и
финализацией оборота; `AGENTS.md:48` фиксирует его явно.
**Вердикт.** `Оставить` (докстринги — `Упростить`)
**Доказательства.** `imported_by`: `lib/core/application_context.py`,
`tests/test_compaction_event_subscriber.py`. Подключение подтверждено
(`application_context.py`, `git log e7ce776` — «подписчик наконец подключён»).

#### class `CompactionEventSubscriber` (строки 33–105, 3 метода)

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 47–52 | Хранить (возможно `None`) ссылку на сервис | Сервис создаётся позже | `application_context.py` | Оставить |
| `set_service` | 54–61 | Привязать сервис после создания | Порядок: подписчик раньше сервиса | `application_context.py` | Оставить — докстринг говорит «когда `RuntimePatcher.apply_all` создаёт сервис позже», но `apply_all` **никакого** compaction-сервиса не создаёт (6 патчей такого нет) — ссылка устарела |
| `feed` | 63–105 | Фильтр события + вызов сервиса + `FINAL_TURN_KEY` | **Не даёт задаче зависнуть** | `lib/channels/postgres_channel.py` | Оставить |

**Замечания по файлу.** `:22-24` утверждает, что файл отделён от `ContextCompactionService`,
чтобы тот «не зависел от asyncio-инфраструктуры шины (`MessageBus.consume_outbound`)» —
но `MessageBus.consume_outbound` в реализации **не используется вовсе**, канал зовёт `feed()`
напрямую. `:36` перечисляет «Канал (postgres/**redis**)» — `redis_channel.py` удалён
(`AGENTS.md:46`). Обе строки лгут; код жив.

---

## `lib/services/project_tool_loader.py` — 331 LOC

**Назначение.** Единственный публичный loader кастомных tool'ов из `workspace/tools/*.py`.
**Что делает.** `_discover()` сканирует каталог, импортирует модуль по имени файла,
находит наследника `Tool` (`_is_tool_class`), собирает `ToolContext` (с DI: settings,
`db_logging_service`, `enterprise_mcp`), создаёт инстанс, проверяет дубликаты по `name` и
`module`, регистрирует. Формат `detail` намеренно остаётся разбираемым
`runtime_inventory.parse_project_tools_detail` (соглашение, зафиксированное в `:26,61,223,324`).
Побочные эффекты: `agent.tools` пополняется; исключения **не** пробрасываются — всё
конвертируется в `failed`/`detail`.
**Зачем нужен.** Штатный путь регистрации project-tools; `RuntimePatcher` **специально не**
занимается этим (запрещено политикой инвентаря, п. 5).
**Вердикт.** `Оставить`
**Обоснование.** Модуль небольшой, границы соблюдены, 331 LOC без единого мёртвого символа.
**Доказательства.** `imported_by`: `lib/core/application_context.py` (единственный
продакшн-вызов) + 4 теста; динамическая регистрация проверяется
`tests/test_runtime_patcher_no_project_tools_boundary.py`.

#### class `ProjectToolsLoadResult` (строки 39–78, 0 методов)
`registered`, `disabled`, `duplicate`, `failed`, `detail`, `error` — результат регистрации,
разбираемый баннером и `diagnose_startup`. Вердикт `Оставить`.

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_discover` | 81–141 | Скан каталога + поиск класса tool'а | Сканирование = точка, где статика даёт ложные нули | `register_project_tools:213` | Оставить |
| `_build_tool_context` | 144–193 | Собрать `ToolContext` с DI | Точка передачи зависимостей tool'ам | `register_project_tools:238` | Оставить |
| `register_project_tools` | 196–331 | **Публичный контракт**, регистрация | Единственный путь | `lib/core/application_context.py` | Оставить |

---

## `lib/services/consolidator_locale.py` — 64 LOC

**Назначение.** Локализация одного системного шаблона `nanobot`.
**Что делает.** `apply_template_overrides()` monkeypatch'ит **глобальное** поле
`prompt_templates._environment._loader` на `ChoiceLoader([FileSystemLoader(overrides), <было>])`.
Запуск из `ApplicationContext.start()`; повторный вызов идемпотентен (проверка
прежнего `ChoiceLoader`). Побочные эффекты: **глобальное мутабельное состояние Jinja2
внутри процесса** — любой, кто импортирует `prompt_templates` до или после, получает
изменённый загрузчик.
**Зачем нужен.** Единственный механизм, которым проект может переопределить шаблон
фреймворка, не форкая библиотеку. Сейчас переопределён один файл:
`workspace/overrides/agent/consolidator_archive.md` (проверено на диске).
**Вердикт.** `Оставить`
**Обоснование.** 64 LOC, один use case, покрыт `tests/test_consolidator_locale.py`.
**Доказательства.** `imported_by`: `lib/core/application_context.py`,
`tests/test_consolidator_locale.py`.

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_overrides_dir` | 30–33 | `workspace/overrides` по умолчанию | Путь к каталогу переопределений | `apply_template_overrides` | Оставить |
| `apply_template_overrides` | 36–64 | Подменить Jinja2-loader, вернуть признак «применено» | Локализация | `application_context.start()` | Оставить |

---

## `lib/services/schema_validation.py` — 345 LOC

**Назначение.** Pre-startup проверка наличия 5 runtime-таблиц в БД.
**Что делает.** Один SELECT к `information_schema.tables` с `timeout` и списком
ожидаемых пар `(schema, table)`. Отсутствие таблиц → `SchemaValidationError`
(наследник `ConfigurationError`), так что конфиг не может уехать «наполовину».
Побочные эффекты: только сетевой вызов (один) и поднятие исключения.
**Зачем нужен.** Без него gateway стартует и падает на первом же запросе к отсутствующей
таблице, а причина неочевидна.
**Вердикт.** `Оставить` (метод `expected_table_names` — `Упростить`)
**Обоснование.** Механизм нужен; конкретная находка — захардкоженная схема.
**Доказательства.** `imported_by`: 6 файлов, включая
`tests/test_startup_schema_validation_live.py` (живой тест против реальной БД) и
`tests/test_gateway_entrypoint_schema_validation.py`. Спека:
`openspec/specs/runtime/startup-schema-validation`.

#### class `MissingTable` (строки 89–103, 1 метод)
`schema`, `name`, `full_name()`. Вердикт `Оставить`.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `full_name` | 101–103 | `schema.name` для сообщения | Читаемость ошибки | `SchemaValidationError._build_message` | Оставить |

#### class `SchemaValidationError` (строки 106–127, 2 метода)
Вердикт `Оставить`.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 113–116 | Сохранить список отсутствующих таблиц | Данные для сообщения | `validate` | Оставить |
| `_build_message` | 118–127 | Текст ошибки | Диагностика | `__init__` | Оставить |

#### class `_MissingConfigKeys` (строки 130–157, 2 метода)
Наследник `SchemaValidationError` для другого отказа: в конфиге нет самих ключей.
Вердикт `Оставить`.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 139–143 | Сохранить пути ключей | Сообщение | `expected_table_names:247` | Оставить |
| `_build_config_message` | 145–157 | Текст с подсказкой по профилю | Подсказка оператору | `__init__` | Оставить |

#### class `SchemaValidationTimeoutError` (строки 160–181, 1 метод)
Вердикт `Оставить`.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 173–181 | Сохранить таймаут и исходное исключение | Диагностика «не БД виновата, а сеть» | `check_tables` | Оставить |

#### class `SchemaValidationService` (строки 203–336, 3 метода)
`DEFAULT_TIMEOUT_SEC = DEFAULT_TIMEOUT_SEC` (классовый атрибут ссылается на модульную
константу) — приемлемо, но избыточно. Вердикт `Оставить`.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `expected_table_names` | 220–248 | 5 ключей из merged SETTINGS → `(schema, table)` | «Имена не зашиты в коде» — инвариант | `validate:314` | **Упростить** — `:244` жёстко кладёт `"public"`, игнорируя настраиваемый `channels.postgres.schema` (`config.json`, читается в `channel_factory`) |
| `check_tables` | 251–301 | Один SELECT, фильтрация отсутствующих | Безопасный фолбэк при таймауте | `validate:322` | Оставить |
| `validate` | 304–336 | Связка: собрать ожидаемые → проверить → бросить | Единая точка входа | `application_context.py:start()` | Оставить |

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_hint_for_profile` | 42–62 | Подсказка по имени профиля | Диагностика | `_build_config_message` | Оставить — **но** попала в `__all__`, хотя приватная; в `__all__` при этом **нет** `_MissingConfigKeys` |
| `_unwrap_settings` | 65–85 | Снять `_LazySettings`-прокси до dict | Работа с merged SETTINGS | `expected_table_names:230` | Оставить |
| `_timeout_exceptions` | 184–200 | Кортеж исключений таймаута драйвера | Классификация | `check_tables` | Оставить |

---

## `lib/services/channel_factory.py` — 151 LOC

**Назначение.** Сборка каналов из конфига.
**Что делает.** `create_all()` проходит по секции `channels` конфига и для каждого
известного канала вызывает `_add_postgres`, затем склеивает список созданных.
`_add_postgres` создаёт `PostgresChannel` с `dsn`, `schema`, лимитами пула из
`channels.postgres.pool.max_conn`, `db_logging_service` и `compaction_event_subscriber`.
Побочные эффекты: создание канала (пул соединений), передача ссылки на подписчик
(точка связывания с `context_compaction`).
**Зачем нужен.** Сейчас канал **один** — PostgreSQL; фабрика даёт единое место, где
известно, как поднимать канал из конфига, без размазывания по `gateway.py`.
**Вердикт.** `Оставить`
**Обоснование.** Единственный владелец создания канала; после удаления `redis_channel`
второй ветки нет, но место для неё остаётся осознанной точкой расширения.
**Доказательства.** `imported_by`: `gateway.py` (единственный продакшн-вызов) + 5 тестов.
**Замечание (не дефект).** При единственном канале `create_all` с его
`if channel_type != "postgres"`-проверками выглядит как будущая точка роста; это осознанно
(`AGENTS.md:46` объясняет, почему второй канал убран), но если новых каналов не планируется —
фабрику можно свернуть в `gateway.py`. Оставляю как `Оставить` по существу, помечаю как
кандидат на `Упростить` при изменении планов.

#### class `ChannelFactory` (строки 27–151, 3 метода)

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 39–57 | Принять признаки печати, журнал, MCP, подписчик | DI | `gateway.py` | Оставить |
| `create_all` | 59–93 | Создать все объявленные каналы | Единая точка | `gateway.py` | Оставить |
| `_add_postgres` | 99–151 | Собрать `PostgresChannel` | Реализация | `create_all` | Оставить |

---

## `lib/services/db_logging_service.py` — 2045 LOC

**Назначение.** Единственный владелец долговечного журнала `agent_gateway_logs` / `agent_question_runs`.
**Что делает.** Асинхронная очередь (`queue.Queue`, `maxsize=10000`) + **фоновый поток-воркер**,
который батчит события (по `batch_size` / `flush_interval_sec`), применяет порог уровня,
подавляет пробные события, штемпелит момент времени из **часов** (не из локального времени),
и пишет одним из двух путей: напрямую в PostgreSQL или батчем через операцию платформы
`log_events` (`:1684-1725`) с резервным локальным сдвигом (`:1640-1682`).
Побочные эффекты: фоновый поток, DDL-проверка (без провижина, `:1548-1583`),
периодический purge по `retention_days` плюс безусловная чистка пустых outbound-событий,
`_bump_schema_max`-подобные операции не применяются. `report_stats()` может **закрыть**
сервис (публичный API).
**Зачем нужен.** Это сердце наблюдаемости: journal-уровень, имена событий, привязка к
`session/user/request` — всё канонизировано здесь и защищено гвардами
(`test_journal_level_canonical.py`, `test_journal_event_name_alignment.py`).
**Вердикт.** `Оставить`
**Обоснование.** Самый импортируемый модуль проекта (27 файлов). Мёртвый символ ровно один
(`log_sync_event`), см. таблицу. Отдельно отмечен ложный ноль `record_registration_failure`.
**Доказательства.** 9 продакшн-импортёров (`lib/channels/postgres_channel.py`,
`lib/core/application_context.py`, `lib/hooks/database_logging_hook.py`,
`lib/hooks/repeat_guard_hook.py`, `lib/services/context_compaction.py`,
`lib/services/runtime_events_subscriber.py`, `lib/services/runtime_patcher.py`,
`lib/services/session_cold_sync_service.py`, `lib/services/turn_delivery_factory.py`) + 18 тестов.
**Наблюдение о размере.** 2045 LOC в одном модуле — крупнейший в группе; `DbLoggingService`
объединяет очередь, воркер, транспорт, question-runs, purge и статистику. Кандидат на
разделение на `DbLoggingQueue` + `DbLoggingWriter` + `DbLoggingPurger`, но это **рефакторинг,
не удаление**: сначала нужны узкие гварды, которые уже есть (`test_log_transport.py`,
`test_log_transport_wiring.py`).

#### class `JournalLevelError` (строки 94–101, 0 методов)
`ValueError` на недопустимом уровне. Вердикт `Оставить` — падение громче молчаливой
потери события; `test_journal_level_canonical.py` держит канон.

#### class `TurnOrder` (строки 384–395, 0 методов)
`ordered`, `unattributed`. Вердикт `Оставить` как **публичный контракт для читателей журнала**:
`docs/ARCHITECTURE.md:340-342` описывает его семантику (строки без `seq` считаются
отдельным счётчиком). Продакшн-вызовов в репозитории **0** — читатель живёт вне агента
(UI/аналитика). Не удалять.

#### class `LogEvent` (строки 460–488, 0 методов)
Событие журнала: `event_type`, `level`, `session_id`, `channel`, `actor`, `summary`,
`payload`, `metadata`, `request_id`, `user_id`, `name`, `id`, `queued_at`. Вердикт `Оставить`.

#### class `_FlushSentinel` (строки 492–493, 0 методов)
Маркер «очередь пуста, пора стоп». Вердикт `Оставить` — различает пустую очередь от
закрытой в `_worker`.

#### class `_QuestionRunRecord` (строки 497–514, 0 методов)
Запись прогона вопроса; 14 полей включая `parent_request_id`/`is_subagent`.
Вердикт `Оставить`.

#### class `DbLoggingService` (строки 517–2029, 41 метод)
Вердикт `Оставить`.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 520–682 | Собрать очередь, счётчики, уровни, retention | DI + состояние | `application_context.py`, 6 тестов | Оставить |
| `attach_transport` | 688–716 | **Вторая** фаза подключения транспорта | Транспорт требует живой MCP-сессии, которой на старте ещё нет | `application_context.py` | Оставить — скрытый контракт: порядок `create` → `attach_transport` → `start` нигде не enforced |
| `start` | 718–729 | Запустить воркер | Lifecycle | `ctx.start()` | Оставить |
| `stop` | 731–752 | Остановить воркер, дождаться flush'а | Graceful shutdown | `ctx.stop()` | Оставить |
| `is_running` | 754–755 | Жив ли воркер | Диагностика | `application_context.py`, тесты | Оставить |
| `_stamp_event_time` | 761–793 | Проставить момент и `seq` из часов | Порядок событий не зависит от TZ | `_enqueue` | Оставить |
| `log_event` | 795–824 | Точка входа для события | Публичный API | `try_log_event`, `log_*` | Оставить |
| `register_request` | 839–899 | Открыть прогон вопроса, записать личность | Идентификация оборота | `db_logging_bus.py:110`, `postgres_channel.py` | Оставить |
| `get_request_id` | 901–910 | Текущий `request_id` сессии | Подпись событий | `log_inbound`/`log_outbound`, `log_transport` | Оставить |
| `clear_request` | 912–917 | Снять личность | Конец оборота | `db_logging_bus.py` | Оставить |
| `finish_request` | 919–952 | Закрыть прогон вопроса | `agent_question_runs` | `db_logging_bus.py`, `postgres_channel.py` | Оставить |
| `_take_turn_identity` | 954–972 | Забрать снимок личности оборота | Одноразовость | `_resolve_event_user_id` | Оставить |
| `_identity_of_request` | 974–989 | Личность по `request_id` | Восстановление | `log_*` | Оставить |
| `log_inbound` | 991–1030 | Входящее сообщение | Журнал входящих | `db_logging_bus.make_inbound_logger` | Оставить — `_default_actor` на `:1015` |
| `log_outbound` | 1032–1081 | Исходящее сообщение | Журнал исходящих | `db_logging_bus.make_outbound_logger` | Оставить |
| `log_tool_call` | 1083–1102 | Вызов tool'а | Аудит tool'ов | `subagent_logging`-патч, `database_logging_hook` | Оставить |
| `log_tool_result` | 1104–1135 | Результат tool'а | Аудит tool'ов | там же | Оставить |
| `log_llm_call` | 1137–1175 | Промпт/ответ модели | Трассировка LLM | `database_logging_hook` | Оставить — `_json_safe` на `:1168-1169` |
| `log_sync_event` | 1177–1205 | Событие из PG→DuckDB sync-пути | **Потребитель `pg_duckdb_sync_service.py` удалён; вызовов в коде 0** | **0** (только `openspec/specs/logging-db/spec.md` и `PENDING-DELETIONS.md`) | **Удалить** |
| `record_registration_failure` | 1207–1218 | Отметить потерю регистрации | Диагностика скрытых потерь | `lib/services/db_logging_bus.py:143` через `getattr` — **жив**, AST даёт ложный ref=0 | Оставить |
| `get_stats` | 1220–1228 | Счётчики в dict | Health/диагностика | `report_stats`, `application_context.py` | Оставить |
| `report_stats` | 1230–1284 | Расширенный отчёт | Диагностика | `application_context.py` | Оставить |
| `_compute_oldest_queued_age_sec` | 1286–1308 | Возраст самого старого в очереди | Метрика давления | `get_stats`/`report_stats` | Оставить |
| `_should_log` | 1314–1332 | Порог уровня | Дешёвый ранний выход | `_enqueue` | Оставить — `journal_level_rank` на `:1335` |
| `_suppress_probe` | 1334–1369 | Отбросить пробные события | Условие **продублировано** из платформы | `_enqueue` | Оставить — риск разъезда с `mcp-platform/libs/enterprise_common/eventing/types` |
| `_reject_level` | 1371–1396 | Отклонить недопустимый уровень | Fail loud | `normalize_journal_level` | Оставить |
| `_enqueue` | 1398–1437 | Положить в очередь | Асинхронность + backpressure | `log_event` | Оставить |
| `_resolve_event_user_id` | 1439–1461 | Дозаполнить `user_id` | Полнота личности | `_enqueue` | Оставить |
| `_worker` | 1463–1534 | Цикл: дренаж → батч → flush → purge | Сердце сервиса, фоновый поток | `start` | Оставить |
| `_db_run` | 1540–1546 | Взять соединение пула | Единый способ | `_ensure_schema`, `_flush_batch`, `_handle_question_run`, purge-методы | **Слить с `workspace/utils/db.py`** — копия общего 7-строчного `try/return run(fn)/except` (см. `duplicates.md`) |
| `_ensure_schema` | 1548–1583 | Проверить существование обеих таблиц | Fail early; DDL **не** провижинит | `_flush_batch`, `_handle_question_run` | Оставить |
| `_flush_batch` | 1585–1638 | Записать батч (PG или MCP) | Основной путь записи | `_worker` | Оставить — `_count_by_type` на `:1632` |
| `_defer_batch` | 1640–1662 | Вернуть батч в очередь | Не терять при недоступной БД | `_flush_batch` | Оставить — `_count_by_type` на `:1661` |
| `_write_fallback` | 1664–1682 | Записать в локальный файл | Ничего не терять молча | `_flush_batch`, `_defer_batch` | Оставить |
| `_flush_batch_via_mcp` | 1684–1725 | Батч через операцию `log_events` платформы | Запись без второго владельца пула PG | `_flush_batch` | Оставить — `_count_by_type` на `:1708,1721` |
| `_handle_question_run_via_mcp` | 1727–1764 | `question_runs` через платформу | То же | `_handle_question_run` | Оставить |
| `_insert_batch` | 1766–1800 | `executemany` в PG | Прямой путь | `_flush_batch` | Оставить |
| `_handle_question_run` | 1802–1848 | Запись прогона вопроса | `agent_question_runs` | `_worker` | Оставить |
| `_upsert_question_run` | 1850–1921 | Upsert по `request_id` | Идемпотентность | `_handle_question_run` | Оставить |
| `_drop_batch` | 1923–1931 | Счётчик `failed` + WARNING, без файла | Не терять молча | `_flush_batch` | Оставить |
| `purge_empty_outbound` | 1937–1975 | Удалить пустые `outbound_*` | Чистка stream-мусора **независимо** от `retention_days` | `_purge_old:2027` | Оставить — поведение зафиксировано в `AGENTS.md:95` |
| `purge_old` | 1977–2019 | `DELETE` по `retention_days` | Ограничение роста БД | `_purge_old:2027` | Оставить |
| `_purge_old` | 2021–2029 | Шаг очистки из воркер-цикла | Развязывает cadence и retention | `_worker` | Оставить |

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `normalize_journal_level` | 104–122 | Привести уровень к `CHECK valid_level` | Канон уровней | `_reject_level`, тесты | Оставить |
| `journal_level_rank` | 125–127 | Числовой вес уровня | Сравнение порогов | `_should_log:1335` | Оставить — **AST даёт ref=1 (ложно «мёртвая»)**, живая |
| `is_probe_event_type` | 130–142 | Пробное ли имя события | Подавление шума | `_suppress_probe` | Оставить — **продублировано** из платформы; гвард `test_journal_probe_suppression.py` |
| `try_log_event` | 145–212 | Defensive-хелпер для producer'ов | Не бросать из sync-путей | `context_compaction.py:381`, `session_cold_sync_service.py` (через `try/except`) | Оставить |
| `next_event_seq` | 302–324 | Ключ порядка из системных часов | Монотонный `seq` | `_stamp_event_time:786` | Оставить — **AST даёт ложный «мёртвый»** |
| `_iso_utc` | 327–331 | ISO-8601 UTC из epoch | Читаемая форма `seq` | `event_time_columns` | Оставить |
| `_event_seq` | 334–353 | Разобрать `seq` из значения | Чтение | `event_time_columns` | Оставить |
| `event_time_columns` | 356–380 | `(seq, iso)` для колонок | Момент хранится **один раз**, в `metadata` | `_stamp_event_time` | Оставить — гвард `tests/test_journal_event_time_columns.py` |
| `order_turn_rows` | 398–420 | Разложить строки на упорядоченные/неатрибутированные | Читатель журнала (вне агента) | **0 вызовов в репо**; задокументирован `docs/ARCHITECTURE.md:341` | Оставить как публичный контракт |
| `_default_actor` | 423–432 | Метка инициатора, когда он не назван | У очереди producer, а не человек | `log_inbound:1015` | Оставить — **AST ложно даёт ref=1** |
| `_json_safe` | 435–456 | Рекурсивно привести к JSON-серизуемому | Промпт/ответ содержат dataclass'ы | `log_llm_call:1168-1169`, рекурсия `:447,449` | Оставить — **AST ложно даёт ref=1** |
| `_count_by_type` | 2032–2045 | Распределение батча по `event_type` | Метрика `written_by_type` | `_flush_batch:1632`, `_defer_batch:1661`, `_flush_batch_via_mcp:1708,1721` | Оставить |

---

## `lib/services/db_logging_bus.py` — 213 LOC

**Назначение.** Адаптеры «событие шины → вызов журнала».
**Что делает.** `make_inbound_logger` / `make_outbound_logger` возвращают `async`-функции,
которые `application_context.py:357-358` ставит в шину через `_wrap_bus_publish`.
Регистрация/закрытие прогона вопроса идёт **до** записи события, а исключение при
регистрации не роняет публикацию — вместо этого `_note_registration_failure` зовёт
`getattr(service, "record_registration_failure", None)`.
**Зачем нужен.** Развязывает шину и журнал: шина не знает про журнал, журнал не знает про шину.
**Вердикт.** `Оставить`
**Обоснование.** Слой тонкий и нужный; мёртвых символов нет.
**Доказательства.** `imported_by`: `lib/core/application_context.py:357-358` + 4 теста.

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `make_inbound_logger` | 54–134 | Адаптер входящих: регистрация запроса + `log_inbound` | Точка подписки | `application_context.py:357` | Оставить — `_note_registration_failure` на `:115` |
| `_note_registration_failure` | 137–148 | Отметить потерю регистрации | Не терять молча | `make_inbound_logger:115` | Оставить — **рефлексия `getattr` на `:143` даёт AST-инвентарю ложный ноль на `db_logging_service.py:1207`** |
| `make_outbound_logger` | 151–213 | Адаптер исходящих + фильтр шума (`stream-delta`/`stream-end`/`progress`) | Чистота журнала | `application_context.py:358` | Оставить |

**Замечание по файлу.** `:62` перечисляет «`channel` — telegram / cli / postgres / redis / ...».
Каналов в проекте один — PostgreSQL; `redis_channel.py` удалён (`AGENTS.md:46`).
Строка-пример устарела, код жив.

---

## `lib/services/log_transport.py` — 493 LOC

**Назначение.** Транспорт записи журнала: платформа вместо прямого PG.
**Что делает.** `McpLogWriter` режет батч по `group_by_identity()` на
`(session_id, user_id, request_id)` — **потому что операция `log_events` берёт идентичность
из контекста вызова, а не из тела батча** — и вызывает операцию платформы, разбирая её
текстовый ответ в счётчики `accepted`/`dropped` (`_counters`). События без полной
подписи уходят в локальный fallback и в счётчик `dropped`. `LocalFallbackSink` пишет
JSONL на диск с потолком `max_bytes` (32 МБ по умолчанию) и ротацией.
`LoopCallRunner` выполняет корутину на loop вызывающего (нужен, потому что лог-воркер —
синхронный поток, а MCP-сессия привязана к loop).
Побочные эффекты: сетевой вызов к платформе, запись файла fallback, чтение
`transport_pending` флага.
**Зачем нужен.** Единственный способ писать журнал, не создавая второго владельца пула PG
в процессе агента. Канон описан в AGENTS.md (фаза 7).
**Вердикт.** `Оставить`
**Обоснование.** Модуль небольшой, границы чистые, гвард-тесты парные
(`test_log_transport.py` + `test_log_transport_wiring.py`) — один проверяет механику,
второй **проводку** в composition root. Мёртвых символов нет.
**Доказательства.** `imported_by`: `lib/core/application_context.py`,
`lib/services/db_logging_service.py` (через `attach_transport`) + 6 тестов.

#### class `LogWriteUnavailable` (строки 72–78, 0 методов)
Vердикт `Оставить` — сигнал недоступности транспорта (не «журнал сломан»).

#### class `WriteResult` (строки 82–98, 1 метод)
`accepted`, `dropped`. Вердикт `Оставить`.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `merge` | 94–98 | Сложить два результата по группам | Суммирование счётчиков | `McpLogWriter.write_events` | Оставить |

#### class `_IdentityKey` (строки 102–124, 1 метод)
`session_id`, `user_id`, `request_id` + метод проверки полноты. Вердикт `Оставить`.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `complete` | 114–124 | Подписана ли группа полностью | Неполная подпись → fallback + `dropped` | `group_by_identity` | Оставить |

#### class `IdentityGroup` (строки 128–132, 0 методов)
`key`, `events`. Вердикт `Оставить`.

#### class `CallRunner` (строки 184–188, 1 метод)
`Protocol` для исполнителя корутины. Вердикт `Оставить` — структурная типизация, чтобы
`DbLoggingService` зависел от протокола, а не от реализации.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__call__` | 187–188 | Выполнить корутину | Контракт | `LoopCallRunner` (реализация) | Оставить — **AST даёт ref=0 (ложный ноль: вызов идёт через поле `run`)** |

#### class `LoopCallRunner` (строки 192–241, 1 метод)
Мост «синхронный воркер → async loop». Вердикт `Оставить`.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__call__` | 210–241 | `asyncio.run_coroutine_threadsafe(...).result(timeout)` | Таймаут не даёт воркеру зависнуть навсегда | `McpLogWriter._invoke` через `self.run` | Оставить |

#### class `McpLogWriter` (строки 245–403, 7 методов)
Писец через операцию платформы. Вердикт `Оставить`.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `write_events` | 265–284 | Батч → группы → сумма `WriteResult` | Публичный API транспорта | `db_logging_service._flush_batch_via_mcp` | Оставить |
| `_to_fallback` | 286–289 | Неполные группы → в fallback | Не терять | `write_events` | Оставить |
| `_write_group` | 291–315 | Одна группа → один вызов | Идентичность берётся из вызова | `write_events` | Оставить |
| `_invoke` | 317–318 | Корутина вызова | Обёртка для таймаута | `_write_group` | Оставить |
| `_counters` | 320–343 | Разобрать текстовый ответ платформы | `accepted`/`dropped` в метрики | `_write_group` | Оставить |
| `upsert_question_run` | 345–398 | Прогон вопроса через платформу | Тот же путь без второго пула | `db_logging_service._handle_question_run_via_mcp` | Оставить |
| `_invoke_question_run` | 400–403 | Корутина вызова | Обёртка | `upsert_question_run` | Оставить |

#### class `LocalFallbackSink` (строки 406–493, 5 методов)
Дисковый резерв. Вердикт `Оставить`.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 421–427 | Путь + потолок размера | Инициализация | `application_context.py:554` | Оставить |
| `path` | 430–431 | Текущий путь файла | Диагностика/UI | широкий круг потребителей (89 ссылок в репо) | Оставить |
| `write` | 433–478 | Дописать JSONL, при превышении — ротация | Ничего не терять | `McpLogWriter._to_fallback`, `db_logging_service._write_fallback` | Оставить |
| `_exceeded` | 480–484 | Превышен ли потолок | Условие ротации | `write` | Оставить |
| `stats` | 486–493 | Размер/счётчики | Диагностика | `application_context.py` | Оставить |

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `group_by_identity` | 135–160 | Разрезать батч по `(session_id, user_id, request_id)` | **Обязательное требование операции `log_events`** | `write_events`, `upsert_question_run` | Оставить |
| `event_to_wire` | 163–181 | `LogEvent` → dict для платформы | Сериализация | `_write_group` | Оставить |

---

## `lib/services/session_cold_sync_service.py` — 680 LOC

**Назначение.** Холодное зеркало сессий: upstream JSONL → PostgreSQL (`storage-hybridization`).
**Что делает.** Демон-поток с тактом 30 сек, батчами по `session_key`, под
per-transaction advisory lock (leader-election для нескольких инстансов). На каждый ключ:
прочитать снапшот, прочитать `updated_at` из PG, при расхождении переписать
(`_do_lww_sync`, last-write-wins) либо залогировать stale. Ушедшие вверх ключи удаляются
(`_cleanup_missing`). Метрики через `get_stats()`; пул-метрики читаются из
`utils.db.get_stats()`. Побочные эффекты: фоновый поток, DML в
`agent_session_meta` / `agent_session_messages`, логирование через `try_log_event`.
**Зачем нужен.** Горячий путь остаётся на upstream JSONL (его владеет
`PGSessionManager`), зеркало нужно для чтения историей и холодных запросов.
**Вердикт.** `Оставить`
**Обоснование.** Нужен и покрыт `tests/test_storage_hybridization_lifecycle.py` +
`tests/test_session_cold_sync_service.py`; архитектурный гард
`tests/test_storage_hybridization.py::TestNoDirectSQLToSessionTables` проверяет, что
**hot path** не пишет SQL напрямую (этот сервис — исключение по назначению).
**Доказательства.** `imported_by`: `lib/core/application_context.py` + 3 теста.

#### class `SessionColdSyncService` (строки 63–672, 29 методов)
Вердикт `Оставить`.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 77–144 | Принять пул, имена таблиц, схему, интервалы, пороги | DI + настройки | `application_context.py`, тесты | Оставить |
| `enabled` | 147–148 | Включён ли зеркал | Быстрый выход | `start` | Оставить |
| `start` | 150–162 | Запустить демон | Lifecycle | `ctx.start()` | Оставить |
| `stop` | 164–188 | Остановить и дождаться | Graceful | `ctx.stop()` | Оставить |
| `_worker` | 190–201 | Такт: синк → пауза | Сердце демона | `start` | Оставить |
| `_compute_delay` | 203–210 | Пауза до следующего такта | Соблюдение интервала | `_worker` | Оставить |
| `_do_sync_batch` | 212–256 | Основной цикл по батчу | Синхронизация | `_worker` | Оставить |
| `_sync_cycle` | 258–264 | Обёртка одного такта с catch | Не ронять демон | `_worker` | Оставить |
| `_sync_session_with_detection` | 266–315 | Сверить/перезаписать один ключ, развести stale и lag | Ядро LWW | `_do_sync_batch` | Оставить |
| `_do_lww_sync` | 317–322 | Переписать по последней записи | Применение решения | `_sync_session_with_detection` | Оставить |
| `_is_stale_logged_recently` | 324–333 | Не спамить ли stale-логом | Шум в журнале | `_sync_session_with_detection` | Оставить |
| `_read_upstream` | 335–336 | Список снапшотов из менеджера | Источник | `_do_sync_batch` | Оставить |
| `_upsert_meta` | 338–370 | Upsert метаданных сессии | Мета | `_do_lww_sync` | Оставить |
| `_replace_messages` | 372–401 | Полная замена сообщений ключа | Зеркало | `_do_lww_sync` | Оставить |
| `_cleanup_missing` | 403–438 | Удалить ключи, исчезнувшие вверху | Согласованность | `_do_sync_batch` | Оставить |
| `_read_pg_updated_at` | 440–451 | Момент последней записи в PG | Основа LWW | `_sync_session_with_detection` | Оставить |
| `_count_pg_sessions` | 453–458 | Сколько сессий в зеркале | Метрика расхождения | `_do_sync_batch` | Оставить |
| `_select_all_pg_keys` | 460–467 | Все ключи в PG | Вход для `_cleanup_missing` | `_do_sync_batch` | Оставить |
| `_try_advisory_xact_lock` | 469–489 | Захват advisory lock в транзакции | Leader-election | `_run_in_tx` | Оставить |
| `_run_in_tx` | 491–501 | Транзакционная обёртка | Атомарность цикла | `_do_sync_batch` | Оставить |
| `_log_failure` | 503–522 | Ошибка цикла в журнал | Наблюдаемость | `_sync_cycle` | Оставить |
| `_log_deleted` | 524–538 | Удаление ключа | След | `_cleanup_missing` | Оставить |
| `_log_stale` | 540–571 | Расхождение времён | Диагностика LWW | `_sync_session_with_detection` | Оставить |
| `_log_lag_exceeded` | 573–600 | Отставание зеркала > порога | Метрика здоровья | `_sync_session_with_detection` | Оставить |
| `get_stats` | 602–638 | Метрики циклов/батчей/пула | Health | `application_context.py` | Оставить |
| `_read_pool_size` | 640–660 | `pool_size`/`pool_available` | Метрики пула | `get_stats` | Оставить — деградирует, если `utils.db.get_stats` недоступен |
| `_validate_ident` | 663–665 | Проверка идентификатора | **SQL-инъекция через имя таблицы** | `_quote` | Оставить |
| `_quote` | 668–672 | Квотирование идентификатора | Безопасный DDL/DML | все SQL-пути | Оставить |

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `resolve_default_sqlite_path` | 675–680 | Вывести путь к sqlite-хранилищу сессий по умолчанию | **0 ссылок**; собственный docstring говорит «Хранилище создаёт библиотека» | **0** | **Удалить** |

**Замечание по файлу.** `:14` обосновывает необходимость сервиса через `PgDuckDbSyncService` —
класс, удалённый вместе с кэш-кластером (`AGENTS.md`, запись о `pg_duckdb_sync_service.py`).
Обоснование надо переписать на «зеркало нужно для чтения историей», без ссылки на удалённый класс.

---

## `lib/services/session_storage.py` — 224 LOC

**Назначение.** Создание менеджера сессий и асинхронного сохранения.
**Что делает.** `SessionStorageService.create()` выбирает backend по `storage`
(`auto`/`jsonl`/`postgres`), для PG поднимает пул `workspace/utils/db.py`, создаёт
`PGSessionManager` поверх upstream `SessionManager` и **оборачивает** `manager.save`
через `install_async_save`, чтобы сохранение ушло в `ThreadPoolExecutor(max_workers=1)`
и не блокировало оборот. Побочные эффекты: пул соединений, фоновый поток-исполнитель
(один, на процесс), запись JSONL-файлов.
**Зачем нужен.** Горячий путь хранения; точка выбора backend'а.
**Вердикт.** `Оставить`
**Обоснование.** Нужен; 224 LOC.
**Доказательства.** `imported_by`: `lib/core/application_context.py`,
`tests/test_session_storage.py`, `tests/test_session_storage_async_save.py`.

**Проверенная гипотеза (НЕ дефект).** `install_async_save` кладёт
`ThreadPoolExecutor(max_workers=1)` в `manager._async_save_executor`, и явного
`executor.shutdown()` в коде нет. Я проверил цепочку пересоздания: `gateway.py:123` создаёт
`ApplicationContext` **один раз**, до `run_forever`, и `run_forever(lambda: asyncio.run(_run(ctx)))`
переиспользует тот же `ctx`. Значит при рестартах gateway исполнитель **не** плодится
(в отличие от впечатления, которое даёт чтение `run_forever` как цикла создания контекста),
а на выходе из процесса его дожидает atexit-хук `concurrent.futures`. Утечки нет.

#### class `SessionStorageError` (строки 33–34, 0 методов)
Вердикт `Оставить`.

#### class `SessionStorageService` (строки 112–224, 1 метод)

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `create` | 129–224 | Создать менеджер сессий по конфигу | Единая точка выбора backend'а | `application_context.py`, 18 ссылок в тестах | Оставить |

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `install_async_save` | 37–109 | Обернуть `manager.save` в однопоточный executor | Не блокировать оборот записью | `create` | Оставить |

---

## `lib/services/config_service.py` — 284 LOC

**Назначение.** Резолв `${VAR}` в конфиге и доступ к секциям.
**Что делает.** `load()` читает `config.json`, рекурсивно пре-резолвит ссылки вида
`${VAR}` (с поддержкой дефолтов), **мутирует** переданный `config`-словарь
(`apply_provider_keys`, `apply_timeouts`) и возвращает его. Побочные эффекты: мутация
входного словаря; `sys.modules` не трогается, файлы не пишутся.
**Зачем нужен.** Единственное место, где `${VAR}` превращается в значения; без него
`config.json` нельзя прочитать в среде с секретами.
**Вердикт.** `Оставить` (`apply_provider_keys` — `Упростить`)
**Обоснование.** Механизм нужен; претензия только к публичности одного метода.
**Доказательства.** `imported_by`: `gateway.py` (×3: `:498,:514,:529`),
`lib/core/application_context.py` (×6), `cli_agent.py:226` + 2 теста.

**Важно про AST-счётчики.** `get_int` и `get_str` в инвентаре имеют `ref=0`, что **ложно**:
`lib/core/application_context.py:278-280` и `:312` вызывают их через атрибут
(`ctx.config_service.get_int(...)`), а детектор считает только вызовы по имени. Это второй
пример ложного нуля в группе (первый — `record_registration_failure`).

#### class `ConfigService` (строки 26–284, 9 методов)
Вердикт `Оставить`.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 39–50 | Запомнить пути и override | DI | `ConfigService()`, `application_context.py` | Оставить |
| `settings` | 57–70 | Ленивый merged SETTINGS | Доступ к конфигу | `settings_section`, потребители | Оставить |
| `settings_section` | 72–82 | Верхнеуровневая секция как dict | **Основной** путь чтения | `gateway.py:498,514,529`; `application_context.py:291,300,1307,1312,1451,1609`; `cli_agent.py:226` | Оставить — **AST даёт ref=1 (ложно низкий)** |
| `get_int` | 84–95 | Число по пути с дефолтом | Таймауты/лимиты | `application_context.py:278-280` | Оставить — **AST ref=0 ложный** |
| `get_str` | 97–104 | Строка по пути | `storage_mode` | `application_context.py:312` | Оставить — **AST ref=0 ложный** |
| `load` | 110–157 | Прочитать + пре-резолвить + мутировать config | Точка загрузки | `application_context.py`, `gateway.py` | Оставить |
| `_pre_resolve_env_refs` | 159–240 | Рекурсивный резолв `${VAR}` с дефолтами | Ядро | `load` | Оставить |
| `apply_provider_keys` | 246–257 | Подставить provider-ключи в config | Заполнение секретами | **только** `load:156` | **Упростить** — единственный вызов внутренний, имя публичное без надобности; переименовать в `_apply_provider_keys` |
| `apply_timeouts` | 259–284 | Проставить таймауты в config | Согласование config и SETTINGS | `application_context.py:276` | Оставить |

---

## `lib/services/enterprise_mcp_client.py` — 609 LOC

**Назначение.** Единственный клиент агента к процессу `enterprise-mcp`.
**Что делает.** Долгоживущая stdio-сессия: `call()` поднимает сессию, формирует `_meta`
из `CallIdentity` (клиент **не** доверяет значениям из тела вызова), вызывает операцию,
разбирает доменную ошибку из префикса `[code]` в тексте. `_ensure_session()` — ленивый
восстановление оборвавшейся сессии; нормальный путь — рукопожатие на старте в
`gateway._connect_enterprise_mcp(ctx)`. `client_from_settings()` дописывает `--profile <имя>`
в args (только для не-`prod` профиля). Побочные эффекты: дочерний процесс, stdio-канал,
собственный MCP-клиент внутри агента.
**Зачем нужен.** Единственный путь агента к данным, векторам, аудиту и журналу платформы.
Без него агент не читает PG ради данных.
**Вердикт.** `Оставить`
**Обоснование.** Ключевой модуль новой архитектуры; контракт `_child_env` соблюдён.
**Доказательства.** `imported_by`: 21 файл. Продакшн: `lib/core/application_context.py`,
`gateway.py`, `cli_agent.py`, `lib/channels/queue_ops.py`, `lib/services/log_transport.py`
+ три tool'а (`audit_analyzer_query`, `history_search_tool`, `legal_summarizer_query`).
12 тестов, включая `test_enterprise_mcp_identity.py`, `test_enterprise_mcp_meta_interception.py`,
`test_enterprise_mcp_config.py`.

**Целевая проверка `_child_env` (`:485-513`) — чисто.** Метод наследует `os.environ`
целиком и добавляет **только** `PYTHONIOENCODING`. Ни `ENTERPRISE_LLM_*`, ни
`ENTERPRISE_VECTOR_*`, ни `ENTERPRISE_EMBED_*`, ни `ENTERPRISE_SNAPSHOT_PATH` не
экспортируются. Декларации живут в `mcp-platform/platform.json`. Страж
`tests/test_enterprise_mcp_settings_contract.py` это фиксирует; я перепроверил вручную.

#### class `CallIdentity` (строки 73–131, 2 метода)
`session_id`, `user_id`, `request_id` → `_meta` платформы. Вердикт `Оставить`.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `with_request_id` | 98–104 | Копия с проставленным `request_id` | Подписать вызов | `call:344` | Оставить |
| `as_meta` | 106–131 | `_meta` для платформы | Протокол | `_meta_for:396` | Оставить |

#### class `CallIdentityIncomplete` (строки 134–140, 0 методов)
Вердикт `Оставить` — fail loud вместо отправки неподписанного вызова.

#### class `EnterpriseMcpUnavailable` (строки 143–144, 0 методов)
Vердикт `Оставить` — 14 ссылок, различает «процесс недоступен» и «операция вернула ошибку».

#### class `EnterpriseOperationError` (строки 147–157, 1 метод)
Доменная ошибка платформы, код разобран из `[code]`. Вердикт `Оставить`.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 154–157 | Сохранить `code` + `message` | Разбор вызывающим | `call` | Оставить |

#### class `EnterpriseMcpClient` (строки 177–513, 12 методов)
Вердикт `Оставить`.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 191–215 | Запомнить команду/аргументы/таймаут | DI | `client_from_settings` | Оставить |
| `is_connected` | 220–221 | Жива ли сессия | Диагностика | `gateway`, тесты | Оставить |
| `describe` | 223–231 | Описание процесса | Стартовый отчёт | `gateway._report_enterprise_mcp_health` | Оставить |
| `list_operations` | 235–262 | Список операций | Сводка по capability на старте | `gateway._report_enterprise_mcp_health` | Оставить |
| `_identity_from_turn` | 264–322 | Собрать личность оборота | Без неё платформа отвергнет вызов | `call` | Оставить |
| `call` | 324–385 | Вызов операции с `_meta` | Основной API | 5 tool'ов/модулей, `log_transport` | Оставить — 19 ссылок |
| `_meta_for` | 387–410 | Собрать/проверить `_meta` | Инвариант подписи | `call` | Оставить |
| `_ensure_session` | 414–445 | Ленивое поднятие/переподнятие сессии | Восстановление, не норма | `call`, `list_operations` | Оставить |
| `_reset` | 447–454 | Сбросить сессию | После обрыва | `_ensure_session` | Оставить |
| `aclose` | 456–457 | Async-закрытие | Async-путь | `gateway.py` внутри loop | Оставить |
| `close` | 459–483 | Синхронное закрытие процесса | Shutdown | `gateway.py`, `ApplicationContext.stop()` | Оставить — 32 ссылки |
| `_child_env` | 485–513 | Окружение дочернего процесса | **Контракт: только `PYTHONIOENCODING`** | `__init__` при старте процесса | Оставить — проверено вручную, чисто |

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_new_request_id` | 47–60 | Сгенерировать `request_id` | Подпись вызова | `_identity_from_turn` | Оставить |
| `_first_text` | 160–166 | Первый текстовый блок ответа | Разбор результата | `call` | Оставить |
| `_split_error_code` | 169–174 | Разобрать `[code] остаток` | Доменная ошибка | `call` | Оставить |
| `_journal_min_level` | 532–551 | Порог журнала из `logging.db.min_level` | Согласовать порог перед вызовом | `client_from_settings:598` | Оставить — **AST даёт ref=2, живая** |
| `client_from_settings` | 554–609 | Собрать клиента из настроек, дописать `--profile` | Единственная фабрика | `ApplicationContext._make_enterprise_mcp`, `gateway.py`, `cli_agent.py` | Оставить |

---

## `lib/services/turn_delivery_factory.py` — 288 LOC

**Назначение.** Fallback-ответ пользователю при internal-ошибке оборота.
**Что делает.** `FallbackTurnDeliveryFactory` — фабрика штатного публичного расширения
`nanobot` (`AgentLoop(turn_delivery_factory=...)`), заменившая патч
`patch_turn_delivery_fail` (решение зафиксировано в
`docs/architecture/decisions/turn-delivery-public-extension.md`).
`create()` и `unrouted()` **переклассифицируют** полученный upstream объект
`_adopt()`: меняют `__class__` на `FallbackTurnDelivery`, поэтому отказ внутри оборота
превращается в внятный текст вместо молчания. `fail()` публикует fallback в шину
и, опционально, пишет `turn_failed` в журнал. Побочные эффекты: запись в журнал,
публикация в шину.
**Зачем нужен.** Пользователь должен получить ответ вместо тишины.
**Вердикт.** `Оставить`
**Обоснование.** Канон — «выход через публичную точку, не патчем»; реализация чистая,
288 LOC. Единственное замечание — `delivery.__class__ = FallbackTurnDelivery` (`:217`)
переназначает класс чужому объекту; это работает для CPython, но ломается при смене
интерпретатора или если upstream начнёт использовать `__slots__`/проверку типа. Стоит
закомментировать как хрупкое место (в коде комментарий есть).
**Доказательства.** `imported_by`: `lib/core/agent_factory.py` (единственный
продакшн-вызов), `tests/test_turn_delivery_factory.py`. Плюс косвенный потребитель
`tests/test_final_delivery_is_signed.py`.

#### class `FallbackTurnDelivery` (строки 61–175, 3 метода)
Поля: `_fallback_text`, `_log_to_db`, `_db_logging_service`, `_agent_id`. Вердикт `Оставить`.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `fail` | 75–100 | Опубликовать fallback в шину | Ответ пользователю | upstream `AgentLoop` при internal-ошибке | Оставить — 17 ссылок |
| `_publish_fallback` | 102–116 | Сборка и публикация сообщения | Фактическая отправка | `fail` | Оставить |
| `_log_turn_failed` | 118–175 | Записать `turn_failed` в журнал | Наблюдаемость отказа | `fail` | Оставить |

#### class `FallbackTurnDeliveryFactory` (строки 178–222, 4 метода)
Вердикт `Оставить`.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 185–200 | Принять шину, текст, признак логирования, журнал, `agent_id` | DI | `build_turn_delivery_factory`, `agent_factory.py` | Оставить |
| `create` | 202–211 | Создать delivery + переклассифицировать | Основной путь | `AgentLoop` | Оставить — 18 ссылок |
| `unrouted` | 213–214 | То же для неадресованного сообщения | Полнота | `AgentLoop` | Оставить |
| `_adopt` | 216–222 | `delivery.__class__ = FallbackTurnDelivery` | Механизм перехвата | `create:209`, `unrouted:214` | Оставить с оговоркой о хрупкости |

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `build_turn_delivery_factory` | 225–273 | Фабрика по настройкам | Единая точка сборки | `lib/core/agent_factory.py` | Оставить |
| `_get` | 276–288 | Чтение вложенного ключа из `settings` | Таймауты/флаги | `build_turn_delivery_factory` | Оставить |

---

## Удалить — сводный список

| Файл | Символ | Строки / LOC | Почему мёртвый | Что сделать перед удалением | Что сломается |
|---|---|---|---|---|---|
| `lib/services/runtime_patcher.py` | `_resolve_agent_id` | 422–458 / 37 LOC | Единственный потребитель `patch_turn_delivery_fail` удалён; grep по всему репо (`lib/`, `tests/`, `tools/`, `openspec/`, `docs/`, `config.json`) даёт **0 вызовов**. Динамического доступа (`getattr`/`importlib`/`globals()`) нет. В `lib/core/application_context.py:1269` живёт независимая копия той же логики — удаление из патчера её не затрагивает. | Удалить функцию; убедиться, что `apply_all` перестал использовать параметр `config` (он уже не использует) — снять и его. Прогнать `tests/test_runtime_patcher.py`, `test_patch_spec_consistency.py`, `test_application_context_single_application_point.py`. | Ничего в рантайме. `PatchReport`/`_PATCH_SPECS` не затронуты. |
| `lib/services/runtime_inventory.py` | `diff_project_tools_from_detail` | 387–394 / 8 LOC | **0 ссылок в репо.** `tools/diagnose_startup.py:202-206` импортирует только `diff_hooks`/`diff_project_tools`/`diff_runtime_patches`. `tests/test_tools_project_loader.py:503` лишь фиксирует в докстринге, что баннер **раньше** звал этот метод. | Удалить функцию. Обновить докстринг `tests/test_tools_project_loader.py:503` («баннер вызывал `diff_project_tools_from_detail`» — уже неправда). | Ничего; `diff_project_tools` (структурный) остаётся и используется. |
| `lib/services/runtime_inventory.py` | `parse_project_tools_detail` | 351–384 / 34 LOC | **Продакшн-потребителя нет.** Единственные вызывающие — `diff_project_tools_from_detail` (тоже мёртвая) и **тесты**: `tests/test_runtime_inventory.py:365,380,391`. Формат `detail`, который она разбирает, остаётся как соглашение (ссылки в `project_tool_loader.py:26,61,223,324` и `application_context.py:1055`), но потребитель формата — баннер на структурных полях, а не regex. | Удалить функцию **вместе с 3 тестами** в `tests/test_runtime_inventory.py:365,380,391`. Отдельно решить судьбу самого формата `detail`: он остаётся только для человека — тогда оставить строковую сборку без парсера. | `tests/test_runtime_inventory.py` потеряет 3 теста; `tools/diagnose_startup.py` и баннер продолжат работать без изменений. |
| `lib/services/db_logging_service.py` | `log_sync_event` | 1177–1205 / 29 LOC | **0 вызовов в коде.** Потребитель `pg_duckdb_sync_service.py` удалён с кэш-кластером (`AGENTS.md`). Осталась только проза: `openspec/specs/logging-db/spec.md` и `PENDING-DELETIONS.md:514`. Docstring `:1189` описывает «PG→DuckDB sync-путь», которого нет. | Удалить метод. Обновить спеку `openspec/specs/logging-db/spec.md` (6 упоминаний) — снять требование «события sync-пути пишутся через `log_sync_event`», иначе спека разойдётся с кодом. Проверить `PENDING-DELETIONS.md:514`. | Ничего в рантайме. Платы за невыполненное требование спеки: формально спека требует метод, которого нет. |
| `lib/services/session_cold_sync_service.py` | `resolve_default_sqlite_path` | 675–680 / 6 LOC | **0 ссылок в репо**, ни через `getattr`, ни через `importlib`. Собственный docstring утверждает «Хранилище создаёт библиотека» — функция противоречит сам себе. | Удалить функцию. | Ничего; хранилище сессий создаёт upstream `SessionManager` (см. `session_storage.py`). |

**Суммарно: 5 символов, 114 LOC.** Ни один файл не удаляется целиком.

### Кандидаты «упростить», которые я НЕ включил в таблицу удаления (нужно решение владельца)

| Файл | Символ | Строки / LOC | Вопрос к владельцу |
|---|---|---|---|
| `lib/services/runtime_events_subscriber.py` | `RuntimeEventsSubscriber.unidentified_turn_events` | 167–174 / 8 LOC | 0 вызовов, но это осмысленный диагностический счётчик. Либо показывать его в health-выводе (тогда он нужен), либо удалить. Сейчас — ни то, ни другое. `duplicates.md:52` ошибочно помечает его как дубликат `GatewayRunner.reset_backoff` (совпадение по форме). |
| `lib/services/db_logging_service.py` | `order_turn_rows` + `TurnOrder` | 384–395, 398–420 / 35 LOC | 0 вызовов в репо, но задокументирован в `docs/ARCHITECTURE.md:341` как API для **читателей** журнала (вне агента). Проверьте, есть ли внешний потребитель; если нет — удалять нельзя только потому, что документ; документ тогда правлю. |

---

## Кросс-подсистемные находки (вне `lib/services/`, видны из моей группы)

1. `lib/core/application_context.py:688-690` — комментарий «Загрузка кэша уже выполнена в composition root (`_init_cache_runtime`)». Метода `_init_cache_runtime` **не существует** (grep по всему репо: только эти комментарии). `tests/test_application_context_cache_lifecycle.py` прямым текстом говорит: «`_init_cache_runtime`, которого в агенте больше нет». Комментарий-остаток снятого кэш-кластера.
2. `lib/core/application_context.py:421` — комментарий «Readiness: PG/duckdb/vector». Проверок `duckdb` и `vector_search` в `RuntimeReadiness` **нет**; строки 1160–1162 фиксируют, что они сняты в фазе 5. Комментарий вводит в заблуждение при чтении стартового вердикта.
3. `gateway.py:154-158` — комментарий про `_init_cache_runtime` и «Кэш к этому моменту уже загружен»; `gateway.py:592` — «(прогрев кэша DuckDB)». Остатки снятого кластера в документации точки входа.
4. `lib/core/application_context.py:688-690` и `gateway.py:156` — оба ссылаются на несуществующий `_init_cache_runtime`; это **одна и та же** находка в двух файлах.
5. `lib/hooks/database_logging_hook.py:252-…` — дубликат `context_compaction.py:583-603` (`_current_request_sender_id`, 21 LOC). Две копии одной функции, вызываются из разных подсистем; при изменении формата `sender_id` разъедутся. Кандидат на `Слить`.
6. `lib/utils/node_access.py:36-…` (`get_settings_section`) и `ConfigService.settings_section` (`config_service.py:72-82`) — две реализации чтения секции конфига. Не идентичны (`node_access` дополнительно терпит non-dict), но дублируют назначение. Кандидат на `Слить`.
7. `docs/audit/AUDIT_PROTOCOL.md` § 1 — сама таблица слоёв устарела: `lib/services/` описана как «кэш DuckDB, логирование, патчи рантайма, health, LLM». Кэша DuckDB и LLM-клиента в `lib/services` больше нет (18 из 19 моих файлов — про logging/health/patches/composition). Протокол — основание для всех отчётов, поэтому его правка затрагивает и читателей отчётов.
8. `workspace/hooks/session_file_redirect_hook.py` — перенаправляет файлы сессии в `workspace/data_store/cache/sessions/<key>/`; каталог `data_store/cache/` достался от снятого кэш-кластера. `runtime_inventory.py:98-122` повторяет этот путь в описании `SessionFileRedirectHook`. Вне моей группы (hooks), но строки лгут.
9. `docs/audit/_data/duplicates.md:52` — ложное срабатывание детектора дубликатов: `RuntimeEventsSubscriber.unidentified_turn_events` (8 LOC) отмечен как дубль `GatewayRunner.reset_backoff` (4 LOC) исключительно по форме. Детектору стоит сравнивать имена/семантику, а не длину.

---

## Как я это проверял (воспроизводимость)

- `git branch --show-current` → `refactor/mcp-platform`; `git log --oneline -3` → `f9310da`.
- Символы, строки, подписи и счётчики — из `docs/audit/_data/inventory.json` (свежий AST), выгружены
  одноразовым скриптом `docs/audit/_scripts/_tmp_dump_syms.py` (вывод — `_tmp_syms.txt`)
  и скриптом сверки покрытия `docs/audit/_scripts/_tmp_check.py`. **Оба файла НЕ удалены:**
  удаление в этой сессии запрещено политикой безопасности (`mavis-trash` недоступен).
  Их нужно удалить вручную — они не относятся к артефактам аудита.
- **Сверка полноты покрытия скриптом** (разбор моего же отчёта): 19 файловых секций,
  сумма LOC 8936 — совпала с `inventory.json`; 47 заголовков `#### class`;
  238 строк таблиц методов/функций против 237 ожидаемых символов (181 метод + 56 функций);
  распределение вердиктов по строкам таблиц: 227 Оставить · 5 Упростить · 5 Удалить · 1 Слить.
- **Перепроверка grep'ом** (не доверял счётчикам) по всему репо:
  `_resolve_agent_id`, `ContextWindowNotSeededError`, `parse_project_tools_detail`,
  `diff_project_tools_from_detail`, `resolve_default_sqlite_path`, `log_sync_event`,
  `record_registration_failure`, `order_turn_rows`, `TurnOrder`, `unidentified_turn_events`,
  `_default_actor`, `_json_safe`, `journal_level_rank`, `next_event_seq`, `_count_by_type`,
  `purge_empty_outbound`, `readiness.check`, `settings_section`, `apply_provider_keys`,
  `apply_timeouts`, `make_inbound_logger`, `make_outbound_logger`, `_note_registration_failure`,
  `_adopt`, `_journal_min_level`, `_estimate_fallback`, `compact(`, `install_async_save`,
  `_async_save_executor`, `ApplicationContext.create`, `run_forever`, `attach_transport`.
- **Прогон гвард-тестов:** `tests/test_runtime_patcher.py`,
  `tests/test_runtime_inventory.py`, `tests/test_patch_spec_consistency.py` → **83 passed**.
  Второй набор: `test_no_cache_api_remains.py`, `test_enterprise_mcp_settings_contract.py`,
  `test_context_compaction.py`, `test_log_transport.py`, `test_log_transport_wiring.py`,
  `test_schema_validation.py`, `test_session_cold_sync_service.py`,
  `test_storage_hybridization.py`, `test_journal_level_canonical.py` → **242 passed, 1 skipped**.
- **Сверка канона вручную:** 5 файлов `workspace/tools/*.py` против 5 записей
  `canonical_project_tools()` — имена сверены со свойством `name` каждого класса
  (`compact_context`, `history_search`, `legal_summarizer_query`, `audit_analyzer_query`,
  `document_read`); 3 файла `workspace/hooks/*.py` против 3 записей
  `canonical_plugin_hooks()`; 6 патчей `_PATCH_SPECS` против 6 строк таблицы
  `docs/architecture/runtime-patcher-inventory.md:49-54`.
- **Визуальная проверка:** `workspace/overrides/` содержит ровно один файл
  (`agent/consolidator_archive.md`) — единственный use case `consolidator_locale`;
  `config.json → channels.postgres.schema` = `"public"` (поэтому захардкоженная схема в
  `schema_validation.py:244` сегодня совпадает, но при смене значения разойдётся).

**Не проверено / требует запуска.**
- `tests/test_startup_schema_validation_live.py` требует живой БД — в этом аудите не запускался;
  вывод про `schema_validation.py:244` сделан чтением `config.json` и кода, не наблюдением.
- Фактическое поведение `compact()` с `idle=False` подтверждено чтением кода
  (`context_compaction.py:142,153-165`), а не запуском tool'а.
- `log_transport` в реальном обороте (реальный вызов `log_events` платформы) не запускался —
  вердикт основан на чтении и гвард-тестах.
