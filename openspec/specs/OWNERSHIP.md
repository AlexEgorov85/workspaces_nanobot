# Владение спецификациями

Сводная раскладка: чьим кодом описана каждая спека. Нужна потому, что проект
разделён на два дерева — агента (`lib/`, `workspace/`, `gateway.py`) и платформу
`mcp-platform` (корпоративный MCP-сервер), — и половина работы состоит в переезде
подсистем между ними. Без этой таблицы вопрос «кто это чинит» не имеет ответа,
кроме как прочитать все 42 спеки.

Раздел `## Scope` в каждой спеке — источник истины, этот файл — его раскладка.
Значение берётся из первой метки в обратных кавычках тела `## Scope` и
проверяется `tools/validate_component_specs.py`: незнакомое значение — ошибка,
пропущенный раздел — тоже.

Быстрый ответ на вопрос «про что этот MCP?»:

```bash
grep -rl '`platform`' openspec/specs --include=spec.md
```

## `platform` — предмет реализован в `mcp-platform`

Три спеки описывают код, который уехал из агента целиком. Ни одна из них не
обновлялась под переезд — это отдельная работа (см. «Что дальше»).

| Спека | Уехало в | Наследник |
|---|---|---|
| `data/cache-provider/spec.md` | capability `data` | `mcp-platform/libs/enterprise_data/snapshot/store.py` (`DuckDbSnapshotStore`) |
| `data/vector-indexes/spec.md` | capability `vectors` | `mcp-platform/libs/vectors/`, объявления в `platform.json → vectors.indexes` |
| `skills/legal-summarizer-query/spec.md` | capability `legal_summarizer` | `mcp-platform/libs/legal_summarizer/`; в агенте не осталось ничего — обёртка `workspace/tools/legal_summarizer_query.py` снята (change `2026-10-03-mcp-native-tools`, п. D6), модель зовёт операцию `mcp_enterprise_legal_summarizer_query_operation` |

Спеки ниже не переезжали, а родились в платформе: предмета в агенте у них
не было и не стало.

| Спека | Где смотреть |
|---|---|
| `data/operation-schema/spec.md` | `mcp-platform/libs/enterprise_common/registry.py` (`build_input_schema`, `_json_type`, `validate_operation_name`, `ToolRegistry.register` — общая точка проверки имени), `libs/enterprise_common/loader.py` (выбор публикуемой схемы, `load_definition`, `load_registry`, `build_server`), `libs/enterprise_common/execution/pipeline.py` (capability в политике и журнале), `INPUT_SCHEMA`, имя и capability в `capabilities/*/tools/*.py`, платформенные операции в `servers/enterprise/tools/*.py`, второй сервер на общем реестре — `servers/_template/` |
| `data/audit/spec.md` | `mcp-platform/libs/audit/` (`registry_loader.py`, `generated_sql.py`, `predefined.py`, `guard.py`), `servers/enterprise/capabilities/audit/`; модель ходит операциями `mcp_enterprise_audit_*` |
| `data/vectors/spec.md` | `mcp-platform/libs/vectors/` (`owner.py`, `embedding.py`), сборка индекса — `servers/enterprise/build_index.py`, объявления — `platform.json → vectors.indexes` |
| `data/query/spec.md` | `servers/enterprise/capabilities/data/` (`service/main.py:DataService`, `tools/`, `guard.py`), пул и классы работы — `mcp-platform/libs/enterprise_data/db.py` |
| `data/duckdb-cache/spec.md` | `mcp-platform/libs/enterprise_data/snapshot/` (`store.py:DuckDbSnapshotStore`, `contracts.py`), корень файла — `platform.json` |
| `data/task-queue/spec.md` | `servers/enterprise/capabilities/data/tools/claim_task.py`, `service/main.py` (`claim_tasks`, `ClaimedBatch`), имя таблицы — `platform.json → data.task_table` |
| `runtime/tool-registry/spec.md` | `mcp-platform/libs/enterprise_common/registry.py:ToolRegistry`, `libs/enterprise_common/loader.py`, раскладка `capabilities/<имя>/tools/` |
| `runtime/tool-execution/spec.md` | `mcp-platform/libs/enterprise_common/execution/` (`pipeline.py`, `context.py`, `quality.py`, `errors.py`) |
| `runtime/db-queue-classes/spec.md` | `mcp-platform/libs/enterprise_data/db.py` (`_Worker`, `submit`, `submit_transaction`), `platform.json` (`pool`, `job_classes`) |

