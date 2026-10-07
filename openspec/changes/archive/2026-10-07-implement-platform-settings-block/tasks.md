# Tasks

## 1. Платформа: приём и слияние блока

- [x] 1.1 `ENTERPRISE_LOG_MIN_LEVEL` в реестре: владелец — агент, ключ блока
      `logging.db.min_level`, `OPTIONAL`
- [x] 1.2 `agent_settings_dictionary()` — единственный словарь приёма
- [x] 1.3 `read_agent_settings_file()` — нет файла → пусто; файл есть и негоден
      → `InfrastructureError`
- [x] 1.4 `merge_agent_settings()` — проверка всех ключей до применения;
      неизвестный ключ / ключ платформы / чужой тип → отказ с называнием ключа
      и счётом «принято N из M»
- [x] 1.5 `Settings(..., agent_settings_path=...)`: чтение и проверка один раз,
      в конструкторе; для `owner="agent"` окружение не читается
- [x] 1.6 `agent_settings_report()` / `agent_settings_summary()`
- [x] 1.7 `agent_settings` в `RESERVED_SECTIONS` и `SHARED_SECTIONS`
- [x] 1.8 `resolved_journal_min_level()` в `execution/factory.py`; дефолт
      сборки — маркер `FROM_SETTINGS`

## 2. Платформа: разбор флага на старте

- [x] 2.1 `servers/enterprise/server.py`: `_agent_settings_path_from_argv`
      (форма с пробелом и `=путь`), отказ на флаге без значения
- [x] 2.2 `build(agent_settings_path=...)` → `Settings(agent_settings_path=...)`;
      `main` передаёт разобранный путь
- [x] 2.3 `min_level` обоих писателей (`data` и слой исполнения) — из слоя
      разрешённых настроек
- [x] 2.4 Баннер печатает `agent_settings_summary` рядом с применённым порогом
- [x] 2.5 `_log_min_level_from_argv` и `log_min_level` в `build()` /
      `_build_container()` сняты
- [x] 2.6 Доктрины `DataService._journal_min_level` и `EventWriter` приведены
      к доставке блоком

## 3. Агент: сборка и доставка блока

- [x] 3.1 `lib/services/agent_settings.py`: объявление
      `platform.json → agent_settings`, строгие `${...}`, чтение блока, запись
      `0o600`, `AGENT_SETTINGS_FILE_FLAG`
- [x] 3.2 `JOURNAL_MIN_LEVEL_PATH` перенесён в `agent_settings.py` и
      переэкспортирован из клиента
- [x] 3.3 `client_from_settings`: в `argv` — один аргумент с путём, ни одного
      значения; `LOG_MIN_LEVEL_FLAG` и `_journal_min_level()` сняты
- [x] 3.4 `config.py` + `config.json`: второй процесс платформы получает
      `--agent-settings-file ${NANOBOT_ENTERPRISE_MCP_AGENT_SETTINGS}`, путь
      читается из объявления платформы через поднятую секцию;
      `NANOBOT_ENTERPRISE_MCP_LOG_MIN_LEVEL` больше не экспортируется

## 4. Тесты

- [x] 4.1 `mcp-platform/tests/test_agent_settings_block.py`: чтение, сводка,
      отсутствие файла, отказы, владение, мутационный страж словаря, блок один
      раз, страж снятого флага на всём дереве платформы, разбор флага сервером
- [x] 4.2 `tests/test_agent_settings_block.py`: объявление и подстановка,
      чтение, публикация, `argv` без значений, «одна строка на настройку»,
      права файла, страж снятого флага и второго процесса
- [x] 4.3 `tests/test_journal_threshold_reaches_mcp.py` — переписан на блок
- [x] 4.4 `tests/test_journal_threshold_single_source.py` — читателей два,
      сверка значений писателя и блока, путь по тождеству
- [x] 4.5 `mcp-platform/tests/test_journal_min_level_flag.py` — разбор флага,
      доезд до обоих писателей, баннер, отказы, «одно объявление»
- [x] 4.6 `mcp-platform/tests/test_journal_writer_threshold_wiring.py` — порог
      из слоя настроек, дефолт — маркер, ни одного литерала в вызовах
- [x] 4.7 `tests/test_mcp_platform_declaration.py` — второй процесс объявляет
      блок; порог в окружение не экспортируется
- [x] 4.8 `tests/test_enterprise_mcp_settings_contract.py` — агентские настройки
      реестра == словарь блока

## 5. Дельта и документация

- [x] 5.1 `specs/runtime/platform-settings/spec.md`: перечисленное исключение
      (`build_execution_layer(min_level=…)` как шов для тестов) + сценарий
      «исключение не становится вторым источником»
- [x] 5.2 `proposal.md` / `tasks.md` приведены в состояние по факту
- [x] 5.3 `platform.json`: ключ `agent_settings` применён владельцем

## 6. Вне этого изменения

- [x] 6.1 Дыра канона про `gateway.console_level` заведена отдельным change'ом
      `close-console-level-canon-gap` (решение не принято, реализации нет)
- [ ] 6.2 Вопрос владельцу: снимать ли `ENTERPRISE_LOG_MIN_LEVEL` из реестра
      (канон требует обратного; см. раздел «Решение, где я не согласился»)
- [ ] 6.3 Удалить вручную `workspace/block.json` — артефакт первого прогона
      теста до автоподстановки `NANOBOT_WORKSPACE` в `$env:TEMP`
      (восстановимое удаление заблокировано политикой окружения)
