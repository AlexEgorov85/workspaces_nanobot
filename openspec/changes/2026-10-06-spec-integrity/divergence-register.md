# Реестр расхождений: код против спек

## Как читать

**Severity — это последствие для читателя, а не утверждение о чьей правоте.**
Решение по каждому пункту принимает владелец отдельно; ни один вариант здесь не
выбран. Формат записи: факт документа → факт кода → последствие → варианты →
чего не хватает для решения.

**Происхождение чисел этого документа.** Часть из них получена тремя независимыми
пересчётами 2026-10-06 (устаревшие ссылки, покрытие и реестр, структурные таблицы),
каждый — с нуля, с явным правилом, без обращения к предыдущему отчёту. Где
пересчёт разошёлся с исходными числами, приведены **пересчитанные**, а исходные
помечены как непригодные; где число невоспроизводимо вовсе — так и написано.
Непроверенных чисел в этом документе не осталось.

**Ложные срабатывания, разобранные и исключённые** — их не меньше, чем находок,
и без этого счёта числа выше не стоили бы ничего:

- `mcp-platform/.../data/tools/log_events.py` — сокращённая запись, файл существует
  по полному пути; не устаревшая ссылка, но и не разрешимая как путь;
- `workspace/hooks/unknown_hook.py` (`runtime/runtime-patcher:193,203`) —
  контрфактический пример в сценарии «WHEN `unknown_hook.py` существует»;
- `tool.pytest.ini` — артефакт регулярки, реальный путь `pyproject.toml`;
- `github/workflows/ci.yml` → `.github/workflows/ci.yml`;
- `profiles/prod.jsonc` — сценарий условный, прод-оверлей агента действительно не
  объявляется;
- все `nanobot/*` и `mcp/server/lowlevel/server.py` — библиотечные пути в
  `.venv/Lib/site-packages`, валидны;
- файлы, существующие только в `.worktrees/fork-f328176d27cd`, **не засчитаны**:
  ветка жива, но не является критерием.

---

## A. Устаревшие ссылки внутри спек

### A1. Описывают удалённый код как существующий — 11 путей (критично)

Пересчёт: исходный отчёт давал 14. Три из четырнадцати — спеки, которые **сами
фиксируют удаление**, а не вводят в заблуждение; они вынесены в A1a.

| Путь | Где встретился | Позиция в тексте |
|---|---|---|
| `lib/services/cache_provider.py` | `data/cache-provider` `:55`, `:331`, `:392` | нормативная (Implementation) |
| `lib/services/duckdb_cache_store.py` | `data/cache-provider` `:333`, `:394` | нормативная |
| `lib/services/pg_duckdb_sync_service.py` | `data/cache-provider` `:334`, `:395` | нормативная |
| `lib/services/table_registry.py` | `data/cache-provider` `:336`, `:400` | нормативная |
| `lib/core/infra_registration.py` | `data/cache-provider` `:401` | нормативная (`register_vector_storage`) |
| `lib/services/cache_provider_impl.py` | `data/cache-provider` | Implementation |
| `lib/services/vector_index_service.py` | `data/cache-provider`, `data/vector-indexes` | нормативная |
| `lib/services/preload_service.py` | `data/vector-indexes` `:176` | нормативная (определения класса `PreloadService` в коде нет; имя встречается в тексте — спеках, документах, CHANGELOG) |
| `lib/commands/compact_command.py` | `upgrade-compatibility` `:57` | упоминание (каталога `lib/commands/` нет вовсе) |
| `lib/services/subprocess_manager.py` | `runtime/entrypoints` | упоминание |
| `tools/build_vectors.py` | `configuration/profiles`, `data/cache-provider`, `data/vector-indexes`, `logging-db`, `runtime/context` — **5 спек** | нормативная в `vector-indexes`, упоминания в остальных |
| `workspace/tools/history_search_tool.py` | `logging-db` `:1550,1948`, `upgrade-compatibility` `:94` | упоминание (код уехал в `mcp-platform/servers/enterprise/capabilities/data/tools/history_search.py`) |
| `tools/recover_stale_sessions.py` | `storage/session-recovery` `:23-26` | **спека сама признаёт отсутствие** |
| `openspec/specs/runtime/call-contract/spec.md` | `skills/legal-summarizer-query` `:16-19` | нормативная ссылка на спеку |

**Факт кода.** Все восемь агентских файлов отсутствуют и на диске, и в git-индексе
(`git ls-files` пуст). Живая замена объявлена и работает:
`mcp-platform/libs/enterprise_data/snapshot/store.py:301` (`DuckDbSnapshotStore`),
`contracts.py:195` (`CacheProvider`), `libs/vectors/builder.py:239`
(`VectorBuilder`), `libs/vectors/owner.py:96` (`VectorIndexOwner`),
`servers/enterprise/build_index.py:185,222`.

**Последствие.** Единственный случай, где устаревшие ссылки занимают нормативные
строки, а не упоминания, — это `data/cache-provider`; её `## Scope` указывает на
живой `DuckDbSnapshotStore`, тело — на снесённый кластер. Спека проходит валидатор
как образцовая (14/17).

