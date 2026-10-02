## Context

Текущее состояние runtime соответствует целевой модели, кроме одного
остатка в composition root.

**Что уже корректно (менять не нужно):**

- `config._initialize_settings(profile)` — единственная точка публикации
  `SETTINGS`. Профиль приходит аргументом; env-fallback отсутствует.
- `gateway.py` — `_parse_args` валидирует `--profile` из argv (whitelist
  `{prod, test}`), затем `_cfg._initialize_settings(profile=args.profile)`.
- `cli_agent.py` — фиксированный `CLI_FIXED_PROFILE = "test"`, `--profile`
  отклоняется через `CLI_REJECTED_FLAGS`. Все три вызова
  `ApplicationContext.create()` не передают `profile`.
- `streamlit_app.py` — получает профиль из argv через
  `_resolve_profile_from_argv()`, не из environment.

**Единственный runtime-остаток:**

`lib/core/application_context.py:207-219` — внутри `create()` есть
ветка:

```python
resolved_profile = ctx_settings["profile"]
if "profile" in kwargs:
    profile = kwargs.pop("profile")
    if profile is not None and profile != resolved_profile:
        raise ConfigurationError(...)
```

Она делает две вещи: (1) принимает `profile` через `**kwargs`,
(2) валидирует его согласованность с `SETTINGS["profile"]`. Вторая
часть — это ранняя форма того же lifecycle-контракта, но первая часть
сохраняет возможность пронести профиль через composition root.

**Расхождение в тестах:**

`tests/test_profile_lifecycle.py::test_cli_agent_no_profile_exits_2`
(строки 278-286) ожидает `exit 2` + `--profile is required` при запуске
`python cli_agent.py` без `--profile`. Это контракт, отменённый Stage F
(`cli_agent.py:74-81` отклоняет `--profile`; `cli_agent.py:106`
инициализирует фиксированный `test`). Оба теста не могут быть верны
одновременно.

**Constraints:**

- Тесты `test_profile_lifecycle.py` запускают entrypoint'ы как
  subprocess — это осознанный выбор (проверка реального поведения, а не
  мока). Изменение контракта CLI требует переработки именно этих
  subprocess-тестов.
- В репозитории есть `tests/conftest.py` autouse-фикстура
  `_bootstrap_config_lifecycle`, вызывающая `_initialize_settings(profile="test")`
  один раз за сессию (легитимизировано отдельным требованием в
  `configuration/profiles` spec). Она не зависит от удаляемой ветки.

## Goals / Non-Goals

**Goals:**

- Убрать `profile` из поверхности `ApplicationContext.create()` так, чтобы
  передача `profile=` приводила к `TypeError`, а не молча проходила
  через compatibility-ветку.
- Устранить взаимоисключающие ожидания в тестах профиля: оставить одну
  модель поведения CLI.
- Перевести защиту «environment не участвует в выборе профиля» с
  привязки к историческому имени переменной на архитектурный инвариант
  пути разрешения профиля.
- Синхронизировать активные спеки и документацию с фактической моделью.

**Non-Goals:**

- Не менять `config.py` — механизм уже соответствует целевой модели.
- Не менять `gateway.py` и `cli_agent.py` (runtime-логика корректна).
- Не трогать `streamlit_app.py` — его удаление отдельный change
  (`remove-streamlit-runtime`).
- Не редактировать `CHANGELOG.md` и `openspec/changes/archive/` —
  исторические записи о прошлых решениях.

## Decisions

### D1: Удалить ветку целиком, а не превращать в `TypeError`

**Выбор:** удалить `if "profile" in kwargs:` блок из `create()`,
обновив docstring метода и module docstring.

**Почему не явный `raise TypeError`:**

