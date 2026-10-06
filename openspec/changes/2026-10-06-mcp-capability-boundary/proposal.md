# Граница MCP: capability вместо зеркала `DataService`

## Why

Capability `data` объявляет **23 операции**, и каждая из них — тонкий адаптер
одного метода `DataService`. Это не совпадение сборки, а правило, выведенное из
одного наблюдения: «у сервиса есть метод → у него есть операция». Наблюдение
верное для внутреннего API и неверное для MCP API, и разница между ними нигде
не записана, потому что `data` — это имя хранилища, а не имя того, что система
умеет.

Переход «метод → операция» виден прямо в объявлениях. Разобранный перечень
(`ToolDefinition` → `handler` → вызов сервиса, AST, а не регулярка):

| операция | метод `DataService` | кто зовёт |
|---|---|---|
| `data.claim_task` | `claim_tasks` | `lib/channels/queue_ops.py:191` |
| `data.release_claimed_tasks` | `release_claimed_tasks` | `lib/channels/queue_ops.py:277` |
| `data.unstick_tasks` | `unstick_tasks` | `lib/channels/queue_ops.py:287` |
| `data.update_task_status` | `update_task_status` | `lib/channels/queue_ops.py:321` |
| `data.queue_stats` | `queue_stats` | `lib/channels/queue_ops.py:494` |
| `data.append_assistant_message` | `append_assistant_message` | `lib/channels/queue_ops.py:347` |
| `data.patch_message_metadata` | `patch_message_metadata` | `lib/channels/queue_ops.py:372` |
| `data.merge_tool_delivery` | `merge_tool_delivery` | `lib/channels/queue_ops.py:392` |
| `data.finalize_turn` | `finalize_turn` | `lib/channels/queue_ops.py:420` |
| `data.fail_task` | `fail_task` | `lib/channels/queue_ops.py:446` |
| `data.append_reasoning` | `append_reasoning` | `lib/channels/queue_ops.py:473` |
| `data.get_message` | `get_message` | `lib/channels/queue_ops.py:484` |
| `data.append_history_notice` | `append_history_notice` | `lib/services/context_compaction.py:578` |
| `data.mirror_session` | `mirror_session` | `lib/gateway/mirror/session_mirror.py:77` |
| `data.session_mirror_state` | `session_mirror_state` | `lib/gateway/mirror/session_mirror.py:79` |
| `data.cleanup_session_mirror` | `cleanup_session_mirror` | `lib/gateway/mirror/session_mirror.py:78` |
| `data.log_events` | `log_events` | `lib/services/log_transport.py:72` |
| `data.upsert_question_run` | `upsert_question_run` | `lib/services/log_transport.py:74` |
| `data.purge_logs` | `purge_logs` | `lib/services/log_transport.py:76` |
| `data.history_search` | `history_search` | модель, `config.json:878` |
| `data.schema_check` | `schema_check` | `gateway.py:363`, проба при подъёме |
| `data.log_event` | `log_event` | **никто** |
| `data.delete_assistant_message` | `delete_assistant_message` | **никто** |

Две последние строки — тот самый случай, который невозможно увидеть в названии.
Проверено отсечением: поиск `\.log_event\(` и `\.delete_assistant_message\(` по
всему дереву `mcp-platform` **вне `tests/`** даёт ровно по одному совпадению, и
оба — тело соответствующей операции. То есть MCP-обёртка является единственным
вызывающим метода сервиса, который вызывает только она. Это операции без
потребителя вообще: их существование оправдано только существованием метода.

### Видимость объявлена, но не обеспечена

`tags=("runtime-only", …)` стоит у служебных операций, и это читается как
security boundary. Её нет:

- `build_server` публикует в `list_tools` **всё зарегистрированное** —
  `loader.py:346-353`, там стоит `for definition in registry`, а не отбор;
- `ToolDefinition.permissions` читают только тесты. Это признано в собственной
  документации: `mcp-platform/docs/MCP-CONTRACTS.md:718` — «`permissions`
  читается только тестами»;
- страж утверждает обратное в тексте: `tests/test_server_bootstrap.py:539` —
  «Фильтрация model-facing инструментов живёт на стороне агента и работает по
  `permissions`/`tags`». На деле агент фильтрует по **именам** в
  `enabled_tools` (`config.json`), а не по тегам.

Итог: из 23 операций **22** адресованы внутренним потокам процесса агента,
и модель может вызвать любую из них, включая `data.purge_logs` (обслуживание
журнала) и `data.finalize_turn` (запись оборота). Метаданные выдают себя за
границу, которой нет.

