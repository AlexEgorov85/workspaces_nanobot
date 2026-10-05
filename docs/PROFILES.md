# Профили конфигурации (prod / test)

## Концепция

Агент может стартовать в одном из двух режимов: **prod** или **test**.
Режим влияет только на построение конфигурации — после загрузки
runtime-таблиц остальная логика агента **не знает**, в каком режиме она
работает.

> **Режим существует только во время разрешения конфигурации. После
> получения разрешённого `SETTINGS` режим исчезает из runtime-модели.**

Это значит:

- В runtime-коде **нет** `if profile == "test"` / `if profile == "prod"`.
- Все компоненты получают настройки через `ApplicationContext.config`
  (`SETTINGS`), который уже содержит правильные значения.
- Если завтра появятся `dev` / `staging` — добавляются только
  `profiles/dev.jsonc` и т.п., **код агента не меняется**.

> **BREAKING в этой версии:** единственный источник профиля — CLI-флаг
> `--profile` в argv `application entrypoint`. Env vars, default-значения,
> и любой implicit-fallback для profile resolution удалены. После
> `import config` доступ к `SETTINGS` бросает `ConfigurationError`,
> пока `config._initialize_settings(profile)` не отработает.
> Подробности — `openspec/specs/configuration/profiles/spec.md` и
> `lib/utils/project_version.py`.

## Структура файлов

```
config.json               ← prod (база; специальный файл не нужен)
profiles/
    test.jsonc            ← test (только дельты от config.json)
session_manager.json      ← per-deploy override (опционально)
```

- **`config.json`** — базовая конфигурация. Без оверлея = prod.
- **`profiles/test.jsonc`** — оверлей для test. Содержит **только** 5
  profile-owned runtime-ключей (см. ниже). Любые другие ключи → fail-fast.
- **`session_manager.json`** — historical per-deploy override (pool,
  timeouts). Применяется на шаге 2 (после `config.json`, до профиля),
  поэтому **не может** перетереть profile-owned runtime-таблицы.

## Profile-owned runtime-настройки

Эти 5 ключей **immutable** после применения профиля:

| Роль | Канал | prod | test |
| --- | --- | --- | --- |
| `conversation_messages` | `channels.postgres.table_name` | `agent_conversation_messages` | `agent_conversation_messages_test` |
| `session_messages` | `channels.postgres.messages_table` | `agent_session_messages` | `agent_session_messages_test` |
| `session_meta` | `channels.postgres.meta_table` | `agent_session_meta` | `agent_session_meta_test` |
| `gateway_logs` | `logging.db.table_name` | `agent_gateway_logs` | `agent_gateway_logs_test` |
| `question_runs` | `logging.db.question_runs_table` | `agent_question_runs` | `agent_question_runs_test` |

В `profiles/test.jsonc` можно указать **только** эти 5 ключей. Никаких
`dsn`, `vector storage`, `skill data`. Иначе — `ConfigurationError` на
старте.

### Профиль на стороне платформы (enterprise-mcp)

Три из этих таблиц пишет **не агент, а платформа**: журнал и прогоны
вопросов уходят операциями `data.log_events` / `data.upsert_question_run`, очередь
задач — операциями `data.claim_task` / `complete` / `data.append_assistant_message`.
Поэтому одного `profiles/test.jsonc` мало: платформе тоже нужно знать, в каком
контуре она работает.

Агент передаёт платформе **только имя** контура — флагом
`--profile <имя>` в `gateway.agent.enterprise_mcp.args`, который
`client_from_settings` дописывает автоматически (только когда профиль не
`prod`). Значения имён таблиц **не передаются никогда**: они объявлены в
`mcp-platform/platform.json → profiles.<имя>`, и значение, присланное
вызывающей стороной, сделало бы вход в данные агента независимым от его
конфигурации — ровно тот дефект, который чинили в фазе 9 («окружение
приоритетнее файла»).

Оверлей платформы закрыт списком `PROFILE_OWNED_KEYS`
(`libs/enterprise_common/settings.py`): перекрывать можно только

| Ключ `platform.json` | prod | test |
| --- | --- | --- |
| `data.log_table` | `public.agent_gateway_logs` | `public.agent_gateway_logs_test` |
| `data.question_runs_table` | `public.agent_question_runs` | `public.agent_question_runs_test` |
| `data.task_table` | `public.agent_conversation_messages` | `public.agent_conversation_messages_test` |

