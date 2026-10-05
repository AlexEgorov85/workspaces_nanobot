# Задачи: имя операции объявляет capability

## 1. Инвариант формы имени в единственной точке регистрации

Проверка встаёт в `ToolRegistry.register`: это единственное место, через которое
проходят оба пути — загрузка из каталога capability и прямая регистрация
платформенных операций (`servers/enterprise/server.py:635-651`). Проверка в
`load_definition` вторая по счёту и нужна только ради сообщения с путём до файла.

- [ ] 1.1 В `libs/enterprise_common/registry.py::ToolRegistry.register` проверять
  форму `name`: ровно одна точка, обе половины непустые, в operation точек нет
- [ ] 1.2 Отказы SHALL быть разными и называть разное: пустое имя (проверка
  непустоты сохраняется), нет точки, лишняя точка, пустая операция. Одно
  сообщение на все четыре выглядело бы одним дефектом, а чинятся они по-разному
- [ ] 1.3 Сверять префикс `name` с полем capability. Сообщение SHALL называть обе
  стороны: объявленное имя с префиксом и объявленную capability
- [ ] 1.4 НЕ подставлять capability из имени, даже когда префикс сходится: при
  пустом поле отказ «не сказано», а подстановка превратила бы сверку в
  проверку объявления против самого себя
- [ ] 1.5 Проверять различимость: два зарегистрированных имени не должны давать
  одно и то же после замены точки на подчёркивание. Сравнение — по имени на
  проводе, без префикса `mcp_<сервер>_`: префикс общий для всех операций
  процесса и на равенство не влияет
- [ ] 1.6 В `load_definition` продублировать сверку, чтобы отказ называл путь до
  файла. Порядок проверок: форма имени → сверка с capability → сверка с
  каталогом (последняя уже добавлена change
  `2026-10-05-call-boundary-invariants`, порядок не переставлять без причины —
  сообщения должны оставаться различимыми)
- [ ] 1.7 Обновить докстринг `ToolDefinition` (`registry.py:65-74`): там
  сказано, что автор объявляет принадлежность дважды — каталогом и полем. После
  этой дельты — трижды, и третье (имя) проверяется отдельно от первых двух
- [ ] 1.8 Обновить докстринг модуля `loader.py` (`:3-29`): пример объявления в
  шапке сейчас показывает `name="log_event"`, `category="data"`, и после
  переезда он перестанет соответствовать правилу, которое сам же и декларирует

## 2. Переезд имён операций

32 файла в `capabilities/*/tools/*.py` плюс две платформенные операции из
группы 4. Имена файлов **не** меняются: `discover_tool_files` обходит
`*/tools/**/*.py` и имя файла в контракт не входит.

- [ ] 2.1 `capabilities/audit/tools/` (3): `list_scripts` → `audit.list_scripts`,
  `run_script` → `audit.run_script`, `generate_sql` → `audit.generate_sql`
- [ ] 2.2 `capabilities/vectors/tools/` (3): `vector_search`,
  `list_indexes`, `index_stats` → с префиксом `vectors.`
- [ ] 2.3 `capabilities/llm/tools/` (2): `complete` → `llm.complete`,
  `embed` → `llm.embed`
- [ ] 2.4 `capabilities/legal_summarizer/tools/` (1): `query_operation` →
  `legal_summarizer.query_operation`. Это самое длинное имя набора: у модели
  `mcp_enterprise_legal_summarizer.query_operation` — 47 символов из 64,
  запас 17. В бюджет влезает, но проверить надо расчётом по всем 34 именам
  (следующие: `data.delete_assistant_message` и `data.append_assistant_message`
  по 44, `data.patch_message_metadata` и `data.cleanup_session_mirror` по 42;
  за бюджет не выходит ни одно), а не «на глаз» по самому длинному префиксу
- [ ] 2.5 `capabilities/data/tools/` (23): префикс `data.`, по списку имён,
  объявленных в `ToolDefinition(name=...)`