Python сам порождает `TypeError("create() got an unexpected keyword
argument 'profile'")` при попадании неожиданного ключа в `**kwargs` —
но только если `_resolve_enable_kwargs` его не проглотит. Текущая
`_resolve_enable_kwargs` (строки 59-96) **молча игнорирует** ключи, не
входящие в `DEPRECATED_ENABLE_KWARGS`: цикл `for key, value in
kwargs.items()` проверяет `if key in DEPRECATED_ENABLE_KWARGS` и
иначе ничего не делает. То есть после удаления ветки `profile`
`create(..., profile="test")` завершился бы **тихо**, без ошибки.

Поэтому требуется явный reject неизвестных kwargs. Это делает
`DEPRECATED_ENABLE_KWARGS` не просто списком defaults, а allowlist'ом:
всё, чего в нём нет, отвергается. Побочный эффект — `TypeError` на
опечатках вроде `enable_aduit=False`, что является улучшением, а не
регрессией.

**Альтернатива (отклонена):** оставить `profile` в отдельном
deprecated-перечне с `DeprecationWarning`. Отклонена, потому что у
`profile` нет migration path в `project.json`: в отличие от
`enable_audit` (переносится в `gateway.enable_audit`) переносить
`profile` некуда — он уже не параметр, а состояние `SETTINGS`.
Держать compatibility-ветку для параметра, у которого нет цели
совместимости, — технический долг без выгоды.

### D2: `create()` продолжает читать `SETTINGS["profile"]` и записывать в `ctx.profile`

**Выбор:** сохранить `resolved_profile = ctx_settings["profile"]` и
`ctx.profile = resolved_profile`.

**Почему:** удаление параметра не означает удаления состояния.
`ctx.profile` — наблюдаемое read-only поле, которое используется в
логах и startup-отчётах. Чтение из `SETTINGS["profile"]` — это ровно
то, что требует контракт: composition root получает профиль из
resolved-конфигурации, а не через аргумент. Одновременно сохранён
побочный эффект: обращение к `ctx_settings["profile"]`
материализует `ConfigurationError` на неинициализированном proxy —
lifecycle-контракт не ослабляется.

**Альтернатива (отклонена):** убрать `ctx.profile` полностью. Отклонена:
поле наблюдаемое и кладётся в `diagnose_startup`-подобный вывод; его
удаление — отдельное решение, не связанное с cleanup'ом параметра.

### D3: Name-agnostic static guard вместо grep по историческому литералу

**Выбор:** заменить `tests/test_nanobot_profile_env_removed.py` на тест,
проверяющий, что функции пути разрешения профиля не обращаются к
`os.environ`.

**Проверяемые функции:** `config._initialize_settings`,
`config.resolve_application_config`, `config._merge_profile_overlay`
(через `inspect.getsource` + проверка на отсутствие обращений к
`os.environ` / `os.getenv` / `environ`).

**Почему не проверять весь `config.py`:** файл законно использует
`os.environ` для экспорта секретов (`_export_secrets_to_env`,
строка 277-285) и для резолва `${VAR}` (`_resolve_env_refs`). Правило
«config.py не читает environment» было бы неверным. Правильная граница —
уже и тоньше: **environment может использоваться для секретов и
`${VAR}` substitution, но никогда не участвует в выборе активного
профиля.**

**Почему не grep по всему репозиторию:** привязка к конкретному
историческому имени хрупка — она ломается на rename переменной, но не
ловит ввод env-fallback под **любым другим** именем. Инвариант
должен быть структурным, а не лексическим.

**Альтернатива (отклонена):** сохранить оба теста. Отклонена, потому
дублируют один контракт; grep-тест даёт ложное чувство защиты
(переименуй переменную — тест замолчит, а регрессия останется).

### D4: CLI-часть `test_profile_lifecycle.py` переписывается, а не удаляется

**Выбор:** удалить `test_cli_agent_no_profile_exits_2`, добавить
`test_cli_agent_starts_without_profile_flag` (проверяет, что CLI стартует
без `--profile` и не требует его) и `test_cli_agent_rejects_profile_flag`
(проверяет exit 2 + упоминание профиля в stderr).

