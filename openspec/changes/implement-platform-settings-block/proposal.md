# Реализация блока настроек агента

## Why

Спека `specs/runtime/platform-settings` лежит в каноне с 2026-10-03 (change
`2026-10-03-platform-settings-block`), а блока, который она описывает, не было:
порог журнала доезжал до платформы флагом запуска `--log-min-level`, который не
объявлен ни в одном файле. Единственным следом применённого значения была
строка в stderr процесса платформы, которая исчезала вместе с процессом, —
то есть вопрос «какой порог журнала применяется» не имел ответа ни в одном
объявлении.

Решение владельца, закрывавшее незакрытое место спеки (путь к файлу блока):
`workspace/data_store/agent-settings.json`, объявляется в
`mcp-platform/platform.json` верхнеуровневым ключом `agent_settings` со
значением `${NANOBOT_WORKSPACE}/data_store/agent-settings.json`. Ключ применён
владельцем 2026-10-04.

## What changes

**Платформа — `libs/enterprise_common/settings.py`**

- Первая агентская настройка в реестре: `ENTERPRISE_LOG_MIN_LEVEL`,
  `owner="agent"`, ключ блока `logging.db.min_level`, `OPTIONAL` (пустой блок
  и отсутствующий файл дают одно состояние — «порог не задан»).
- `agent_settings_dictionary()` — единственный словарь принимаемых ключей.
  Агент своего не имеет.
- `read_agent_settings_file()` — чтение файла: отсутствие — пустой блок,
  нечитаемый/неразбираемый/не-объект — `InfrastructureError`.
- `merge_agent_settings()` — проверка **всех** ключей до применения первого;
  неизвестный ключ, ключ платформенной настройки и значение не своего типа —
  отказ с называнием ключа и счётом «принято N из M».
- `Settings(..., agent_settings_path=...)` — блок читается и проверяется один
  раз, в конструкторе. Для `owner="agent"` блок — **единственный** источник:
  окружение не читается (оно приоритетнее файла и молча затирало бы
  объявление), `platform.json` — тоже.
- `agent_settings_report()` / `agent_settings_summary()` — что пришло и что
  применено, ключами блока.
- `agent_settings` в `RESERVED_SECTIONS` (объявление пути, а не значение) и в
  `SHARED_SECTIONS`.

**Платформа — `execution/factory.py`**: `resolved_journal_min_level()` читает
порог из слоя разрешённых настроек; `build_execution_layer()` по умолчанию
берёт его оттуда (дефолт — маркер `FROM_SETTINGS`, не значение), пустое
значение — «без фильтра», а не `INFO`.

**Платформа — `servers/enterprise/server.py`**: разбор
`--agent-settings-file` (и `=путь`), передача пути в `Settings`, печать
`agent_settings_summary` в баннере, `min_level` обоих писателей — из слоя
настроек. `_log_min_level_from_argv` и параметр `log_min_level` в `build()` /
`_build_container()` сняты.

**Агент — `lib/services/agent_settings.py` (новый)**: чтение объявления
`platform.json → agent_settings`, строгое разворачивание `${NANOBOT_WORKSPACE}`,
чтение блока, сборка значений из `config.json`, запись файла с правами
`0o600`, аргумент запуска `--agent-settings-file <путь>`.

**Агент — `lib/services/enterprise_mcp_client.py`**: `LOG_MIN_LEVEL_FLAG` и
`_journal_min_level()` сняты, в argv едет один аргумент с путём к блоку и ни
одного значения. `JOURNAL_MIN_LEVEL_PATH` живёт в `agent_settings.py` и
переэкспортирован.

**Агент — `config.py` + `config.json`**: второй процесс платформы
(`MCPProvider` → `mcp_enterprise_*`) получает `--agent-settings-file
${NANOBOT_ENTERPRISE_MCP_AGENT_SETTINGS}`; путь читается из объявления
платформы через поднятую секцию `enterprise_mcp.cwd`, а не вычисляется.
`NANOBOT_ENTERPRISE_MCP_LOG_MIN_LEVEL` больше не экспортируется.

## Требования спеки → реализация → тест