## `shared` — контракт между агентом и платформой

Здесь нормативные требования адресованы обеим сторонам: одна решает, другая
исполняет. При переезде такие спеки меняются, а не исчезают.

| Спека | Агент | Платформа |
|---|---|---|
| `configuration/profiles/spec.md` | `profiles/test.jsonc`, `config.json` | `platform.json → profiles.test`, `PROFILE_OWNED_KEYS` |
| `runtime/platform-settings/spec.md` | блок собирает `enterprise_mcp_client.py` | принимает `libs/enterprise_common/settings.py` (`owner=OWNER_AGENT`), разбирает `servers/enterprise/server.py` |
| `observability/logging-db/spec.md` | `lib/services/db_logging_service.py`, `log_transport.py` | операции `data.log_events`, `data.log_event`, `data.purge_logs` |
| `interfaces/tools-history-search/spec.md` | агентской обёртки не осталось: `workspace/tools/history_search_tool.py` снят (change `2026-10-03-mcp-native-tools`, п. D6), личность вызова подставляет `lib/hooks/mcp_identity_hook.py` | SQL и изоляция по `session_id`/`user_id` в `data.history_search` — берутся из контекста вызова, а не из аргументов модели |
| `runtime/session-files/spec.md` | `lib/services/session_files.py` (резолвер каталога сессии), `workspace/hooks/session_file_redirect_hook.py` (перенаправление записи в `files/`), `lib/utils/session_file_store.py` | `mcp-platform/servers/enterprise/tools/session_files.py` (`SessionHandle`), `libs/enterprise_common/session/workspace.py`, корень — `platform.json → execution.session_root` |
| `runtime/call-contract/spec.md` | `lib/hooks/mcp_identity_hook.py` (подстановка личности), `workspace/skills/enterprise_mcp/SKILL.md` (коды в словаре модели) | `mcp-platform/libs/enterprise_common/execution/errors.py` (`FAILURE_CODES`, `normalize_exception`, `failure_from_payload`), разбор конвейера — `execution/pipeline.py` |
| `testing/unified-test-contract/spec.md` | `tests/`, `pyproject.toml`, `.github/workflows/ci.yml` | `mcp-platform/tests/`, `mcp-platform/pyproject.toml` |
| `runtime/event-model/spec.md` | `lib/services/runtime_events_subscriber.py`, `workspace/hooks/`, `lib/core/agent_factory.py` (события оборота) | `mcp-platform/libs/enterprise_common/eventing/` (`types.py` — закрытый словарь, `models.py` — конверт, `writer.py`), таблица `agent_gateway_logs` |
| `runtime/call-timeout/spec.md` | `config.json → tools.mcpServers.enterprise.tool_timeout` (модельная нога) и `gateway.agent.enterprise_mcp.tool_timeout_sec` (фоновая), `lib/services/enterprise_mcp_client.py` | `mcp-platform/platform.json → execution.execution_timeout_sec`, конвейер `execution/pipeline.py`; страж знака зазора — `tests/test_mcp_platform_declaration.py` |

## `agent` — предмет реализован в агенте