**Почему не удалить весь CLI-блок:** эти subprocess-тесты — единственное
место, где проверяется реальное поведение CLI на уровне процесса.
Модульные тесты `_parse_args` в `test_cli_agent_profile.py` не покрывают
error-boundary `ConfigurationError → exit 2` в `main()` (строки
267-271). Потеря этого покрытия означала бы регрессию error-lifecycle
контракта.

**Subprocess-тест для CLI требует БД?** Нет: smoke-путь
(`cli_agent.py:118-134`) инициализирует `SETTINGS` и создаёт
`ApplicationContext` без `ctx.start()`, поэтому PG-соединения не
устанавливаются. `test_gateway_no_profile_exits_2` уже работает по этому
принципу.

### D5: Исторические артефакты не переписываются

**Выбор:** `CHANGELOG.md` и `openspec/changes/archive/` не редактируются.

**Почему:** это записи о прошлых решениях. Архивный change
`2026-09-27-config-profile-cli-flag` анонсировал удаление env-переменной
профиля — это факт того решения, и его дельта-spec по определению
описывает состояние на момент принятия. Редактирование архива создаёт
ложную картину для читателя, который изучает историю изменения.

**Следствие для guard-тестов:** проверка «0 упоминаний» применяется к
active-документации и runtime-коду, а не к `CHANGELOG.md` и
`openspec/changes/archive/`.

## Risks / Trade-offs

**[Reject неизвестных kwargs ломает существующие тесты, передававшие
неизвестные ключи]** → Перед удалением ветки прогнать
`tests/test_application_context_role.py` (там есть
`test_resolve_with_unknown_kwarg_silently_keeps_it`) и обновить его:
после change неизвестный kwarg должен отвергаться, а не игнорироваться.
Это ожидаемое изменение поведения, фиксируемое в тесте.

**`ctx.profile` может быть `None` при неинициализированном `SETTINGS`]**
→ Поведение не меняется: обращение к `ctx_settings["profile"]` уже
существует в текущем коде (строка 206) и поднимает
`ConfigurationError` на uninitialized proxy. Порядок обращений
сохраняется.

**[Параллельная работа другого агента над теми же файлами]** →
Перед каждой правкой перечитывать файл; не откатывать уже внесённые
изменения. Список выполненного другим агентом зафиксирован в
`tasks.md` § «Уже выполнено параллельным агентом» и не дублируется.

**[Subprocess-тесты CLI могут стать медленнее]** → CLI без `--profile`
доходит до создания `ApplicationContext` (в отличие от прежнего
мгновенного `exit 2` в `_parse_args`). Оценка: единицы секунд на
импорт `nanobot`. Существующие subprocess-тесты
`test_cli_agent_profile.py` уже имеют `timeout=10.0`, что покрывает
этот бюджет.

## Migration Plan

1. Удалить compatibility-ветку `profile` из `create()`, добавить reject
   неизвестных kwargs, обновить docstring'и.
2. Обновить `tests/test_application_context_role.py` под новое поведение
   reject.
3. Переработать CLI-часть `tests/test_profile_lifecycle.py`.
4. Удалить `tests/test_nanobot_profile_env_removed.py`, добавить
   name-agnostic static guard.
5. Перевести env-проверку в `tests/test_cli_agent_profile.py` на
   произвольные profile-like переменные.
6. Обновить активные спеки, документацию, `CHANGELOG.md` (новая запись
   в `[Unreleased]` — существующие записи не трогаются).
7. `pytest` + `openspec validate`.

**Rollback:** все изменения обратимы через `git checkout` — новых
миграций данных нет, схема не меняется, public API `config` не меняется.
Единственное необратимое — удалённые тесты восстанавливаются из
коммита.

---

## Дополнение 2026-10-02: почему дельты спеки удалены

Change выполнен по коду и закрыт, но `openspec archive` его не принимал:

