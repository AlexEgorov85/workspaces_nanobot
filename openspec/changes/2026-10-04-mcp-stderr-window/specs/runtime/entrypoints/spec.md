# runtime/entrypoints Specification — дельта

## ADDED Requirements

### Requirement: stderr процесса платформы виден отдельно от stderr агента

stderr процесса `enterprise-mcp` MUST NOT попадать в консоль вперемешку с
журналом агента, если оператор это объявил: `errlog`, передаваемый в
`mcp.client.stdio.stdio_client`, MUST быть файлом-приёмником из
`gateway.agent.enterprise_mcp.stderr_log`
(`lib/services/enterprise_mcp_client.py::EnterpriseMcpClient._errlog`).

Путь журнала MUST NOT попадать в argv процесса: это транспорт на стороне
агента, а не настройка платформы, и объявление настроек платформы из агента
запрещено.

Баннер рукопожатия MUST называть, куда ушёл stderr платформы, независимо от
исхода: имя файла и результат попытки открыть окно. Молчание о режиме
наблюдения запрещено — «окно не открылось» и «смотреть не на что» MUST
различаться в выводе.

Отказ открыть файл или окно MUST NOT поднимать исключение и MUST NOT
останавливать старт: процесс продолжит писать в stderr агента, а баннер
скажет, куда именно.

#### Scenario: объявлен файл — stderr уходит в файл, а не в stderr агента

- **WHEN** в `gateway.agent.enterprise_mcp` объявлен `stderr_log`
- **THEN** `stdio_client` MUST получать `errlog` открытым файлом
  (`_errlog()`, проверяется `TestClientWiring::test_declared_path_becomes_errlog`)
- **AND** путь MUST NOT встречаться в `describe()["args"]`
  (`::test_path_never_reaches_process_arguments`)

#### Scenario: файл не объявлен — поведение прежнее

- **WHEN** ключ `stderr_log` отсутствует или пуст
- **THEN** `errlog` MUST быть `None`, то есть значением по умолчанию
  `stdio_client` — stderr агента
  (`TestClientWiring::test_without_declaration_errlog_stays_none`,
  `::test_blank_declaration_is_no_declaration`)

#### Scenario: баннер называет назначение stderr

- **WHEN** рукопожатие прошло (`gateway._connect_enterprise_mcp`)
- **THEN** в выводе MUST быть строка `stderr_report()`
  (`TestHandshake::test_stderr_destination_is_reported`)

#### Scenario: невозможность открыть файл не останавливает старт

- **WHEN** по объявленному пути открыть файл нельзя (например, на пути
  лежит каталог)
- **THEN** `open_redirect` MUST вернуть `None` без исключения, клиент
  MUST продолжить работу в stderr агента
  (`TestRedirectFile::test_unopenable_file_is_not_fatal`)

### Requirement: stderr процесса платформы доступен для чтения в отдельном окне

Файл-приёмник MUST показываться отдельной программой-зрителем: на Windows —
в новой консоли (`CREATE_NEW_CONSOLE`) с чтением файла с ожиданием и
UTF-8, на Linux — в терминале с `tail`, следующим за пересозданием файла.

Создание процесса платформы MUST остаться у SDK: перенаправляется `errlog`,
а не подменяется протокольная обвязка `stdio_client`.

На машине без графики (`DISPLAY` и `WAYLAND_DISPLAY` пусты) окно MUST NOT
искаться, а отчёт MUST называть файл.

#### Scenario: окно на Windows создаётся с правильной кодировкой

- **WHEN** зритель поднимается на Windows
- **THEN** команда MUST содержать `Get-Content ... -Wait` и принудительную
  UTF-8 (`chcp 65001`, `OutputEncoding`), иначе русские сообщения в окне
  были бы нечитаемы
  (`TestWindowsViewer::test_command_waits_for_the_file`,
  `::test_command_forces_utf8`)

#### Scenario: headless Linux называет файл вместо окна

- **WHEN** `DISPLAY` и `WAYLAND_DISPLAY` пусты
- **THEN** отчёт MUST содержать путь к файлу и упоминать `DISPLAY`
  (`TestPlatformDispatch::test_headless_linux_reports_the_file`)

#### Scenario: кавычки в пути не ломают команду зрителя

- **WHEN** объявленный путь содержит одинарную кавычку
- **THEN** литерал MUST быть экранирован удвоением
  (`TestWindowsViewer::test_quote_in_path_is_escaped`)

### Requirement: Журнал stderr платформы относится к прогону и не рвётся на обрыве

Файл MUST обнуляться при первом открытии в процессе агента, а
переподключение после обрыва сессии MUST дописывать в тот же файл. Иначе
зритель показывал бы вывод прошлого прогона как текущий, а обрыв сессии
обрывал бы журнал ровно в том месте, ради которого его читают.

#### Scenario: прошлый прогон не виден как текущий

- **WHEN** файл уже содержит вывод прошлого прогона
- **THEN** `open_redirect` MUST открыть его на запись с обнулением
  (`TestRedirectFile::test_log_is_truncated_not_appended`)

#### Scenario: переподключение не открывает файл заново

- **WHEN** `_errlog()` вызван повторно после обрыва сессии
- **THEN** MUST вернуться тот же открытый файл, а не новый
  (`TestClientWiring::test_file_is_opened_once_per_client`)