| Спека | Где смотреть |
|---|---|
| `architecture/component-model/spec.md` | мета-шаблон самой спецификации |
| `architecture/skill-tool-boundary/spec.md` | `workspace/skills/`, `workspace/tools/`, `lib/services/project_tool_loader.py` |
| `documentation/component-registry/spec.md` | `COMPONENTS.md`, `tools/validate_component_specs.py` |
| `infrastructure/test-profile-tables/spec.md` | `sql/*/create_public_*_test.sql`, `tools/apply_test_profile_tables.py` |
| `runtime/agent-hooks/spec.md` | `lib/core/agent_factory.py`, `lib/hooks/` |
| `runtime/anti-loop/spec.md` | `lib/hooks/repeat_guard_hook.py` |
| `runtime/context/spec.md` | `lib/core/application_context.py` |
| `runtime/entrypoints/spec.md` | `gateway.py`, `cli_agent.py`, `lib/lifecycle/` |
| `runtime/error-fallback/spec.md` | `lib/services/turn_delivery_factory.py` |
| `runtime/operator-console/spec.md` | `lib/utils/logging_utils.py`, `lib/hooks/terminal_tool_print_hook.py`, `lib/channels/postgres_channel.py`, `gateway.py` (`gateway.console_level`) |
| `runtime/runtime-events-subscription/spec.md` | `lib/services/runtime_events_subscriber.py` |
| `runtime/runtime-patcher/spec.md` | `lib/services/runtime_patcher.py` |
| `runtime/startup-schema-validation/spec.md` | `lib/services/schema_validation.py` |
| `sessions/session-hybridization/spec.md` | `lib/session/pg_session_manager.py` |
| `observability/usage-store/spec.md` | `lib/core/agent_factory.py` |
| `infrastructure/upgrade-compatibility/spec.md` | `requirements.txt`, `tests/contract/` |
| `validation/component-spec-validation/spec.md` | `tools/validate_component_specs.py` |
| `runtime/queue-channel-switch/spec.md` | `lib/channels/queue_ops.py:QueueOps`, `lib/channels/postgres_channel.py` |
| `runtime/patch-to-hook/spec.md` | `lib/services/runtime_patcher.py` (`_PATCH_SPECS`, `PatchSpec`), `lib/services/runtime_inventory.py`, `docs/architecture/runtime-patcher-inventory.md` |

`COMPONENTS.md` — реестр компонентов, не спека: раздела `## Scope` в ней нет по
той же причине, по какой его нет у этого файла.

## Требует решения

- **`sessions/session-recovery/spec.md`** помечена `agent` по замыслу, но
  **реализации нет ни в одном дереве**. Поиск `SessionRecovery` /
  `session_recovery` по `lib/`, `workspace/`, `gateway.py`, `cli_agent.py`,
  `config.json`, `tools/` даёт ноль совпадений. Архивный change
  `2026-09-27-session-recovery` утверждает, что `SessionRecoveryService` создан в
  `ApplicationContext.create()`, — такого кода нет. Либо спека описывает
  нереализованное намерение, либо подсистема не была сделана. По OpenSpec это
  оформляется change-каталогом, а не правкой существующей спеки.
- **Три спеки `platform` устарели по содержанию.** Разметка фиксирует, чей это
  код, но не переписывает контракт под новое место. Особенно заметно на
  `skills/legal-summarizer-query`: спека описывает subprocess-IPC со
  `scripts/cli_query.py`, которого в агенте уже нет, а фактический вызов идёт
  через MCP-операцию `legal_summarizer.query_operation`.
- **Ни один из 11 активных change'ов не завершён** — 170 задач из 385, поэтому
  ни один не готов к архивированию по критерию OpenSpec (выполненные задачи,
  а не наличие текста).
  `tools/change_status.py` считает препятствием **другое**: дельта заводит
  несуществующую спеку; объявляет `ADDED` для требования, уже лежащего в
  каноне (архив добавил бы второй заголовок с тем же именем); объявляет
  `REMOVED` для живого требования; `MODIFIED` переписывает требование, чей
  текст — вместе со **сценариями** — разошёлся с каноном. Требование,
  совпадающее с каноном, — признак состояния, а не помеха.
  «Требуют решения: 0 из 12» после приведения дельт к канону означает не
  «всё сделано», а «архив больше не может испортить канон»: работа
  change'ов не сделана, и это видно по счётчикам задач, а не по отчёту.
- **Дельты разошлись с каноном у 8 change'ов из 12, и архив это повредил бы.**
  Восемь change'ов объявляли `ADDED` поверх живых требований, `REMOVED` для
  живых и `MODIFIED` с устаревшим текстом. Хуже всего был
  `unify-runtime-channels`: его `REMOVED` снёс бы из канона пять требований
  `runtime/entrypoints`, в том числе два про `role` — а код роль реализует
  (`lib/core/application_context.py:186`, `:427`) и охраняет
  `tests/test_application_context_role.py`. Дельты приведены к тексту канона,
  `REMOVED` живых требований сняты, работа change'ов осталась в `tasks.md`
  нетронутой: архив стал холостым по канону, но change'ы по-прежнему не
  выполнены.
