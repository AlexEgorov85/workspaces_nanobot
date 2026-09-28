## Context

Текущее состояние — `SchemaValidationError.__str__` и
`_MissingConfigKeys.__str__` возвращают английский текст
("Schema validation failed for profile='…'…"), не зависящий
от профиля. Сообщение печатается через `logger.error(...)` и
`stderr` в `gateway.main()` / `cli_agent.main()` (см. `spec.md:128-134`).

Мотивация — в proposal.md. Здесь — только технические решения
по реализации.

## Goals / Non-Goals

**Goals:**
- Сообщение на русском с сохранением английских имён собственных
  (таблицы, профиль, config-ключи, CLI-команды, файлы).
- Actionable-подсказка для `prod` и `test`, generic-fallback
  для остальных профилей.
- Сохранение structured-полей исключения (`.missing`,
  `.missing_config_keys`, `.profile`) и публичного API
  `SchemaValidationService`.
- Обратная совместимость по API (тот же класс, тот же базовый
  `ConfigurationError`, тот же `exit 2`).

**Non-Goals:**
- Локализация остального gateway-startup (баннер, логи хуков,
  `Starting nanobot gateway…`). Это вне scope — для этого
  нужен отдельный change с инфраструктурой i18n.
- Локализация `error_messages.internal_error` (это ответ агента
  пользователю через `TurnDelivery.fail` — отдельный компонент).
- Изменение списка 6 проверяемых таблиц или их источника
  (SETTINGS).
- Добавление новых полей в исключение.

## Decisions

### Decision 1: Текст сообщения — обычные строковые литералы в `_build_message`

Сообщение формируется через `f-string`-конкатенацию в
`SchemaValidationError._build_message` /
`_MissingConfigKeys._build_config_message`. Никаких
gettext / i18n-инфраструктуры — в проекте её нет, и для одной
точки вывода это overengineering.

**Альтернатива:** вынести строки в `workspace/overrides/...` и
читать через `consolidator_locale`-подобный механизм.
Отвергнуто — `consolidator_locale.py` подменяет Jinja-шаблоны
nanobot, а не runtime-строки; заводить параллельную инфраструктуру
ради одного сообщения — непропорциональные затраты.

### Decision 2: Имена таблиц / профиля / config-ключей — латиницей

В теле сообщения остаются латиницей:
- `schema.table` (например, `public.agent_question_runs_test`);
- имя профиля (`profile='prod'`, `profile='test'`);
- config-ключи (`channels.postgres.table_name`);
- имена CLI-утилит и флагов (`tools/migrate.py --apply`).

Переводятся только служебные слова («не найдены», «примените»,
«подсказка» и т.п.). Это согласовано с AGENTS.md
(«Имена собственные — всегда латиницей, без перевода»).

### Decision 3: Actionable-подсказка — профильно-зависимая

Helper `_hint_for_profile(profile: str) -> str` возвращает
профильно-зависимую команду:

```python
def _hint_for_profile(profile: str) -> str:
    if profile == "prod":
        return "python tools/migrate.py --apply"
    if profile == "test":
        return "python tools/apply_test_profile_tables.py"
    return "примените миграции для выбранного профиля"
```

Whitelist закрытый (`{"prod", "test"}`) — те же два профиля,
что поддерживаются конфигурацией (см. AGENTS.md → «Профили
конфигурации», `config.PROFILES = {"prod", "test"}`).

**Альтернативы:**
- Всегда generic-вариант. Отвергнуто — теряем actionable-ness,
  оператор всё равно идёт в README.
- Hardcode длинного списка команд по всем возможным
  именам таблиц. Отвергнуто — таблицы могут быть переименованы
  через SETTINGS (`test_custom_names_not_hardcoded` в тестах);
  команды привязаны к профилю, а не к именам.

### Decision 4: Loguru-сообщение тоже переводится

Текущее `logger.error("startup schema validation failed:
profile={} missing={}", ...)` тоже переводится на русский —
оператор видит его в journald / файловых логах gateway.

### Decision 5: Тесты обновляются под новые подстроки

`tests/test_schema_validation.py::test_message_contains_missing_and_profile`
проверяет конкретные подстроки сообщения. Эти подстроки
меняются с английских на русские. Структурные проверки
(`profile`, `schema.table`, класс исключения) — сохраняются.

## Risks / Trade-offs

- **[Risk]** Любой внешний код, парсящий stderr gateway для
  alerting, может сломаться на смене языка.
  **Mitigation:** Парсить по структурным полям
  (`exc.missing: list[MissingTable]`) через Python-вызов,
  а не по stderr. Текущий `tools/diagnose_startup.py`
  парсит startup-лог по hook/project-tool inventory, не по
  тексту schema-validation. Мониторинг, завязанный на
  точный текст stderr, в проекте не зафиксирован
  (см. AGENTS.md → Configuration → `logging.db.*` —
  журнал событий идёт через `DbLoggingService`, а не
  через grep по stderr).

- **[Risk]** При добавлении третьего профиля придётся
  обновить `_hint_for_profile` и тест.
  **Mitigation:** Тест явно проверяет три кейса
  (prod / test / unknown); добавление профиля — отдельный
  OpenSpec change (по AGENTS.md → «введение третьего
  профиля — отдельный OpenSpec change»).

- **[Risk]** При смене имени `tools/migrate.py` / `tools/apply_test_profile_tables.py`
  подсказка устареет.
  **Mitigation:** Эти имена стабильны (CHANGELOG ссылается
  на них как на служебные скрипты проекта); при будущем
  переименовании — обновить константу в одном месте.

## Migration Plan

Не требуется: меняется только текст сообщения, без изменения
поведения, API или схемы БД. Никаких миграций, никаких
feature-flags. После merge — оператор сразу видит новый
текст при следующем неуспешном старте.

## Open Questions

Нет.