**Варианты.** (1) Переписать Implementation/Forbidden под платформенную реализацию.
(2) Свести спеку к дельте, историю перенести в `docs/`. (3) Оставить с явной
пометкой «Implementation описывает снятую подсистему — читать Scope».

**Чего не хватает.** `openspec/changes/2026-10-04-close-cache-provider-canon-gap`
не прочитан: есть спека-дельта, но она не слита. Если это незакрытая попытка закрыть
разрыв, то A1 — не расхождение, а открытая задача.

### A1a. Спека говорит правду о снесённом — 3 (не дефект)

Эти три исходный отчёт считал дефектами; пересчёт показал обратное. Править их не
надо, но и «исправления» ожидать нельзя — упоминание остаётся осознанным.

| Путь | Где | Что на самом деле написано |
|---|---|---|
| `lib/commands/compact_command.py` | `upgrade-compatibility:57` | «MUST ДОЛЖЕН быть удалён» — каталога `lib/commands/` нет вовсе |
| `lib/services/subprocess_manager.py` | `runtime/entrypoints:351` | спека прямо пишет «уже удалены, на диске их нет» |
| `tools/recover_stale_sessions.py` | `storage/session-recovery:26,33,35,127` | спека признаёт отсутствие (четыре упоминания) |

**Ещё одна поправка исходного отчёта: четыре усечённых пути он пропустил.** Из них
три — живые файлы, записанные без префикса, и потому не битые ссылки, а сокращение:
`bus/events.py` → `nanobot/bus/events.py` в `.venv` (4 раза, `error-fallback:67,77-79`);
`data/service/main.py` и `data/service/writer.py` →
`mcp-platform/servers/enterprise/capabilities/data/service/` (3 раза,
`db-queue-classes:91,254,319`). Четвёртый, `skills/audit_analyzer/scripts/cli.py`
(`logging-db:571`), стоит внутри строки примера `tools.exec("python …")` и
действительно нигде не существует — то есть это не ссылка, а содержимое примера.

### A2. Символ удалён, но упомянут как живой — 3

| Спека | Утверждение | Факт |
|---|---|---|
| `logging-db` `:245` | «проверяется `TestEventTimeThroughRealBuffering`» | класса нет; на `tests/test_turn_observability_events.py:111` осталась надгробная надпись. Реальный класс — `TestEventTimeThroughMcpTransport` (`:151`) |
| `logging-db` `:502-503` | producers `PgDuckDbSyncService._log_sync_event`, `PreloadService._emit_health_event` | оба класса удалены; остались только упоминания в докстрингах `lib/services/db_logging_service.py:211,1468` |
| `runtime/error-fallback` `:15` | `runtime_patcher.py:_wrap_fail` | символа нет, 0 совпадений по репозиторию; фактический текст отказа — `lib/core/project_settings.py:117` |

**Последствие.** `logging-db` — самая читаемая спека журнала, и её Verification
отправляет в класс, которого нет. Ошибка выглядит как «тест удалён», а не как
«спека устарела», и поэтому чинится не там, где надо.

### A3. Запрет охраняет несуществующий объект

`data/cache-provider:319` (`## Forbidden Behavior`): «вводить второе хранилище кэша
помимо `cache_provider.py`». Файла `cache_provider.py` в проекте нет — запрет
нечего соблюдать, и он читается как живое правило.

**Последствие.** Хуже отсутствия запрета: он занимает место живого правила и
снимает с автора необходимость написать запрет на существующий объект.

### A4. Тесты переехали в `mcp-platform/tests/` — 4

| Путь в спеке | Реальное место | Спека |
|---|---|---|
| `tests/test_architecture_boundaries.py` | `mcp-platform/tests/` | `runtime/db-queue-classes` |
| `tests/test_db_job_classes.py` | `mcp-platform/tests/` | `runtime/db-queue-classes` |
| `tests/test_pool_settings_seam.py` | `mcp-platform/tests/` | `runtime/db-queue-classes` |
| `tests/test_live_stdio_contract.py` | `mcp-platform/tests/` | `testing/unified-test-contract` |

**Последствие.** Мягче A1: код жив, устарел префикс пути. Но `Verification`
перестаёт работать как инструкция «запусти это».

### A5. Тестов нет вовсе — 5

`tests/test_duckdb_cache_store.py`, `tests/test_pg_duckdb_sync_service.py`
(`data/cache-provider`), `tests/test_event_log.py` (`logging-db`),
`tests/test_history_search_tool.py` (`upgrade-compatibility`),
`tests/test_history_search_benchmark.py` (`testing/unified-test-contract`).
Поиск по всему репозиторию — «NOT FOUND ANYWHERE».

---

## B. Межспековые расхождения (9; 6 доведены до кода)

### B1. Имя события сжатия: `context_compacted` против `agent.compacted` — высокая

- `logging-db:596-598` — «SHALL записывать событие `context_compacted` в
  `agent_gateway_logs` через `DbLoggingService.log_event`»;
- `runtime/entrypoints:344` — «`agent_gateway_logs` MUST получить
  `event_type="agent.compacted"`»;