- **`enterprise-mcp-platform` закрыт 2026-10-08** (143/143), архив добавил в
  `runtime/call-contract` девять требований об идентичности вызова, которых в
  каноне не было, хотя реализация давно на месте (`lib/hooks/mcp_identity_hook.py`,
  `lib/core/agent_factory.py:408`, страж `tests/test_service_identity.py`).
  Последней была задача 7.4: правило чистки журнала объявляет платформа
  (`data.log_retention_days`, `data.log_purge_empty_outbound`), а агент
  переопределял его значением, которого нет ни в одном конфиге — оно приходило
  дефолтом `90` из `application_context.py`. Теперь периодический purge зовёт
  `purge_logs` без аргументов и получает серверное решение; расписание
  `purge_interval_sec` осталось за агентом. Правка сдвинула строки и унесла
  девять ссылок канона — поправлены по содержанию, а не сдвигом номера:
  взята строка из HEAD и найдено её новое место.
- **Сценарий — часть требования, и об этом легко забыть.** Разбор, который
  сравнивал только прозу, объявлял требования совпадающими, пока сценарии
  расходились; расхождение обнаруживалось бы уже при архиве. На живом дереве
  таких требований оказалось 14. Сравнение идёт по полному блоку
  (`## `#`–`###` как граница тела), горизонтальные разделители `---`
  игнорируются — это оформление, а не содержание.
- **Change'ы копятся быстрее, чем архивируются.** За проект архивировано 50,
  активно 12, и часть из них давно выполнена. Причина видна на примерах
  `2026-10-04-task-queue-platform-ops` и `2026-10-04-task-queue-channel-switch`:
  собственная работа обоих была сделана целиком, а незавершёнными их держали
  чекбоксы в разделах «Не в этом change'е» и «Открыто для владельца» — то есть
  обещания, которые этим change'ам не принадлежали. Чужой чекбокс нельзя
  закрыть, не закрыв чужую работу. Архивировать change с такой историей нельзя
  вслепую: его собственные формулировки успевают разойтись с кодом, и архив
  это обнаруживает — при разборе этих двух канон утверждал, что удаление
  tombstone'ов не выполнено, хотя файлов в дереве нет.
- **Три сценария `data/query` расходятся с кодом** и ждут решения владельца:
  код отказа пула — `pool_busy` (`mcp-platform/libs/enterprise_data/db.py:453`),
  а не `queue_full`; предел ожидания — `wait_sec` у класса работы, а не
  `pool_timeout`; форма ответа о приёме события — строка `"accepted"` /
  `"dropped"` либо пара `{"accepted": n, "dropped": m}`, а не объект с
  `reason`. Расхождения перечислены в `## Verification` этой спеки.

### Решено 2026-10-08 (бывшие «требует решения», сняты решением владельца)

- **`runtime/cli-client` удалена вместе с change, который её вводил.** Спека на
  546 строк и 8 требований описывала модуль `lib/channels/cli_channel.py`,
  которого не было ни на диске, ни в `git ls-files`. Она была создана коммитом
  `2affe7e` («12 capability-спек, отсутствовавших в каноне») **при живом,
  незаархивированном change** — то есть в обход собственного правила реестра:
  «canonical spec создаётся ПОСЛЕ архивации change» (`COMPONENTS.md:9-14`).
  Change `unify-runtime-channels` отменён и удалён: за девять дней не сделан ни
  один пункт. Вместе с ним ушли пять требований, которых в каноне не было и
  которые были отменяемыми (`ApplicationContext.create` без `role`,
  `composition принадлежит Gateway`, и ещё три) — то есть `role` остаётся и в
  коде, и в каноне.
  Побочный эффект, который пришлось снять руками: в `runtime/entrypoints` было
  **пять пометок** «предпосылка живёт **до** `unify-runtime-channels`» (строки
  268, 302, 372, 1199 и сценарий рядом) — они объявляли требования временными
  в ожидании снятия. Change отменён, снимать больше некому, поэтому все пять
  переписаны на постоянные. **Общий класс:** отмена плана требует пересмотра
  всех мест, которые этот план обещал изменить, — иначе канон ссылается в
  пустоту и объявляет вечным то, что собиралось отменить.
- **`queue-as-anchor-identity`: 9 задач закрыты по диску, архив не запущен.**
  Чекбоксы врали в обе стороны — часть работы была сделана и описана в прозе
  «Что сделано на этой волне», но не отмечена. Перепроверено и закрыто Ф0.1-0.2,
  Ф1.1-1.2, Ф3.1-3.4, Ф7.1. Ф1.5 остаётся открытой по существу: идентичность
  сведена к одному владельцу архивным `2026-10-05-unify-turn-identity`
  (`lib/services/turn_identity.py`), но два документа всё ещё обещают
  `metadata.correlated`, которого код не даёт.
  **Ошибка, которую я повторил:** субагент назвал две задачи невыполнимыми как
  написанные, и я повторил это владельцу без проверки. Проверка показала, что
  4.2 требует **объявить** ключ `unclosed_run_threshold_sec` (то есть это
  обычная задача, а не ссылка на снятое), и уехала только строка в 4.6. Вывод
  тот же, что и раньше: утверждение субагента проверяется отдельно от его адреса.
- **Реестр компонентов врёт не о тех строках.** Задачи двух change'ов утверждали,
  что `COMPONENTS.md` перечисляет `CacheProvider` и `VectorIndexService` с
  несуществующими файлами агента. Сплошная проверка всех 59 ссылок реестра по
  дереву показала: `VectorIndexService` не встречается ни разу, `CacheProvider`
  указывает на живой `mcp-platform/libs/enterprise_data/snapshot/contracts.py`.
  Реальным расхождением была строка `Profiles`: источник профилей контура
  указан как `config.json::profiles`, тогда как профили объявляет
  `mcp-platform/platform.json::profiles` (проверено проходом по вложенным
  dict'ам). Исправлено `3bf1a8c`. **Урок:** утверждение задачи о состоянии
  реестра устарело раньше, чем её закрыли, и проверка дерева заменила собой
  правку — правки не потребовалось.
- **Прозуические разделы канона правится напрямую, а не дельтой.** Формат дельт
  на `## Public Contract` / `## Implementation` / `## Error Behavior` и прочие
  прозуические разделы **не расширяется**: дельта применяется архивом
  необратимо к канону, общему с несколькими писателями. Проза приведена к коду
  в `data/cache-provider`, `data/vector-indexes` и `architecture/component-model`
  — 13 утверждений, каждое с подтверждением `файл:строка`.
- **У `gateway.console_level` теперь один владелец.** Ключ читался двумя
  модулями (`operator_console` разбирал значение, `logging_utils` поднимал
  `ConfigService` и читал секцию сам), а чтение было обёрнуто в
  `except Exception`, который подставлял дефолт. Решение владельца: владелец —
  `operator_console.resolve_console_level()`, `logging_utils` — потребитель,
  отказ объявляется и называет оба источника. Канон при этом был **прав**:
  требование «не заменяться дефолтом молча» уже существовало
  (`runtime/operator-console`), и его не исполнял единственный читатель.
  Рантайм-переключения глубины в дереве нет — `set_console_level` зовётся только
  при старте, поэтому «дефект переключения» из proposal'а не подтвердился.
- **Требование о публикации сводки здоровья индексов снято.** Публиковать было
  некому (`PreloadService` удалён), а требование жило в нормативном тексте без
  носителя. Решение владельца — снять; расчёт `compute_index_health` остаётся, а
  тест, охраняющий его инварианты, объявлен в докстринге как неснимаемый.
  **Уточнение к прежней записи:** «второго канона, требующего то же событие», не
  оказалось — его требование «Sync-события через DbLoggingService» снято архивным
  change `2026-10-05-logging-db-dead-producers`.
- **Требование «Прогрев индексов при старте» приведено к коду.** Оно называло
  несуществующие `provider.preload_indexes(db_table)`, `self._index_cache` и
  место объявления `gateway.vector.index.indexes.*`; требовало ронять старт на
  сбое одного индекса и запрещало сборку по требованию — тогда как код именно её
  и делает (`mcp-platform/libs/vectors/owner.py:414-420`) и сбой индекса
  пропускает (`servers/enterprise/server.py:956-958`).