- [ ] 2.6 Переезд делать **одним изменением набора**: наполовину переехавший
  реестр даёт модели два одинаковых имени (`data.generate_sql` и
  `generate_sql` → `mcp_enterprise_data_generate_sql`), и один инструмент молча
  вытеснит другой. Проверка 1.5 это отвергнет, но чинить придётся откатом
- [ ] 2.7 Обновить `loader.build_server` (`loader.py:294-303`): имя публикуется в
  `list_tools` как есть, и менять здесь нечего — это подтверждает, что точка
  законна на проводе. Проверить пробой, а не комментием

## 3. Словарь: `category` → `capability`

Группа **отключаемая**: инварианты 1–2 формулируются к полю и работают с любым
его названием. Отдельная она потому, что трогает 32 файла объявлений плюс
словарь в коде, а соседний change откладывал именно массовые переименования
(«закрепляется по одному пункту и только с доказанным дефектом»). Дефект здесь
назван: две стороны одного сравнения называются разными словами, и сообщение
об отказе вынуждено пояснять, что «категория» и «capability» — одно и то же.

- [ ] 3.1 Переименовать поле в `ToolDefinition` и во всех точках чтения:
  `registry.register`, `by_category` (переименовать в `by_capability`),
  `to_summary` (ключ `category` → `capability`), `loader.load_definition`,
  `execution/pipeline.py:149`
- [ ] 3.2 Обновить 32 объявления `category=` → `capability=`
- [ ] 3.3 Проверить, что `_json_type` и прочая рефлексия не ходят по `dataclasses`
  с именем поля в строку: иначе переименование тихо сломает сборку схемы
- [ ] 3.4 Строки в `tests/test_legal_summarizer_capability.py:163`
  (`definition.category == "legal_summarizer"`) и подобные — правятся здесь же

## 4. Платформенные операции: `platform.*`

- [ ] 4.1 `servers/enterprise/tools/read_result.py`: `name="read_result"` →
  `name="platform.read_result"`, `category="session"` → `capability="platform"`
- [ ] 4.2 `servers/enterprise/tools/session_files.py`: то же. Префикс `session`
  дал бы `session.session_files`, поэтому значение меняется, а не переиспользуется
- [ ] 4.3 Закрыть ловушку `_capability_from_path` (`loader.py:101-108`): функция
  возвращает `enterprise` для файлов `servers/enterprise/tools/*.py` вместо `""`,
  как обещает докстринг. Либо ограничить поиск каталога корнем `capabilities/`,
  либо явно отвергать такой путь. Проверено пробой: `read_result.py` и
  `session_files.py` дают `enterprise`
- [ ] 4.4 НЕ добавлять `platform` в `_ALL_CAPABILITIES` (`server.py:411-413`):
  платформенные операции регистрируются по наличию `execution.artifacts` /
  `execution.workspace`, а не по фильтру `--capabilities`, и значение из
  объявления не участвует в отборе
- [ ] 4.5 Зафиксировать следствие: журнал для этих двух операций начнёт писать
  `platform` вместо `session`. Выборка «по capability» за прошлый период и за
  текущий несопоставимы по этому значению — это объявляется в тексте переезда, а
  не остаётся на заметку
- [ ] 4.6 Файлы НЕ переносить в `capabilities/platform/tools/`: контейнер не
  отдаёт `artifacts` и `workspace` capability-инструментам, а перенос дал бы
  загрузчику второй путь к файлам сессии — запрещено
  `mcp-platform/tests/test_tool_execution_boundaries.py`

## 5. Объявления вне платформы

Здесь переезд **ломает поведение молча**, поэтому список полный.