**Не путать с одноимённым понятием.** `libs/enterprise_data/audience.py` — это
класс работы в пуле PostgreSQL (чьё потолок времени и лимит ожидания
применяются к соединению). Значения там те же (`model`/`runtime`), смысл другой:
пул не должен знать про видимость инструментов, а реестр — про распределение
соединений. Общего кода у них не будет намеренно.

### Что уже сделано и потому в объём не входит

Три пункта исходной постановки канон уже держит, и дублировать их — значит
заявить о платформе то, чего в ней нет:

- **форма имени.** `ToolDefinition.capability` обязателен
  (`registry.py:85`), форма `<capability>.<operation>` сверяется в
  `validate_operation_name` (`registry.py:195`), а принадлежность объявленным
  сервером capability — в `ToolRegistry.register` (`registry.py:426`). Каталог
  сверяется отдельно (`loader.py:217`). Это change
  `2026-10-05-tool-capability-namespace`, он архивируется и не дублируется.
- **идентичность.** `validate_handler` **отвергает** объявления параметров
  `session_id`/`user_id`/`request_id` (`registry.py:116`, `:145`): операция не
  может принять личность аргументом. Плоский transitional-адаптер для Nanobot
  0.3.5 уже стоит на границе MCP — `execution/pipeline.py::_resolve_identity`,
  то есть именно там, где его и предписывает постановка.
- **тонкость обёртки.** Операции уже не содержат бизнес-логики.

### Два дефекта, найденных попутно

1. `settings.py:952-994` объявляет в `data` **20** операций, на диске **23**:
   выпали `data.mirror_session`, `data.session_mirror_state`,
   `data.cleanup_session_mirror`.
2. Страж, который обязан это заметить, **односторонний**:
   `tests/test_settings_registry.py:542` утверждает `set(cap.tools) - found`, то
   есть реестр → диск, и никогда `found - set(cap.tools)`. Дрейф диск → реестр
   невидим — потому рассинхрон и держится зелёным. Симптом и лечение описаны в
   `2026-10-05-tool-capability-namespace` § «Стражи замолкают», этот страж —
   очередной случай того же класса, и он будет чиниться вместе с границей, а не
   после неё.

## What this change does

Три решения, принятые владельцем до начала правок:

1. **Полное разнесение.** `runtime.*`, `history.*`, `observability.*`; capability
   `data` становится service-only; стражи, требующие операций у каждого
   каталога capability, правятся.
2. **Настоящее поле `audience`** (`model` / `runtime` / `admin`) и публикация в
   `list_tools` только для `model`. Это единственный enforcement, который
   честно работает при текущем контракте идентичности.
3. **Внутренние операции из MCP уходят**, а не получают новые имена.

### Классификация: внешняя / runtime / внутренняя

| операция | класс | основание |
|---|---|---|
| `data.history_search` | внешняя | в `enabled_tools`; изоляция по `session_id`+`user_id` |
| `data.schema_check` | runtime | проба при подъёме `gateway.py:363`, в `enabled_tools` её нет |
| `data.purge_logs` | runtime | `log_transport.py:76`; обслуживание журнала, но вызывает его рантайм, не человек |
| 18 остальных | runtime | перечисленный выше вызывающий модуль |
| `data.log_event` | **внутренняя** | вызывающих нет; обёртка — единственный вызывающий метода |
| `data.delete_assistant_message` | **внутренняя** | то же |

`runtime.*` остаются MCP-операциями, и это не выбор, а следствие устройства:
платформа живёт в отдельном процессе, и клиент агента
(`lib/services/enterprise_mcp_client.py`) — единственный путь к ней из фоновых
служб. Прямой вызов сервиса в обход MCP невозможен не по вкусу, а потому что
сервиса в процессе агента нет.

### Целевой состав

| операция | operations | аудитория | сервис | основание |
|---|---|---|---|---|
| `runtime.task` | claim, update, release, fail, unstick, stats | runtime | `DataService` | жизненный цикл очереди; один модуль вызова |
| `runtime.message` | get, append, patch_metadata, append_reasoning, merge_tool_delivery, append_history_notice | runtime | `DataService` | одна таблица, один модуль вызова плюс compaction |
| `runtime.session` | mirror, state, cleanup | runtime | `DataService` | зеркало сессии; один модуль |
| `observability.journal` | append_batch, purge | runtime | `DataService` | долговечный журнал; один модуль |
| `observability.run` | upsert | runtime | `DataService` | запись прогона — другая сущность и другая таблица |
| `history.search` | — | model | `DataService` | единственная внешняя capability |
| `platform.schema_check` | — | runtime | `DataService` | проба состава платформы, не бизнес-состояние |

23 операции → 7 инструментов. Число не является целью: цель — отсутствие
объявлений, оправданных существованием метода.