Всё остальное (пул, LLM, эмбеддинги, снимок, индексы) профилем **не
разделяется** намеренно: это shared runtime resources, как и `cache.local_path`
у агента. Пул — тем более: его владелец один.

Три важных свойства:

- **Неизвестный профиль падает, а не берёт базу.** База — это боевые имена,
  и молчаливый откат означал бы, что тестовый контур пишет в боевой журнал;
  заметить это можно только по содержимому журнала.
- **Сверка на старте.** Оверлей объявлен в двух файлах, и правка одного без
  другого возможна. Сразу после рукопожатия агент берёт у платформы список
  таблиц, которые она реально проверяет (`data.schema_check` → поле `tables`) и
  сверяет со своими. Расхождение — `ConfigurationError` и отказ подниматься.
- **В очереди задач сверка полная.** `data.schema_check` раньше не включал
  `task_table` в проверяемые, хотя платформа им пользуется, — из-за этого
  профиль, перекрывший очередь, проверял бы наличие боевой таблицы.

### Что профилем НЕ разделяется

- Таблицы сессий (`messages_table` / `meta_table`) — платформа ими не
  пользуется, они целиком в ведении агента.
- Снимок DuckDB, векторные индексы, пул соединений, LLM/эмбеддинги.

## Запуск

### Lifecycle-gate (Phase A)

`SETTINGS` больше **не строится** на module-level. Импорт `import config`
делает чистый import — никакого merge, никакого env-чтения, никакого
default-профиля. `SETTINGS` публикуется **только** через
`config._initialize_settings(profile)`, вызываемый из application
entrypoint.

```text
process start
  ↓
application entrypoint (gateway.py / cli_agent.py)
  ↓
определение профиля по startup-контракту entrypoint:
  gateway.py      → argparse --profile (whitelist {"prod","test"}, обязателен)
  cli_agent.py    → фиксированный "test" (флаг не принимается)
  ↓
config._initialize_settings(profile)   ← единственная точка публикации
  ↓
ConfigurationResolver строит SETTINGS
  ↓
runtime imports / ApplicationContext
  ↓
channels / services / agent
```

Invariant: `SETTINGS` SHALL NOT be constructed during `import config`.
Доступ к `SETTINGS` без `_initialize_settings(...)` — `ConfigurationError`.

### Application entrypoints

Все три entrypoint'а следуют одному error lifecycle contract:
`ConfigurationError` поднимается validation-кодом, ловится в `main()`,
конвертируется в `sys.exit(2) + stderr FATAL`.

**Gateway:**

```bash
python gateway.py --profile=prod
python gateway.py --profile=test
```

`gateway.py --profile=prod --smoke` — smoke-режим: инициализирует
SETTINGS, печатает баннер + имя runtime-таблицы, выходит 0. Используется
только в integration-тестах; production — без `--smoke`.

**CLI agent** — фиксированный профиль `test`, флаг `--profile` НЕ принимается:

```bash
python cli_agent.py                    # REPL / smoke — профиль всегда test
python cli_agent.py --smoke            # smoke mode (см. выше)
python cli_agent.py --profile=test     # ОТКАЗ: ConfigurationError + exit 2
```

CLI — локальный test/dev entrypoint, а не production-deploy interface.
Фиксированный профиль убирает ложную универсальность и уменьшает
количество комбинаций для тестирования. `test` в CLI НЕ означает
урезанный runtime: тот же AgentLoop, Skills, Tools, DuckDB, Vector
search, Memory, Logging, Prompts, Runtime patches, что и в gateway.
Различие — только в profile и transport (CLI == in-memory bus).

**Streamlit удалён в фазе 1** миграции `enterprise-mcp-platform`: `streamlit_app.py`,
`lib/services/subprocess_manager.py` и секция `streamlit.*` из `config.json`
не существуют, как и `test_streamlit_app.py`. Живы два entrypoint — `gateway.py`
и `cli_agent.py`; оба получают профиль через `argv`.

### Без `--profile`

Поведение зависит от entrypoint:

- `gateway.py` без `--profile` падает с `exit 2` + stderr
  `"FATAL: --profile is required"`. Это deliberate fail-fast: оператор,
  набравший `python gateway.py` без флагов, должен явно выбрать профиль.
- `cli_agent.py` без `--profile` работает штатно — профиль `test`
  зафиксирован в коде entrypoint.