- **код:** `lib/services/context_compaction.py:377,396` пишет `agent.compacted`;
  словарь платформы объявляет `AGENT_COMPACTED = "agent.compacted"`
  (`mcp-platform/libs/enterprise_common/eventing/types.py:32`, в `EVENT_TYPES` `:94`).
  Литерал `context_compacted` в модуле есть только как часть идентификатора
  (`context_compaction.py:480`).

**Последствие.** Событие читается агентом через `history_search`. Имя из
`logging-db` не существует в словаре и не пишется в таблицу, поэтому требование
«агент найдёт факт сжатия» выполнимо только по имени из `entrypoints`.
`logging-db` противоречит себе же: `:1454` допускает имя «вне словаря», тогда как
`tools-history-search:360-362` требует, чтобы опечатка в последнем компоненте
отвергалась проверкой словаря.

**Варианты.** (1) Править `logging-db`. (2) Править код, вернуть `context_compacted`.
(3) Зафиксировать таблицу соответствия имён в `logging-db` — прецедент есть,
`logging-db:1878` уже так делает.

### B2. Путь записи авто-сжатия: несуществующие обёртки — высокая

- `logging-db:617-619` — «WHEN `runtime_patcher._wrap_auto_compact_archive` или
  `_wrap_maybe_consolidate_by_tokens` вызвал `record_external_compaction(...)`»;
- `runtime/context:204` — «`postgres_channel` MUST распознать
  `isinstance(msg.event, ContextCompactionEvent)` и вызвать `_notify(...)`»;
- **код:** в `lib/services/runtime_patcher.py` нет ни одного упоминания
  `compact`/`consolidat`, объявлено 4 `PatchSpec`; `_wrap_auto_compact_archive` не
  встречается в репозитории вне `openspec/`; `record_external_compaction`
  (`context_compaction.py:404`) не имеет вызывающих в `lib/` и `workspace/`.
  Фактический путь: `lib/channels/postgres_channel.py:1415-1419` →
  `CompactionEventSubscriber.feed` → `notify_session_compacted`
  (`compaction_event_subscriber.py:90`) → запись (`context_compaction.py:483`).

**Последствие.** Оба сценария описывают путь записи, которого нет. Читатель,
ищущий, где фиксируется факт авто-сжатия, приходит к несуществующим функциям.

**Варианты.** (1) Переписать сценарий `logging-db` на
`CompactionEventSubscriber → notify_session_compacted`. (2) Переписать
`runtime/context:204` на подписчика вместо приватного `_notify`. (3) Пометить
сценарий историческим.

### B3. Имя операции: `legal_summarizer.query_operation` против `platform.query_operation` — высокая

- `skills/legal-summarizer-query:13,29-31` — «модель получает операцию
  `mcp_enterprise_legal_summarizer_query_operation`», «объявлена в
  `config.json → tools.mcpServers.enterprise.enabled_tools`»;
- `data/operation-schema:47-48,62-64` — «платформенная операция — объявленная вне
  `capabilities/<capability>/tools/`. Это свойство пути к файлу»;
- **код:** в `enabled_tools` девять имён, включая `platform.query_operation` и **не
  включая** `legal_summarizer.query_operation`. Объявление —
  `mcp-platform/servers/enterprise/tools/query_operation.py:232-236`
  (`capability="platform"`); докстринг `:3` — «Перенос из capability».
  Каталог `capabilities/legal_summarizer/tools/` содержит один `__init__.py`.
  Проекция имени для модели — `nanobot/agent/tools/mcp.py:609` + `_sanitize_name`
  (`:176-178`), то есть фактическое имя — `mcp_enterprise_platform_query_operation`.

**Последствие.** Обе строки спеки дают модели несуществующее имя — это обещание в
skill-контракте, а не описание.

**Варианты.** (1) Переписать имя и `## Scope` в `skills/legal-summarizer-query`.
(2) Пометить «домен остался, операция переехала». (3) Проверить change
`2026-10-05-legal-summarizer-session-scope`, которая описывает именно этот переезд —
если переезд намеренный, то спека просто не обновлена.

### B4. Ссылка на несуществующую соседнюю спеку — средняя

`skills/legal-summarizer-query:16-19` — «общий контракт вызовов описан в
`workspace/skills/enterprise_mcp/SKILL.md` и в
`openspec/specs/runtime/call-contract/spec.md`». Каталога `runtime/call-contract`
в спек нет; поиск `*call-contract*` по `openspec/` — 0 файлов. При этом документ
**существует как незакрытая дельта в трёх change**: `2026-10-03-mcp-native-tools`,
`2026-10-04-enterprise-mcp-http-transport`, `2026-10-05-legal-summarizer-session-scope`.

**Варианты.** (1) Слить одну дельту в `openspec/specs/runtime/call-contract/`.
(2) Переписать ссылку на активную дельту с пометкой «незакрыто». (3) Убрать ссылку,
оставив `SKILL.md`.

### B5. Имя метода записи: `log_event` против `try_log_event` — средняя

`logging-db:596-598` требует `DbLoggingService.log_event`, и следующая же строка
(`:600-601`) запрещает прямые `INSERT` из `ContextCompactionService`. Код
(`context_compaction.py:392-397`) зовёт `try_log_event`, который объявлен в
`lib/services/db_logging_service.py` как defensive-helper: переживает
`svc is None` и возвращает `bool`. При `None` код пишет WARNING и делает no-op;
спека этот исход не описывает, хотя `runtime/entrypoints:318-320` описывает его
верно.

