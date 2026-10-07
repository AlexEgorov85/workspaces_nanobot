# Задачи

## 1. Агент: глубина строки вызова следует за исходом

- [x] 1.1 `lib/hooks/terminal_tool_print_hook.py`: отказ → `CONSOLE_LEVEL_QUIET`,
  успех → `CONSOLE_LEVEL_TURN`; обоснование в модульной строке
- [x] 1.2 Там же: обновлён модульный докстринг — «единственный терминальный
  источник ошибки tool'а» снова правда, потому что отказ виден при любой глубине

## 2. Платформа: громкость объявлена

- [x] 2.1 `libs/enterprise_common/settings.py`: `ENTERPRISE_LOG_STDERR_LEVEL`
  (`FROM_FILE`, `file_key="logging.stderr_level"`) + секция `logging` в
  `SHARED_SECTIONS` и `SHARED_SETTINGS`
- [x] 2.2 `mcp-platform/platform.json`: секция `logging` с `stderr_level: "INFO"`
  и `_about`, объясняющим, что видно на каждом уровне
- [x] 2.3 `servers/enterprise/server.py`: `_resolve_stderr_level` (чистая
  функция, уровень как число) + `_configure_logging(settings)`; `main()` читает
  `Settings` **до** настройки вывода
- [x] 2.4 Там же: уровень ставится на корневой логгер явно, а не только через
  `basicConfig` (он no-op при наличии обработчиков)
- [x] 2.5 Там же: неизвестное имя уровня — `WARNING` с перечнем допустимых и
  откат на `INFO`

## 3. Стражи

- [x] 3.1 `tests/test_operator_console_levels.py`: настоящий `TerminalToolPrintHook`
  прогоняется через боевой `console_sink_filter` — успех виден на `turn`, отказ
  виден на `turn`/`quiet`/`trace` с текстом ошибки, успех скрыт на `quiet`
- [x] 3.2 `mcp-platform/tests/test_operator_call_output.py`: объявленный уровень
  доезжает до числа уровня (`DEBUG`/`warning`/`INFO`), файл → реестр → `Settings`
  (значение из файла названо в `logging`), опечатка даёт `WARNING` и `INFO`,
  логгер `mcp` остаётся на `WARNING` даже при `DEBUG` процесса
- [x] 3.3 Соседние наборы: `mcp-platform/tests/test_settings_registry.py`,
  `test_profile_overlay.py`, `test_server_bootstrap.py`,
  `test_journal_min_level_flag.py`, `test_agent_settings_block.py` (214 проверки)
  и `tests/test_operator_console_levels.py`, `tests/contract/
  test_composite_hook_lifecycle.py`, `tests/test_agent_factory.py` (48)
- [x] 3.4 Каждый страж проверен ломанием (правка дерева с немедленным
  восстановлением; переносы строк сохраняются, иначе восстановление переписало бы
  файл целиком в LF — так уже случилось в пробе предыдущего change):
  глубина `trace` на обоих исходах, то есть исходный дефект → 3 падения;
  глубина `turn` на обоих исходах (отказ не виден на `quiet`) → 1; уровень
  процесса всегда `INFO` → 3; опечатка в уровне названа не будет → 1; логгер
  `mcp` больше не понижен → 3. Без ломок 16 и 14 passed.

## 4. Проверка вживую

- [x] 4.1 Подъём с профилем `test` и `logging.stderr_level: DEBUG`: строка
  успеха вернулась — `DEBUG:...:вызов list_scripts → ok (169мс)`, отказ на
  `WARNING` — `вызов queue_stats → error identity_missing (0мс)`
- [x] 4.2 Тот же прогон на `INFO`: строки успеха нет, отказ на месте

## 5. Оформление канона

- [x] 5.1 `openspec/changes/2026-10-05-visibility-under-control/`: proposal,
  tasks, дельта в `specs/runtime/operator-console/spec.md`
- [x] 5.2 AGENTS.md: глубина по исходу в записи хука, ключ
  `logging.stderr_level` — в записи клиента платформы