| Требование (canon) | Где | Тест |
| --- | --- | --- |
| Блок передаётся одним аргументом; значения не в argv; профиль отдельно | `agent_settings.AgentSettingsBlock.argv`, `settings.AGENT_SETTINGS_FILE_FLAG`, `server._agent_settings_path_from_argv` | `tests/.../test_journal_threshold_reaches_mcp.py::TestClientForwardsTheThreshold`; `mcp-platform/tests/test_journal_min_level_flag.py::TestArgvParsing` |
| Права файла не доступны group/other | `BLOCK_FILE_MODE`, `publish_agent_settings` | `tests/test_agent_settings_block.py::TestFilePermissions` |
| Платформа объявляет закрытый словарь; агент не объявляет свой | `agent_settings_dictionary()`, `AGENT_BLOCK_PATHS` | `mcp-platform/tests/test_journal_min_level_flag.py::TestOneDeclaration`, `tests/.../test_enterprise_mcp_settings_contract.py` |
| Неизвестный ключ — отказ с называнием ключа и словаря | `merge_agent_settings` | `mcp-platform/tests/test_journal_min_level_flag.py::TestRefusals` |
| Владение, а не приоритет | `merge_agent_settings`, `read_platform_file` | `mcp-platform/tests/test_agent_settings_block.py::TestRefusedBlock` |
| Блок не перенаправляет доступ к данным; проверка мутацией | словарь + страж перечислением | `mcp-platform/tests/test_agent_settings_block.py::TestDataAccessIsOutOfReach` |
| Значение попадает в код через слой разрешённых настроек | `Settings._resolve` (ветка `OWNER_AGENT`), `resolved_journal_min_level()` | `::TestSingleSource`, `::test_reaches_the_journal_writer`, `test_journal_writer_threshold_wiring.py::TestWiringIsNotOptional` |
| Объявленное исключение не становится вторым источником | дефолт `FROM_SETTINGS` | `test_journal_writer_threshold_wiring.py::test_the_default_threshold_is_the_settings_layer` |
| Блок применяется один раз при старте | `Settings.__init__` | `mcp-platform/tests/test_agent_settings_block.py::TestAppliedOnce` |
| Применённое значение видно на старте; сводска включает отклонённые | `agent_settings_report`, `agent_settings_summary`, баннер сервера | `::TestAcceptedBlock`, `test_journal_min_level_flag.py::TestThresholdReachesTheWriter` |
| У значения один путь доставки; старый флаг снят | флага нет в `lib/**` и в `mcp-platform/**` | `::TestOldDeliveryIsGone` (AST: литерал в коде, не в доктрине) |
| Добавление настройки не требует правки кода запуска | `AGENT_BLOCK_PATHS` + реестр | `tests/test_journal_threshold_single_source.py::TestOneSourceWithTwoReaders` |
| Один источник у обеих половин журнала | `block_values` / `_make_db_logging` | `test_journal_threshold_single_source.py::test_writer_and_the_block_resolve_the_same_value` |

## Дельта спеки

`specs/runtime/platform-settings/spec.md` — `## MODIFIED Requirements` для
требования «Значение попадает в код только через слой разрешённых настроек»:
в спеку внесено **перечисленное исключение** — параметр
`build_execution_layer(min_level=…)` существует ради тестов, дефолтом
сборки стоит маркер «взять из слоя», и ни один вызов в коде платформы не
передаёт его литералом. Сценарий «исключение объявлено, а не подразумевается»
требовал именно перечисления, а молчание было бы дефектом.

## Решение, где я не согласился с заданием

**Настройка порога осталась в реестре** (указание «убери
`ENTERPRISE_LOG_MIN_LEVEL` из реестра» не выполнено). Канон говорит обратное,
дословно:

> «Ключ блока MUST быть настройкой, объявленной в реестре платформы с
> `owner=OWNER_AGENT`» (`specs/runtime/platform-settings`).
>
> «Существующий `OWNER_AGENT` в реестре — это объявление, которым сейчас не
> пользуется ни одна настройка и которое нечем наполнить: канала доставки не
> существует. **Эта спека — тот канал**; до неё объявление было недостижимым».

Снятие объявления оставило бы `agent_settings_dictionary()` пустым, и блок
оказался бы нечем наполнить: доставка есть, принять нечего. Вместо снятия
объявления переписан страж, который его требовал:
`test_journal_min_level_flag.py::TestOneDeclaration::test_registry_declares_no_threshold_setting`
(«в реестре не должно быть настройки порога, потому что он едет флагом») и
зеркальный страж в дереве агента
`tests/test_enterprise_mcp_settings_contract.py::test_no_setting_is_owned_by_the_agent`.
Оба опирались на отсутствие канала. Теперь они требуют противоположного:
реестр **обязан** содержать агентскую настройку, и её ключ обязан совпадать со
словарём блока.

Если решение владельца всё же снять объявление, то блок придётся объявлять вне
реестра — и это уже правка спеки, а не реализации.

## Осталось вне этого изменения

1. **Дыра канона: уровень консоли.** `gateway.console_level` не доезжает в
   конфиг консоли. Ключ консоли не может лежать в том же файле — платформа
   отвергнет его как неизвестный, — а перекрытие им `config.json` дало бы
   второй путь к одному значению. Заведено отдельным change'ом
   `close-console-level-canon-gap` (см. каталог рядом).
2. **Второй процесс платформы и порядок.** Путь блока экспортируется при
   инициализации настроек, файл пишет клиент агента при создании
   контекста. Если бы `MCPProvider` поднимал процесс раньше, второй процесс
   прочитал бы отсутствующий файл — это законное состояние («настроек
   агента нет»), не сбой; на сегодня `MCPProvider` поднимается лениво, при
   первом обращении к инструменту.