**Варианты.** (1) Назвать `try_log_event` и описать no-op. (2) Оставить как есть с
пометкой.

### B6. Список обязательных разделов: 9 против 2+7 против 10 в коде — средняя

- `component-model:419-429` — 9 обязательных разделов, включая
  «Запрещённое поведение (**Negative Requirements**)`» и «Реализация
  (**Implementation Reference**)»;
- `component-spec-validation:17-30` — 2 базовых + 7 по маркеру, с именами
  `Forbidden Behavior` и `Implementation`;
- **код:** `BASE_SECTIONS` (`validate_component_specs.py:55-59`) = 3, включая
  `Scope`, которого **нет ни в одном из двух списков**;
  `FULL_TEMPLATE_SECTIONS` (`:72-80`) = 7, совпадают со вторым списком.

**Последствие.** Автор спеки получает от двух норм разные ответы на вопрос «обязан
ли я писать `## Boundary` в спеке без `## Responsibility`». Отдельно: `## Scope`
обязателен по коду и не упомянут ни одной нормой как базовый раздел.

### B7. Раскладка каталогов против фактических доменов — средняя

`component-model:306-358` перечисляет 15 доменов, включая `channels`, `sessions`,
`observability`, `interfaces`, `security`. На диске 13 доменов, и **все пять
перечисленных отсутствуют**. Отсутствуют в раскладке, но есть на диске:
`logging-db`, `storage`, `tools-history-search`, `upgrade-compatibility`,
`documentation`, `validation`.

**Отдельно:** 6 спек лежат в доменах, которых нет в закрытом списке категорий
(`component-registry:70-85`), при том что правило гласит «неизвестная категория
ДОЛЖНА считаться ошибкой реестра» (`:87-91`): `storage` (3 спеки), `logging-db`,
`tools-history-search`, `upgrade-compatibility`.

**Важно для решения.** Блок `:306-358` **не помечен SHALL/MUST**, в отличие от
соседних требований. При буквальном прочтении это описание, а не норма — и тогда
расхождение меняет тип с «противоречие» на «описательное». Это решение владельца,
и оно меняет severity.

### B8. Поля реестра: 5 полей против 4-колоночного носителя — низкая

`component-model:370` требует 5 колонок, `component-registry:17-21` перечисляет
5 полей. `COMPONENTS.md:32` строит таблицы в **4** колонки, категория выражена
заголовком секции (`### Architecture`, `:30`) и строкой статистики (`:18-26`).
`COMPONENT_ROW_RE` (`:114-120`) разбирает 4 поля.

### B9. Внутреннее противоречие `cache-provider`: Scope против тела

`cache-provider:9-10` (`## Scope`) объявляет: «подсистема целиком уехала из агента
в capability `data`… Реализация: `mcp-platform/libs/enterprise_data/snapshot/store.py`» —
это соответствует коду. `:331-336` (Implementation) перечисляет агентские файлы —
это не соответствует. Одна спека, два разных ответа на вопрос «чья это подсистема».

### Проверено и расхождения НЕТ (4 из 7 первоначальных подсказок)

- **Владение `## Scope`.** Все три названные спеки объявляют владельца, и все три —
  `platform`. Прямого противоречия нет; проблема внутри спек вынесена в A1/B9.
- **Проекция имён операций.** `tools-history-search:12,26`,
  `legal-summarizer-query:29-31` и `operation-schema:34-36` согласованы между собой и
  с кодом относительно механики проекции. Спор был не о проекции, а о том, какое
  имя на проводе объявлено — это B3.
- **Видимость `operator-console` против `logging-db`.** `operator-console:255-258`
  требует один ключ `gateway.console_level`; в `config.json` он есть
  (`"turn"`), четырёх булевых флагов нет. Код читает уровень через
  `console_level_of` (`lib/utils/logging_utils.py:252-260`). В `logging-db` слов
  `console_level`, `CONSOLE_LEVEL`, `print_` — **ноль совпадений**: спек, который
  мог бы противоречить, молчит.
- **Владение фактом сжатия.** Владелец единственный — `ContextCompactionService`,
  и это подтверждается независимо из `storage/session-hybridization:535-543`.
  Двойного описания владения нет; расхождения касаются имени события и механики
  доставки (B1, B2), а не владения.

---

## C. Расхождения реестра (проверены все 12 записей)