### Неподдерживаемый `--profile`

Для `gateway.py` любое значение вне `{"prod", "test"}` (например,
`--profile=dev`, `--profile=staging`, `--profile=foo`) —
`ConfigurationError` + exit 2. Whitelist закрытый; введение третьего
профиля — отдельный OpenSpec change. `cli_agent.py` отклоняет сам флаг
`--profile` независимо от значения.

### Environment не участвует в выборе профиля

**Environment не является источником профиля.** Никакая переменная
окружения — ни под историческим именем, ни под любым другим — не
участвует в выборе активного профиля. Профиль определяется только
явным startup-контрактом entrypoint: argv `--profile` для `gateway.py`
и фиксированный `test` для `cli_agent.py`.

Их игнорирование — это отсутствие кода, который их читает, а не
активный sanitization-механизм. Environment остаётся легитимным
каналом для **секретов**, `${VAR}`-подстановки и внешних URL — запрет
касается только выбора профиля.

Деплои должны передавать `--profile` через `command:` в
`docker-compose.yml` / k8s manifest / systemd unit / GitHub Actions.

## Профиль всегда приходит через argv

`--profile` передаётся **только** через argv, никогда через env vars: иначе
переменная окружения пережила бы перезапуск и «протекла» бы в чужой процесс.
Оба живых entrypoint держатся этого правила:

```python
# gateway.py — argparse --profile (whitelist {"prod","test"}, обязателен)
# cli_agent.py — фиксированный "test", флаг не принимается
```

**Историческая справка.** Правило было сформулировано, когда третий entrypoint
существовал: `gateway.py` spawn'ил `streamlit_app.py` через
`lib/services/subprocess_manager.py::spawn_streamlit` и передавал профиль так:

```python
proc = subprocess.Popen(
    [sys.executable, "-m", "streamlit", "run", str(script),
     "--server.headless", "true",
     "--server.port", str(port),
     "--", f"--profile={profile}"],   # SETTINGS["profile"] родителя
    ...
)
```

И `streamlit_app.py`, и `SubprocessManager` удалены в фазе 1 — код выше приведён,
чтобы было видно, откуда взялось требование «argv, а не env».

## Порядок merge (ConfigurationResolver)

```
1. config.json                     ← база
2. session_manager.json (если есть) ← per-deploy override
3. config.json                      ← nanobot-настройки
4. profiles/<mode>.jsonc            ← профиль (если mode != prod)
5. .secrets.env                     ← ${VAR} подстановка
6. validate_runtime_isolation()     ← hard-fail
```

**Ключевое:** profile overlay (шаг 4) — последний перед валидацией. Это
гарантирует, что profile-owned runtime-ключи **immutable после применения
профиля**. Даже если `session_manager.json` или `config.json` содержат
prod-имена — профиль их перетирает.

## Валидация (hard-fail, не warning)

### `validate_profile_overlay()`

Проверяет, что `profiles/<mode>.jsonc` содержит **только** 6
разрешённых runtime-ключей. Любой посторонний ключ (включая `dsn`,
`storage_table`, `tables`) → `ConfigurationError`.

### `validate_runtime_isolation()`

После всех merge-шагов проверяет **точное соответствие** runtime-таблиц
профилю:

- `mode=test` → все 6 имён должны быть `*_test` (точные).
- `mode=prod` → все 6 имён должны быть prod (точные, без `*_test`).

Суффикс `_test` сам по себе недостаточен: `foo_test` в test-режиме →
fail. Точное соответствие — единственный надёжный способ.

## Migration: env-based deploy → CLI-флаг

⚠️ **BREAKING.** Если ваш деплой передавал профиль через переменную
окружения (env-based способ передачи профиля), переведите его на
CLI-флаг. Ниже используется плейсхолдер `<PROFILE_ENV_VAR>` — имя
конкретной переменной не имеет значения: runtime не читает **никакую**
env-переменную для выбора профиля.

### docker-compose.yml

```yaml
# БЫЛО (больше не работает):
services:
  gateway:
    environment:
      - <PROFILE_ENV_VAR>=prod     # игнорируется runtime
    command: ["python", "gateway.py"]

# СТАЛО:
services:
  gateway:
    command: ["python", "gateway.py", "--profile=prod"]
    # никаких env vars для profile
```

### Kubernetes (Deployment / CronJob / StatefulSet)