- [ ] 5.1 `config.json → tools.mcpServers.enterprise.enabled_tools` (8 записей,
  `:870-879`): `list_scripts` → `audit.list_scripts` и т. д. Нанобот принимает и
  имя с провода, и обёрнутое (`mcp.py:1147-1152`), поэтому годятся имена с
  namespace. Запись, которой нет в реестре, даёт `WARNING` в журнале агента
  (`mcp.py:1170-1180`) и тихую потерю инструмента у модели: сводка capability на
  старте остаётся зелёной
- [ ] 5.2 `gateway.py:361-365` — тройки проб capability:
  `("vectors", "list_indexes", …)`, `("data", "schema_check", …)`,
  `("audit", "list_scripts", …)`. Здесь последствие **тихое**: по `schema_check`
  читается список таблиц для сверки профиля, а `platform_tables is None`
  обрабатывается словами «уже показано строкой выше, добивать второй ошибкой
  незачем» (`gateway.py:423-425`) — сверка просто перестанет выполняться
- [ ] 5.3 `gateway.py:377-385` — сравнения `operation == "schema_check"`,
  `"list_indexes"`, `"list_scripts"` в разборе ответа пробы: те же новые имена
- [ ] 5.4 `workspace/skills/audit_analyzer/SKILL.md` — таблица операций
  (`:16-22`), порядок выбора (`:27-36`), раздел про индексы (`:38-43`, `:50-55`),
  крупный результат (`:61-66`), запрет выдумывать имена (`:86-87`)
- [ ] 5.5 `workspace/skills/enterprise_mcp/SKILL.md` — пример `_execution`
  (`:44-48`) и чтение результата через `read_result` (`:62-66`)
- [ ] 5.6 `mcp-platform/docs/MCP-CONTRACTS.md` — пример вызова (`:60-64`) и
  пример `_execution` в ответе (`:263`)
- [ ] 5.7 `mcp-platform/docs/TARGET-ARCHITECTURE.md:294-298`,
  `mcp-platform/README.md:195-199`, `mcp-platform/docs/architecture.html:331-335` —
  три копии одного примера объявления
- [ ] 5.8 Обновить `_about`-пояснение в `config.json → tools.mcpServers.enterprise`
  о `tool_timeout`, если в нём упоминаются операции по имени

## 6. Стражи

- [ ] 6.1 Новый `mcp-platform/tests/test_tool_namespace.py`:
  - форма имени принимается / отвергается (нет точки, лишняя точка, пустая
    операция, пустое имя) — **все пять случаев**, а не только успешный: страж,
    проверяющий один путь, проходит ровно там, где проверки нет;
  - префикс не совпадает с полем → отказ, сообщение называет обе стороны;
  - capability не восстанавливается из имени: объявление с пустым полем и
    сходящимся префиксом SHALL давать отказ «не сказано»;
  - платформенная операция с `capability="platform"` регистрируется, с
    незнакомым значением — отвергается;
  - `read_result.py` и `session_files.py` проходят регистрацию (регрессия на
    ловушку `enterprise`);
  - различимость после проекции: `data.generate_sql` и `data_generate_sql`
    отвергаются, сообщение называет оба имени
- [ ] 6.2 В существующих стражях заменить плоские имена на новые — по факту
  совпадений: `test_audit_capability.py` (19), `test_server_bootstrap.py` (19),
  `test_llm_capability.py` (8), `test_legal_summarizer_capability.py` (7),
  `test_session_files_operation.py` (7), `test_read_result_operation.py` (5),
  `test_operation_schema_permissiveness.py` (4),
  `test_vectors_embedder_wiring.py` (2), `test_data_log_events.py` (2),
  `test_audit_values_and_serialization.py` (1), `test_enterprise_client_identity.py`,
  `test_llm_service.py`, `test_session_mirror_operations.py`,
  `test_session_workspace.py`, `test_tool_execution_pipeline.py`,
  `test_vector_builder.py`