| Компонент | Расхождение | Severity |
|---|---|---|
| `Profiles` (`:49`) | указывает `config.json::profiles`; **ключа в `config.json` нет** (верхний уровень: `agents, channels, transcription, providers, api, gateway, tools, modelPresets`). Реально: `profiles/test.jsonc` + `config.py:255` `_PROFILES_DIR`, `config.py:292` `_SUPPORTED_PROFILES` | средняя |
| `ComponentRegistry` (`:70`) | объявляет реализацию «N/A (мета-спецификация)», тогда как своя же спека (`:9`) объявляет реализацией `tools/validate_component_specs.py` — 10 КБ кода, `main()` на `:416` | средняя |
| `ComponentSpecValidation` (`:76`) | то же: «N/A (правила валидации)» против кода, объявленного в `validation/component-spec-validation:9` | средняя |
| `OperationSchema` (`:64`) | заявлен `complete`. По пересчёту чек-лист «честных статусов» (`component-registry:41-47`) нарушен по **4 из 5**, а не 5 из 5: пункт «есть требование со сценарием» выполнен (3 требования, 12 сценариев, 24 маркера WHEN/THEN). Отсутствующих разделов **7 из 7**, а не 5 — нет `Responsibility`, `Boundary`, `Public Contract`, `Forbidden Behavior`, `Dependencies`, `Implementation`, `Verification`; в спеке есть только `Purpose`, `Scope`, `Requirements`. Отдельно: пункт «spec соответствует коду» нарушен — спека утверждает «сегодня `_json_type()` выбрасывает `NoneTypeType`», а `registry.py:306-355` публикует `["array","null"]` и бросает `ToolLoadError`; «загрузчик перезаписывает объявление безусловно, `loader.py:141-146`, `:165`» — эти строки содержат `_capability_name` и докстринг, а `loader.py:246-249` уважает объявленную схему. **Здесь канон отстал от кода в противоположную сторону** | высокая |
| `SkillToolBoundary` (`:35`) | **расхождение №5, не заявленное ранее.** Реализация «N/A (архитектурное правило)», тогда как спека объявляет реализацией `docs/skill-tool-architecture.md` и `docs/SKILL_AUTHORING.md` — **оба файла существуют** | средняя |
| Блок «Статистика» (`COMPONENTS.md:18-26`) | **расхождение №6, не заявленное ранее.** Объявляет `Missing = 0`, тогда как `component-model:382` требует, чтобы каждый production-компонент имел запись в реестре, а `missing` определён как «компонент есть в коде, спецификации нет». Таблица **занижает собственный пробел** | высокая |
| `CacheProvider` (`:61`) | строка реестра **верна** (символы проверены: `contracts.py:195`); не соответствует коду тело спеки — см. A1/B9 | высокая |
| `VectorIndexBuilder`/`VectorIndexOwner` (`:62-63`) | пути и символы верны (`builder.py:239`, `owner.py:96`); несоответствие в требованиях спеки — A1. Отдельно: **одна спека на два компонента** против правила «один компонент — одна спецификация» (`component-model:459`) | средняя |
| `ComponentModel`, `SkillToolBoundary` | статус `partial` не мотивирован: 8 требований / 7 сценариев и 5 / 7 при наличии объявленной реализации | низкая |

**Проверено и верно.** Блок «Статистика» сходится с таблицей до цифры
(12/1/9/2/0). Заметка о сносе `lib/services/cache_provider.py` и
`vector_index_service.py` **правдива** — оба файла отсутствуют и на диске, и в
индексе. Три записи `partial` у `ApplicationContext`, `RuntimePatcher`,
`StartupSchemaValidation` честны: пути и символы проверены
(`application_context.py:141,722,840`, `runtime_patcher.py:462`,
`schema_validation.py:203`).

**Пробелы реестра.** 28 спек против 11 различных, на которые ссылается реестр, —
**17 спек без записи**, и среди них `runtime/entrypoints` (два стартовых пути
системы). Категории `channels`, `sessions`, `observability`, `interfaces`,
`security` есть в закрытом списке и имеют живой код, но в реестре пусты. Блок
«План заполнения» помечен «Wave 1 (завершён)»; **Wave 2 отсутствует** — плана
закрытия 63 неописанных компонентов нет.

---

## D. Структура и вакуумность валидатора

### D1. Обход через отсутствие `## Responsibility` — высокая

Реализация: `FULL_TEMPLATE_MARKER = "Responsibility"` (`:87`), ветка `:227`.
Лазейкой пользуются **20 из 28** спек. Проба: спека с 3 из 7 разделов и без
`Responsibility` → **exit 0**.

**Поправка на 20, а не на 19 — и объяснение, почему первый счёт был неверен.**
Настоящих `## Responsibility` ровно **8**: `component-model`, `skill-tool-boundary`,
`profiles`, `cache-provider`, `vector-indexes`, `context`, `platform-settings`,
`startup-schema-validation`. Девятым в наивном подсчёте выходит
`architecture/component-model`, но там заголовок лежит **внутри fenced-блока с
шаблоном** (L71–L107) и вырезается `strip_fenced_code` (`:165-178`) — это пример
разметки, а не собственный раздел спеки. Отсюда важное следствие: **спека-шаблон
сама оказалась в короткой форме** и освобождена от проверки семи разделов. То есть
`TEMPLATE_SPEC` (`:84`) — мёртвая константа, а исключение шаблона не объявлено:
он проходит **побочным эффектом**, а не правилом.

Поэтому совпадение «есть `Responsibility` ⟺ есть `Implementation`» сильное, но
**не 9/9/9, а 8/8**: у тех же 8 спек есть обе, а `Verification` есть у 9 —
девятый `component-model` имеет настоящий `## Verification` на L478, без
настоящего `Responsibility`. Число «19 спек без `Verification`» верно и устойчиво
(28 − 9), но это другая величина: без `Verification` — 19, без `Responsibility` — 20.