```yaml
# БЫЛО:
spec:
  containers:
    - name: gateway
      env:
        - name: <PROFILE_ENV_VAR>
          value: "prod"
      command: ["python", "gateway.py"]
# СТАЛО:
spec:
  containers:
    - name: gateway
      command: ["python", "gateway.py", "--profile=prod"]
      # никаких env vars для profile
```

### systemd unit

```ini
# БЫЛО:
[Service]
Environment=<PROFILE_ENV_VAR>=prod
ExecStart=/usr/bin/python /opt/gateway/gateway.py
# СТАЛО:
[Service]
ExecStart=/usr/bin/python /opt/gateway/gateway.py --profile=prod
# никаких Environment= для profile
```

### GitHub Actions

```yaml
# БЫЛО:
- name: Start gateway (e2e)
  env:
    <PROFILE_ENV_VAR>: prod
  run: python gateway.py &
# СТАЛО:
- name: Start gateway (e2e)
  run: python gateway.py --profile=prod &
```

### Health-check в проде

Рекомендуется: добавьте в продовый мониторинг алерт на стартовый баннер
`profile=`. Если видите `profile=test` в проде — это ошибка деплоя.

## Что НЕ изолируется профилем

Эти вещи намеренно **общие** между prod и test (read-only):

- DSN (`channels.postgres.dsn`) — общая БД.
- Домен скилла (`oarb.audits`, `oarb.audit_reports` и т.п.) — read-only.
- Vector-storage (`oarb.audit_vectors`) — read-only.
- Реестры (`agent_predefined_scripts`) — read-only.

Если потребуется их изолировать (FAISS-пути, DuckDB-кеш,
cron-файл) — это **отдельная задача**. Текущий change их не затрагивает.

## Что меняется в runtime

**Меняется:**

| Слой | Prod (явно) | Test (явно) |
| --- | --- | --- |
| Баннер | `profile=prod` | `profile=test` |
| `channels.postgres.table_name` | `agent_conversation_messages` | `..._test` |
| `channels.postgres.messages_table` | `agent_session_messages` | `..._test` |
| `channels.postgres.meta_table` | `agent_session_meta` | `..._test` |
| `logging.db.table_name` | `agent_gateway_logs` | `..._test` |
| `logging.db.question_runs_table` | `agent_question_runs` | `..._test` |

**Не меняется:**

- DSN, skill data, vector storage, реестры (намеренно общие).
- Cron-файл, FAISS-пути, DuckDB-пути (out of scope).

## Тестирование

См. ``tests/test_profile_lifecycle.py`` (acceptance по tasks.md § D.1–D.5,
§ D.7) и ``tests/test_profile_integration.py`` (merge-order + resolver).

## Архитектурная гарантия

> **Никто ниже `ConfigurationResolver` не знает слово "profile" /
> "test" / "prod".**

Можно проверить:

```bash
grep -rn 'profile.*==.*"test"\|profile.*==.*"prod"' lib/ workspace/ tools/
# Должно быть 0 результатов (или только в CLI-parser)
```

> **Дополнительно:** больше нет module-level `SETTINGS = ...` в
> `config.py`. `import config` — чистый import без side-effects.
> `SETTINGS` — это `_LazySettings` proxy, который поднимается
> `ConfigurationError` на любом доступе до явного
> `config._initialize_settings(profile)` из application entrypoint.

## Резюме

```text
process start
   │
   ▼
argv
   │
   ▼
 application entrypoint (gateway.py / cli_agent.py)
   определяет профиль по своему startup-контракту
   (gateway: --profile, whitelist {"prod","test"}, обязателен;
    cli_agent: фиксированный "test", флаг не принимается)
   ↓
   ▼
config._initialize_settings(profile)      ← lifecycle-gate
   │
   ▼
ConfigurationResolver (config.py)
   │
   ├── 1) config.json
   ├── 2) session_manager.json (override ДО профиля)
   ├── 3) config.json
   ├── 4) profiles/<mode>.jsonc (ПРОФИЛЬ — последний)
   ├── 5) ${VAR} резолв
   └── 6) validate_runtime_isolation() — hard-fail
   │
   ▼
SETTINGS ("profile" обязательный ключ)
   │
   ▼
ApplicationContext / Channel / Session / Logging / Agent / Skills
   ↓
Никто здесь не знает слово "profile"
```