```
configuration/profiles ADDED failed for header
"### Requirement: Application entrypoint profile contract различает
gateway и CLI" - already exists
```

Причина — та же, что у `startup-schema-validation`: обе дельты были
слиты в активные спеки вручную до применения инструментом.

Прежде чем удалять дельты, каждое требование сверено с активной спекой
построчно. Результат:

| Capability | Требование | Состояние |
|---|---|---|
| `runtime/entrypoints` | ADDED `ApplicationContext.create MUST NOT принимать profile` | идентично активной |
| `runtime/entrypoints` | ADDED `Production entrypoints MUST NOT передавать profile…` | идентично активной |
| `runtime/entrypoints` | MODIFIED `Deprecated kwargs с явной compatibility boundary` | идентично активной |
| `runtime/entrypoints` | MODIFIED `CLI имеет фиксированный профиль test` | идентично активной |
| `runtime/entrypoints` | MODIFIED `Единая typed signature… с role` | **тот же текст, иначе разбит на строки** |
| `configuration/profiles` | ADDED `Application entrypoint profile contract…` | в активной **дополнительный** сценарий `Streamlit invocation is explicitly defined`, которого в дельте нет |
| `configuration/profiles` | MODIFIED `Разрешение до инициализации runtime` | идентично активной |
| `configuration/profiles` | MODIFIED `Profiles are limited to a whitelist` | идентично активной |
| `configuration/profiles` | MODIFIED `CLI entrypoint имеет фиксированный профиль test` | текст требования идентичен; разницу давали следующие за ним секции `## Forbidden Behavior`, `## Dependencies` и далее, попавшие в тело требования при разборе |
| `configuration/profiles` | REMOVED `Application entrypoint requires --profile` | **уже отсутствует** в активной |
| `configuration/profiles` | REMOVED `All application entrypoints share identical lifecycle contract` | **уже отсутствует** в активной |

Итог: семь требований применены и совпадают дословно, ещё три расходятся
только переносами строк либо тем, что в активной версии **больше**, чем в
дельте (сценарий про Streamlit). REMOVED-требования уже удалены — их отказ
и давал сообщение `nothing to remove`.

Поэтому применение дельты **откатило бы** более поздние правки: потерялся
бы сценарий про Streamlit, а требования перезаписались бы устаревшими
переносами строк. Форма `MODIFIED` требует дословно повторить тело
требования целиком, то есть дельта стала бы второй копией нормативного
текста, расходящейся с активной спекой при каждой её правке.

Решение: дельты удалены, в `.openspec.yaml` выставлен `skip_specs: true` —
штатный механимент для change'а, который намеренно не трогает спеки.

### Проверка контракта по коду

Архивация не должна закрывать change, который на самом деле не выполнен,
поэтому контракт проверен по реальному коду, а не по чекбоксам:

1. `ApplicationContext.create` — параметров `script_dir`, `workspace_dir`,
   `role`, `storage_override`, `session_override`; `profile` в сигнатуре
   отсутствует.
2. Ни один вызов `create(...)` в production-коде (`lib/`, `tools/`,
   `gateway.py`, `cli_agent.py`) не передаёт `profile=` — проверено обходом
   AST, а не текстовым поиском.
3. `_resolve_enable_kwargs` отвергает любой ключ вне
   `DEPRECATED_ENABLE_KWARGS` через `TypeError`, поэтому `profile=` даёт
   `TypeError`, а не молчаливый игнор. Живая проверка подтвердила: и
   `profile=`, и опечатка `enable_aduit=` отвергаются.
4. `gateway.py` и `cli_agent.py` вызывают `config._initialize_settings(...)`
   раньше `ApplicationContext.create(...)` — профиль разрешается до
   composition root.
5. Живая проверка `create(role="gateway", …)` без `profile`: `ctx.profile`
   равен `test`, то есть значение пришло из `SETTINGS`.