### D2. Проверяются 10 заголовков из 19 — средняя

Не проверяются никогда: `Inputs`, `Outputs`, `State`, `Configuration`, `Lifecycle`,
`Владение данными`, `Error Behavior`, `Invariants`, `Consumers`. Проба: «полный
шаблон» из 10 разделов → exit 0. Русские формы (`Конфигурация`, `Жизненный цикл`,
`Состояние`, `Инварианты`, `Поведение при ошибке`, `Потребители`) встречаются по
одному разу и не имеют синонимов в коде.

### D3. Тела разделов не проверяются — высокая

Проба: все обязательные заголовки с **пустыми телами**, одно требование, один
сценарий, единственный маркер → **exit 0**. В дереве пустых тел сейчас нет, то
есть щель латентна и полностью доступна.

### D4. Один маркер на всю спеку вместо маркера в каждом сценарии — средняя

`:312` — `WHEN_THEN_RE.search(body)` один раз на тело `## Requirements`. Проба: два
сценария, маркер только во втором → exit 0. Регулярка принимает
`КОГДА|ТОГДА|ВЫБОР|ЕСЛИ|WHEN|THEN|IF|GIVEN` (четыре значения в требовании
отсутствуют) и требует ровно `**КОГДА**` — форма `**КОГДА:**` не подходит.

### D5. Таблица синонимов ≠ «Словарь терминов» — средняя

> **ЗАКРЫТО 2026-10-06 решением владельца, таблица снята.** Расхождение было
> реальным и лечилось не пополнением словаря, а отказом от синонимов: словарь —
> глоссарий терминов, а не таблица имён разделов. `SECTION_ALIASES` удалён,
> все 19 канонических имён переведены на английский (коммит `bc76dfa`).
> Запись сохранена как свидетельство о том, почему решение принято.

`SECTION_ALIASES` (`:90-104`) — ручной список из 12 заголовков. Проверено:
`Требование` → **отвергается**, `Контракт` → **отвергается**,
`Зависимость` → **отвергается**, хотя это легитимные по словарю
(`component-model:255-272`) формы. Обратно — в списке есть формы, которых в
словаре нет: `Назначение`, `Проверка`, `Реализация`, `Negative Requirements`. У
`Scope` синонимов нет вовсе.

### D6. Ссылка «Спецификация» проверяется на существование, но не на смысл

`resolved.is_file()` (`:379`) — любой файл в любом месте. Проба: запись со ссылкой
на сам `COMPONENTS.md` → exit 0.

### D7. Дубликат компонента маскируется невалидным статусом

Проверка статуса (`:335-342`) с `continue` выполняется **раньше** проверки
дубликата (`:344-346`): строка с невалидным статусом выпадает из `seen` целиком, и
её ссылка и реализация не проверяются. Проба: дубликат со статусом `WAT` в
второй строке → сообщение только про статус, про дубликат не сказано.

### D8. Несовпавшая строка реестра исчезает молча — высокая

`COMPONENT_ROW_RE` (`:114-120`) — жёсткие 4 группы; `finditer` (`:331`) молча
пропускает всё остальное, а счётчик (`:446`) этого не показывает. Три
воспроизведённых способа выключить всю реестровую проверку: дефис в имени, пробел в
имени, кириллическое имя, и — главное — **приведение реестра к канонической
5-колоночной форме**: пробы с 5 колонками, неизвестной категорией, битой ссылкой и
несуществующей реализацией дают **exit 0 и «Компонентов в реестре: 0»**. То есть
исправление реестра по норме **выключает** его проверку.

### D9. Категории не проверяются вовсе

Слов «категор/Категория/category» в валидаторе **ноль вхождений**. `SCOPE_VALUES`
(`:66`) — владелец **кода**, не домен. `iter_spec_files` (`:161`) берёт `**/spec.md`
любой глубины: спека в корне домена проходит (три такие есть на диске).

### D10. Паритет реестра и спек не проверяется

`registry` используется только в `len()` (`:446`). Обратного прохода «каждая
`spec.md` есть в реестре» нет — 28 против 12 не видны вообще.

### D11. Валидатор не запускается ни CI, ни тестами — высокая

`.github/workflows/ci.yml`: `ruff check lib workspace` (`:58`), `pytest tests/`
(`:64`), coverage (`:100`), contract (`:144`), платформа (`:201-217`). Ни один шаг
не вызывает валидатор. В `tests/` (165 файлов) нет ни одного ссылающегося на
скрипт. Guard-тестов **ноль**.

### D12. Мёртвая константа, из-за которой шаблон проходит случайно

`TEMPLATE_SPEC` объявлена на `:84` и **не используется нигде** (ровно одно
вхождение — само объявление). Спека-шаблон проходит проверку потому, что её
заголовки лежат внутри fenced-блока (`:71-107`), который вырезает
`strip_fenced_code` (`:165-178`, вызов `:208`).

### Итог аудита: «22 пробы, 11 проходят на заведомо плохой спеке»