- [ ] 6.3 **Не трогать** три файла, где то же слово означает другое:
  `test_db_job_classes.py:564` (`claim_task` / `claim_tasks` — методы
  `libs/enterprise_data/db.py`), `test_journal_fabricated_event_names.py:106`
  (`log_event` / `log_events` / `accept` — точки входа журнала),
  `tests/legal_summarizer/architecture/test_document_cache_boundaries.py:120`
  (`read_result` — метод кэша документов). Переименование там не по границе
  операций MCP и сломает несвязанные проверки
- [ ] 6.4 В `tests/test_mcp_platform_declaration.py` (он уже читает
  `tools.mcpServers.enterprise`) добавить проверку бюджета имени для модели:
  `len("mcp_" + "enterprise" + "_" + имя) <= 64` для каждой записи белого списка.
  Бюджет объявлен там, где известно имя MCP-сервера; вычислять его в платформе
  нельзя — это сделало бы имя сервера вторым владельцем
- [ ] 6.5 В `tests/test_mcp_platform_declaration.py` — проверку, что каждая
  запись `enabled_tools` совпадает с именем на проводе. Сегодня несогласованная
  запись видна только `WARNING` в журнале нанобота, а на стороне платформы ничем
- [ ] 6.6 `tests/test_audit_analyzer_skill_doc.py` (11 совпадений) — страж
  проверяет, что навык не обещает несуществующих операций; после переезда он
  обязан сверять с **новыми** именами, иначе перестанет ловить опечатку в
  объявлении
- [ ] 6.7 Проверить остальные совпадения по именам операций на стороне агента и
  не править выборочно: `test_diagnose_startup.py`,
  `test_enterprise_mcp_client.py`, `test_enterprise_mcp_http_transport.py`,
  `test_enterprise_mcp_identity.py`, `test_enterprise_mcp_meta_interception.py`,
  `test_gateway_enterprise_mcp_startup.py`, `test_mcp_identity_hook.py`,
  `test_mcp_operations_live.py`, `test_project_settings.py`,
  `test_repeat_guard_hook.py`, `test_runtime_health.py`,
  `test_runtime_inventory.py`

## 7. Координация

- [ ] 7.1 Формулировка дельты `2026-10-05-call-boundary-invariants` говорит
  «категория». После группы 3 это станет неточью. Правится **в его задачах** при
  архивации; здесь его файлы не трогаются
- [ ] 7.2 Требование umbrella-change `enterprise-mcp-platform` к реестру
  («поля `category`, `version`, `enabled`, `tags`, `permissions` допустимы, но на
  загрузку не влиять», `specs/runtime/tool-registry/spec.md:18-20`) после этой
  дельты описывает уже неверное состояние: на загрузку влияют и категория, и
  имя. Задача 5.1 соседнего change готовит `MODIFIED` по категории — дополнять
  его надо именем, иначе перечеркнутое требование переедет в канон как есть
- [ ] 7.3 `AGENTS.md` — запись про `lib/services/enterprise_mcp_client.py`
  перечисляет операции платформы по именам; проверить и переписать

## Граница переезда

- **Одно изменение, а не серия.** Имена на проводе, белый список модели, пробы на
  старте и навыки обязаны поменяться вместе. Переименование только в реестре
  оставляет модель без инструментов, а переименование только в навыках — с
  инструкцией, которой никто не может выполнить.
- **Живая проверка — только целиком.** `tests/test_mcp_operations_live.py` зовёт
  операции по имени против поднятой платформы; до и после переезда он зовёт
  разные наборы, и «красный» посередине ничего не значит. Гонять пофайлово, не
  выключая остальные строки.
- **Данные не переезжают.** Старые строки журнала остаются с плоскими именами, и
  `history_search` вернёт оба вида. Это следствие, а не дефект; миграции базы нет
  и не требуется — имя хранится значением.
- **`read_result` и `session_files` меняют значение capability** (`session` →
  `platform`). Это единственное изменение содержимого журнала в этом change, и
  оно объявлено в 4.5, а не выполнено молча.
