## 1. Реализация в `lib/services/schema_validation.py`

- [x] 1.1 Добавить helper `_hint_for_profile(profile: str) -> str`,
      возвращающий actionable-команду по профилю
      (`prod` → `python tools/migrate.py --apply`,
      `test` → `python tools/apply_test_profile_tables.py`,
      иначе → generic-вариант «примените миграции для выбранного
      профиля»); проверить, что функция покрыта docstring с
      явными примерами для `prod` / `test` / unknown

- [x] 1.2 Перевести `_build_message()` на русский: шапка
      «Не найдены обязательные runtime-таблицы», заголовок
      «Отсутствуют таблицы:», список таблиц `  - <schema.table>`,
      и подсказка из `_hint_for_profile(self.profile)`;
      проверить, что `tests/test_schema_validation.py::TestSchemaValidationError::test_message_contains_missing_and_profile`
      и `test_empty_missing_message_still_safe` остаются зелёными
      после перевода (структурные поля не меняются)

- [x] 1.3 Перевести `_build_config_message()` на русский:
      «Не найдены обязательные ключи конфигурации», заголовок
      «Отсутствуют ключи:», список ключей `  - <dot.path>`,
      подсказка с указанием секций `project.json`
      (`channels.postgres.*` / `logging.db.*`);
      проверить, что `tests/test_schema_validation.py::TestExpectedTableNames::test_lazy_settings_missing_keys_reports_correctly`
      остаётся зелёным (assertion про `<settings>` сохранён)

- [x] 1.4 Перевести loguru-сообщение
      «startup schema validation failed: profile=… missing=…»
      на русский («не пройдена startup-проверка схемы: profile=…
      отсутствуют=…»); проверить визуально, что loguru-запись
      читается оператором без перевода

## 2. Тесты

- [x] 2.1 Обновить `test_message_contains_missing_and_profile` —
      заменить подстроки `assert "hint: apply migrations" in text`
      на assertions под новые русские строки
      («Не найдены обязательные runtime-таблицы»,
      «Отсутствуют таблицы:», `python tools/migrate.py --apply`
      для профиля `prod` / `python tools/apply_test_profile_tables.py`
      для профиля `test`); проверить, что тест зелёный

- [x] 2.2 Добавить параметризованный тест
      `test_hint_for_profile` с кейсами
      `("prod", "python tools/migrate.py --apply")`,
      `("test", "python tools/apply_test_profile_tables.py")`,
      `("dev", "примените миграции для выбранного профиля")`;
      проверить, что новый тест зелёный

- [x] 2.3 Добавить тест `test_missing_config_keys_message_russian` —
      проверить, что `_MissingConfigKeys.__str__` содержит
      русскую шапку, имя профиля, список ключей и подсказку
      про `project.json`; проверить, что тест зелёный

## 3. Валидация

- [x] 3.1 Прогнать `python -m pytest tests/test_schema_validation.py -q` —
      все тесты зелёные

- [x] 3.2 Прогнать `openspec.cmd validate i18n-schema-validation-error` —
      валидация зелёная (проверка структуры proposal/spec/design/tasks)

- [x] 3.3 Визуально проверить вывод при ручном запуске
      `python gateway.py --profile=test` против пустой БД —
      оператор видит русское сообщение с actionable-командой
      `python tools/apply_test_profile_tables.py`

## 4. Свидетельства приёмки (2026-10-02)

Пункты 1.1–1.4, 2.1–2.3 выполнены кодом **до** появления этого прохода —
чекбоксы оставались незакрытыми. Сверка велась по коду, а не по отметкам.

| Пункт | Доказательство |
|---|---|
| 1.1 | `lib/services/schema_validation.py:37` `_hint_for_profile`, docstring с `Examples` для `prod` / `test` / `dev` (:45-51) |
| 1.2 | `lib/services/schema_validation.py:113-122` — русская шапка, «Отсутствуют таблицы:», список `  - <schema.table>`, подсказка из `_hint_for_profile` |
| 1.3 | `lib/services/schema_validation.py:140-152` — «Отсутствуют ключи:», список `  - <dot.path>`, подсказка про `channels.postgres.*` / `logging.db.*` |
| 1.4 | `lib/services/schema_validation.py:269` — «не пройдена startup-проверка схемы: profile=… отсутствуют=…» |
| 2.1 | `tests/test_schema_validation.py:79,100` |
| 2.2 | `tests/test_schema_validation.py:124-139` `TestHintForProfile`, параметризован |
| 2.3 | `tests/test_schema_validation.py:111` `test_missing_config_keys_message_in_russian` |
| 3.1 | `29 passed in 0.25s` |
| 3.2 | `Change 'i18n-schema-validation-error' is valid`, exit 0 |
| 3.3 | Фактический вывод объекта исклюнения для профилей `prod` / `test` / `dev` и при пустом списке отсутствующих — текст полностью русский, подсказка actionable |

Пункт 3.3 проверен не запуском `gateway.py`, а печатью того же объекта
`SchemaValidationError`, который entrypoint выводит в stderr перед
выходом с кодом 2. Разница — только в источнике данных о недостающих
таблицах (реальный SELECT против собранного вручную списка); формат,
шапка и подсказка формируются одним и тем же кодом.