**Статус: не воспроизведено.** Это пересказ отчёта одного из проходов аудита, а не
измеренный нами факт. Пересчёт 2026-10-06 независимым набором проб дал другое:
**24 пробы, валидатор слеп к 13**. Числа расходятся и по величине, и по значению:
здесь «проходит на дефекте» означает слепое место валидатора, в нашем наборе
красные пробы — это тоже слепые места, а зелёные — работающие проверки. Совпадение
слова «11» между двумя измерениями означает разные вещи и совпадением не является.

Перечень, приведённый ниже, сохранён как исходное утверждение отчёта; сверить его
по пунктам с нашим набором имеет смысл только после того, как набор проб будет
лежать в репозитории (задача 7.1), — до этого он невоспроизводим.

Зелёные на дефекте по отчёту аудита: пустые тела, тело сценария пустое, требование
без метки, обход семи разделов, шаблон из 10 разделов, весь текст на английском,
маркер только во втором сценарии, дубль раздела через синоним
(`## Purpose` + `## Назначение`), неоднозначный владелец в `Scope` (определился как
`platform` при тексте «частично в `platform`, частично в агенте»), спека в корне
домена, домен вне списка, спека без записи в реестре, имя реестра с
дефисом/пробелом/кириллицей, 5-колоночная таблица, ссылка на `COMPONENTS.md`.

**Подтверждено пересчётом 2026-10-06 (совпадает с отчётом):** обход семи разделов;
пустые тела разделов (проверено на четырёх разделах); маркер только во втором
сценарии; дубль раздела через синоним; неразобранная строка реестра;
5-колоночная таблица; ссылка на `COMPONENTS.md`; запись реестра вне категории.

**Добавлено пересчётом, в отчёте не значилось:** второй H1 в файле спецификации
(`logging-db`); дубль компонента, замаскированный невалидным статусом второй
строки реестра.

**Расхождение в числе строк:** в перечне 15 пунктов, из которых один («четыре
способа сломать разбор строки реестра») описывает четыре пробы, — итого 18, а не
22. Счёт, который не сходится внутри себя, — первый признак, что методика
подсчёта, а не одно число требует пересмотра.

Красные (проверки работают): нет `## Scope`, файл 0 байт, полный шаблон без
`## Boundary`, сценарий заголовком `#####`, дубликат при валидных статусах, битая
ссылка на `spec.md`, нет `COMPONENTS.md`, точный дубль `## Requirements`, нет
заголовков `###` вообще, `deprecated` + несуществующий файл, владелец без обратных
кавычек, нет файла реализации под `--strict`.

---

## E. Пробелы покрытия — числа пересчитаны, исходные не сходятся

**Числа «102 кандидата / 32 со спекой / 63 без / 41 в работе / 20 пробелов»
признаны непригодными.** Причина не только в гранулярности: **арифметика исходного
отчёта не сходится** — `32 + 63 = 95 ≠ 102` (семь кандидатов не учтены ни в одной
категории), `41 + 20 = 61 ≠ 63` (два не отнесены ни к «в работе», ни к «пробелам»).
Это самостоятельный дефект. Плюс правило гранулярности **никогда не было
объявлено**: отчёт смешивает модули для одних доменов, кластеры для других и
«одна операция = компонент» для третьих, поэтому число 102 невоспроизводимо.

Пересчёт по явно объявленному правилу — **модуль production-кода с публичным
top-level `class`/`def`, не `__init__.py`, не связка одной MCP-операции**
(`lib/`, `workspace/`, `tools/`, `gateway.py`, `cli_agent.py`, `config.py`,
`mcp-platform/libs/`, `mcp-platform/servers/`; исключены `tests/`, `.venv/`,
`_quarantine/`, `.worktrees/`, кэши): из 317 `.py` отсеяно 108 → **206 кандидатов**
(70 агент / 136 платформа). Покрытие считалось по **объявленной реализации спеки**
(`Implementation`/`Реализация`, иначе тело `## Scope`), а не по любому упоминанию:
наивный grep завышал покрытие примерно до 70.
Разделять эти два числа принципиально: без разделения «63 не опис��но» читается как
приговор коду, который находится в работе.

| Домен | Кандидатов | Спека | В работе | Пробел |
|---|---|---|---|---|
| **АГЕНТ** | **70** | **20** | **33** | **17** |
| platform/legal_summarizer | 72 | 0 | 21 | **51** |
| platform/common | 18 | 5 | 12 | 1 |
| platform/data | 11 | 2 | 6 | 3 |
| platform/vectors | 9 | 6 | 1 | 2 |
| platform/audit | 7 | 0 | 5 | 2 |
| platform/llm | 4 | 0 | 1 | 3 |
| **ПЛАТФОРМА** | **136** | **17** | **56** | **63** |
| **Всего** | **206** | **37** | **89** | **80** |

Подтверждено отдельно и независимо от гранулярности: **ровно две спеки** ссылаются
на реально удалённый код — `data/cache-provider` (все 8 путей отсутствуют) и
`storage/session-recovery` (`tools/recover_stale_sessions.py`). Остальные пять
«пропусков» оказались артефактами разбора относительных путей: `platform.json` →
`mcp-platform/platform.json`, `ci.yml` → `.github/workflows/ci.yml`,
`execution/factory.py` и `servers/enterprise/server.py` — относительно
`mcp-platform/`, `sql/*/create_public_*_test.sql` — пять файлов.

