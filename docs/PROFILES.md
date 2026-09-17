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
> Подробности — `openspec/changes/config-profile-cli-flag/` (текущий
> change) и `lib/utils/project_version.py`.

## Структура файлов

```
project.json              ← prod (база; специальный файл не нужен)
profiles/
    test.jsonc            ← test (только дельты от project.json)
session_manager.json      ← per-deploy override (опционально)
```

- **`project.json`** — базовая конфигурация. Без оверлея = prod.
- **`profiles/test.jsonc`** — оверлей для test. Содержит **только** 6
  profile-owned runtime-ключей (см. ниже). Любые другие ключи → fail-fast.
- **`session_manager.json`** — historical per-deploy override (pool,
  timeouts). Применяется на шаге 2 (после `project.json`, до профиля),
  поэтому **не может** перетереть profile-owned runtime-таблицы.

## Profile-owned runtime-настройки

Эти 6 ключей **immutable** после применения профиля:

| Роль | Канал | prod | test |
| --- | --- | --- | --- |
| `conversation_messages` | `channels.postgres.table_name` | `agent_conversation_messages` | `agent_conversation_messages_test` |
| `session_messages` | `channels.postgres.messages_table` | `agent_session_messages` | `agent_session_messages_test` |
| `session_meta` | `channels.postgres.meta_table` | `agent_session_meta` | `agent_session_meta_test` |
| `worker_claims` | `channels.postgres.claims_table` | `agent_worker_claims` | `agent_worker_claims_test` |
| `gateway_logs` | `logging.db.table_name` | `agent_gateway_logs` | `agent_gateway_logs_test` |
| `question_runs` | `logging.db.question_runs_table` | `agent_question_runs` | `agent_question_runs_test` |

В `profiles/test.jsonc` можно указать **только** эти 6 ключей. Никаких
`dsn`, `vector storage`, `skill data`. Иначе — `ConfigurationError` на
старте.

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
application entrypoint (gateway.py / cli_agent.py / streamlit_app.py)
  ↓
argparse парсит --profile (whitelist {"prod","test"})
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

**CLI agent:**

```bash
python cli_agent.py --profile=prod
python cli_agent.py --profile=test
python cli_agent.py --profile=test --smoke   # smoke mode (см. выше)
```

**Streamlit:**

```bash
streamlit run streamlit_app.py -- --profile=prod
streamlit run streamlit_app.py -- --profile=test
```

`streamlit_app.py` парсит `--profile` из `sys.argv` (всё после `--`
streamlit-run пробрасывает как позиционные элементы). При первом запуске
вызывается `_initialize_settings(profile)`; `st.rerun()` повторно
вызывает module-level statements, но guard через `globals()`
предотвращает второй вызов lifecycle-gate.

### Без `--profile`

Каждый entrypoint без `--profile` падает с `exit 2` + stderr
`"FATAL: --profile is required"`. Это deliberate fail-fast: разработчик,
набравший `python gateway.py` без флагов, должен явно выбрать профиль.

### Неподдерживаемый `--profile`

Любое значение вне `{"prod", "test"}` (например, `--profile=dev`,
`--profile=staging`, `--profile=foo`) — `ConfigurationError` + exit 2.
Whitelist закрытый; введение третьего профиля — отдельный OpenSpec change.

### Environment fallback

**Не существует.** Любая устаревшая переменная окружения для передачи
профиля (исторически — `NANOBOT_PROFILE`) **не читается runtime-кодом**.
Приложение просто не работает с такими переменными; их игнорирование —
это отсутствие кода, который их читает, а не активный sanitization
механизм. Деплои должны передавать `--profile` через `command:` в
`docker-compose.yml` / k8s manifest / systemd unit / GitHub Actions.

## Application subprocess получает `--profile` через argv

Когда `gateway.py` spawn'ит `streamlit_app.py`, профиль передаётся в
argv child (НЕ в env). Это контракт — никаких env vars:

```python
# lib/services/subprocess_manager.py:spawn_streamlit
proc = subprocess.Popen(
    [sys.executable, "-m", "streamlit", "run", str(script),
     "--server.headless", "true",
     "--server.port", str(port),
     "--", f"--profile={profile}"],   # SETTINGS["profile"] родителя
    ...
)
```

`streamlit_app.py` получает свой `--profile` из argv и инициализирует
SETTINGS через тот же lifecycle-gate.

## Порядок merge (ConfigurationResolver)

```
1. project.json                     ← база
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

⚠️ **BREAKING.** Если ваш деплой до сих пор использовал env-based
передачу профиля (исторически — `NANOBOT_PROFILE=prod` в
`docker-compose.yml` / k8s manifest / systemd unit / GitHub Actions),
переведите его на CLI-флаг.

### docker-compose.yml

```yaml
# БЫЛО (больше не работает):
services:
  gateway:
    environment:
      - NANOBOT_PROFILE=prod     # игнорируется runtime
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
        - name: NANOBOT_PROFILE
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
Environment=NANOBOT_PROFILE=prod
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
    NANOBOT_PROFILE: prod
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

Если потребуется их изолировать (FAISS-пути, DuckDB-кеш, Streamlit-файлы,
cron-файл) — это **отдельная задача**. Текущий change их не затрагивает.

## Что меняется в runtime

**Меняется:**

| Слой | Prod (явно) | Test (явно) |
| --- | --- | --- |
| Баннер | `profile=prod` | `profile=test` |
| `channels.postgres.table_name` | `agent_conversation_messages` | `..._test` |
| `channels.postgres.messages_table` | `agent_session_messages` | `..._test` |
| `channels.postgres.meta_table` | `agent_session_meta` | `..._test` |
| `channels.postgres.claims_table` | `agent_worker_claims` | `..._test` |
| `logging.db.table_name` | `agent_gateway_logs` | `..._test` |
| `logging.db.question_runs_table` | `agent_question_runs` | `..._test` |

**Не меняется:**

- DSN, skill data, vector storage, реестры (намеренно общие).
- Cron-файл, FAISS-пути, DuckDB-пути, Streamlit-файлы (out of scope).

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
application entrypoint (gateway.py / cli_agent.py / streamlit_app.py)
   parses --profile (whitelist {"prod","test"}, required)
   ↓
   ▼
config._initialize_settings(profile)      ← lifecycle-gate
   │
   ▼
ConfigurationResolver (config.py)
   │
   ├── 1) project.json
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