Две поправки к исходной постановке, обе — по коду, а не по вкусу:

- **`observability.events` с `append`/`append_batch` невозможен.** Операции
  «одна запись» нет: `data.log_event` никем не вызывается. Единственная
  оставшаяся операция журнала — пакетная, и инструмент с перечислением из
  одного значения хуже, чем два инструмента без перечисления.
- **`schema_check` — платформенная операция, а не `observability`.** Она
  отвечает про таблицы, объявленные слоем платформы (`server.py:279-301`), а
  про состояние capability `data`. Объявление в `servers/enterprise/tools/`
  снимает сразу две вещи: сверку с каталогом (это свойство пути, а не
  объявления, `loader.py:123`) и требование стражей к наличию capability в
  `_ALL_CAPABILITIES`.

### Служебные операции получают настоящую аудиторию

Поле `audience` обязательное и без значения по умолчанию — по той же причине,
что и `capability`: молчаливое `model` опубликовало бы служебную операцию, а
молчаливое `runtime` спрятало бы рабочий инструмент у модели. Разбор 35
объявлений: 9 `model` (ровно те, что в `enabled_tools`), 1 `admin`
(`vectors.index_stats` — диагностика, в белом списке её нет), 25 `runtime`.

## Граница

Не входит:

- **дробление `DataService`.** Цель — граница MCP, а не новая разметка
  сервисов. Все семь инструментов вызывают существующие методы; ни одного нового
  `TaskService`/`MessageService` не появляется.
- **`data.execute` и любой универсальный RPC.** Объединение операций делается
  только внутри одной сущности, и перечисление видно в `inputSchema`.
- **отказ на вызове.** Идентичность плоская
  (`platform.json → execution.require_call_meta=false`), роли вызывающего в ней
  нет, отличить рантайм от модели на проводе нечем. Граница — публикация плюс
  белый список агента; право на полноценный authorization остаётся отдельным
  вопросом и в этом change не решён.
- **перенос бизнес-логики в операции.** Обёртки остаются тонкими адаптерами.
- **upstream Nanobot.** Ни `nanobot-ai==0.3.5`, ни его зависимости. Всё — в
  enterprise overlay.
- **ключи файла настроек.** `PROFILE_OWNED_KEYS` (`settings.py:1350`) —
  `data.log_table`, `data.question_runs_table`, `data.task_table`. Имена таблиц
  журнала и прогонов доходят до платформы по проводу, и смена ключа сломала бы
  оверлей профилей. Секция `data` в `platform.json` остаётся; новые capability
  объявляют те же настройки как заинтересованные, что `platform.json` уже
  поддерживает (`:1233`, «может быть несколько»).

## Последствия, которые надо учесть до правки кода

Фильтр `list_tools` ломает не только ожидаемое. Часть стражей читает схемы
операций **через discovery**, и после фильтра они либо падают, либо — что хуже —
начинают проверять меньше, чем раньше:

| место | что станет |
|---|---|
| `test_server_bootstrap.py:650-653` | `set(registry.names()) == {t.name}` — утверждение **переворачивается**: discovery обязан быть подмножеством |
| `test_server_bootstrap.py:655-658` | «у каждой операции есть описание» — начнёт пропускать служебные, останется зелёным на неполной выборке |
| `test_server_bootstrap.py:674-678` | `tools[definition.name]` для служебной операции — `KeyError` |
| `test_server_bootstrap.py:876-877` | `assert "data.schema_check" in names` — падение по двум причинам сразу |
| `test_operation_schema_permissiveness.py:59-63` | словарь схем пустеет для служебных |
| `test_tool_execution_pipeline.py:947-953` | то же, зависит от того, какие операции читает |
| `test_template_server_contract.py:78` | `<=` останется верным только если шаблон объявлен `model` |

Схемы служебных операций надо брать **из реестра**, а не из discovery: это
внутриплатформенный вопрос, и подписывать его публикацией для модели — значит
делать видимость зависимостью от того, что видно. Канон это фиксирует отдельным
требованием, иначе страж «у каждой операции есть описание» тихо ослабнет.

## Решение владельца

1. **Разнесение** — вариант A (полное, `data` → service-only), 2026-10-06.
2. **Видимость** — вариант 1 (`audience` + фильтр `list_tools`), 2026-10-06.

Отвергнуто осознанно: вариант B (оставить `data.*` как namespace, вынести только
`history.search` и `platform.schema_check`) — дешевле в обвязке, но MCP API
продолжал бы описывать структуру хранилища; вариант 2 во втором вопросе (отказ на
вызове) — потребовал бы отдельного транспорта или учётных данных.