**Порядок доменов подтвердился, абсолютные числа — нет.** Крупнейший непокрытый
домен — `legal_summarizer` (72 модуля, 0 со спецификацией, 51 настоящий пробел;
ровно 9 подпакетов плюс `cli.py`/`cli_query.py` — те самые «10 подсистем»).
Наблюдаемость/журнал — 6 модулей. Три хука `workspace/hooks/` — ровно 3.

**Опровергнуто: «пять навыков агента».** Навыков **два** —
`workspace/skills/audit_analyzer` и `workspace/skills/enterprise_mcp`, плюс один
платформенный (`mcp-platform/libs/legal_summarizer/skill/SKILL.md`). Всего в
репозитории 3 файла `SKILL.md`, и в `config.json → gateway.agent.skills` объявлен
только `audit_analyzer`.

Верхние кандидаты с признаком компонента и упоминаниями вне спек:

| Компонент | Путь | Признак | Упоминается вне спек |
|---|---|---|---|
| `PostgresChannel` | `lib/channels/postgres_channel.py:93` | жизненный цикл сообщения, своя конфигурация, граница «канал↔агент» | docs 35, changes 27, archive 43 |
| `QueueOps` | `lib/channels/queue_ops.py:93` | контракт, состояние `_client`/`_worker_id`, доменная ошибка | changes 24 |
| `ChannelFactory` | `lib/services/channel_factory.py:27` | своя конфигурация, порядок сборки, производная от `console_level` | AGENTS **0**, docs 2 |
| `ConfigService` | `lib/services/config_service.py:26` | контракт загрузки конфигурации; **назван компонентом в `component-model:44`** | AGENTS 0, docs 4, changes 0 |
| `EnterpriseMcpClient` | `lib/services/enterprise_mcp_client.py:642` | долгоживущая сессия, `aclose`/`close`, личность вызова | docs 4, changes 16 |
| `AgentFactory` | `lib/core/agent_factory.py:59` | композиция `AgentLoop` со всеми хуками; **назван в `component-model:50`** | AGENTS 2, docs 8 |
| `SessionStorageService` | `lib/services/session_storage.py:112` | своё хранилище, своя ошибка | AGENTS 0, changes 3 |
| `TurnIdentityStore` | `lib/services/turn_identity.py:204` | единственный владелец идентичности оборота (`logging-db:3281`) | AGENTS 0, docs 0, архив 6 |
| `RuntimeHealth`/`RuntimeReadiness` | `lib/services/runtime_health.py:102,149` | свой жизненный цикл, реестр проверок | docs 4+4 |
| `McpHealthMonitor` | `lib/gateway/mcp_health.py:136` | собственный `asyncio.Task`, интервал и backoff | docs 0 |
| capability `audit` + `libs/audit/` | `mcp-platform/.../capabilities/audit/service/main.py` | свой конвейер, реестр скриптов, белый список таблиц | docs 3, changes 7 |
| `sql_safety` | `mcp-platform/libs/enterprise_data/sql_safety.py` | отдельная ответственность; категория `security` в списке есть, записей 0 | AGENTS 2, docs 10, changes 12 |
| домен `legal_summarizer` | `mcp-platform/libs/legal_summarizer/` (11 подсистем) | документный разбор, чанкинг, map-reduce, retrieval, кэш | AGENTS 10, **docs 48**, changes 141 |
| `enterprise_client/llm.py` | `mcp-platform/libs/enterprise_client/llm.py` | отдельный процесс-клиент для внешних вызывающих | docs 11, changes 10 |
| `eventing/writer.py` | `mcp-platform/libs/enterprise_common/eventing/writer.py` | собственный контракт записи; спекки используют его только как «источник типов» | AGENTS 0, changes 15 |

**Обратная дыра:** `storage/session-recovery` — спека есть (7 требований, реализовано
одно), кода нет; сама фиксирует отсутствие `tools/recover_stale_sessions.py`, а
change `2026-09-27-session-recovery` помечен выполненным при отсутствии кода.

---

## F. Структурные мелочи, не являющиеся расхождениями

- 26 спек используют `#### Scenario:`, две — русское `#### Сценарий:`
  (`runtime/agent-hooks` — 5, `runtime/db-queue-classes` — 20). Валидатор принимает
  оба (`SCENARIO_HEADING_RE`, `:110`). Формально не дефект; при ужесточении
  маркеров привести к одному виду обязательно, иначе новое требование закроет эти
  две спеки неожиданно для их авторов.
- `runtime/platform-settings:129` — `## Requirements` после `## Implementation` и
  `## Verification`. Порядок валидатор не проверяет.
- `data/cache-provider:115` — сценарий на `profiles/prod.jsonc`; файла нет,
  сценарий условный, severity низкий.
- `configuration/profiles:507` запрещает ветки `if profile == "prod"`. Литерал
  `profile ==` встречается в production-коде ровно один раз:
  `lib/services/schema_validation.py:58,60` в `_hint_for_profile()`, где выбирается
  **текст подсказки оператору**, а не поведение. Запрет по существу держится;
  автостража на него в `tests/` и `tools/` нет. Формулировку «запрет опровергнут
  кодом» здесь ставить нельзя.