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

### По умолчанию (default = test, fail-safe)

```bash
python gateway.py
# или
python cli_agent.py
```

→ стартует в **test**-режиме. Если `profiles/test.jsonc` отсутствует →
`ConfigurationError: profiles/test.jsonc не найден. Создайте profiles/test.jsonc.`

### Явное указание test

```bash
NANOBOT_PROFILE=test python gateway.py
# или
python gateway.py --profile=test
```

### Prod

```bash
NANOBOT_PROFILE=prod python gateway.py
# или
python gateway.py --profile=prod
```

Prod = чистый `project.json` (никакого оверлея). Все runtime-таблицы
должны быть prod-именами — иначе `ConfigurationError` на старте.

### Streamlit

`streamlit_app.py` тоже принимает `--profile` / `NANOBOT_PROFILE`.

### Cron

Cron-процессы **не имеют CLI**. Они читают `NANOBOT_PROFILE` из env.
В systemd unit / docker-compose / k8s manifest обязательно задавайте
`NANOBOT_PROFILE=prod` для prod-деплоев.

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

## Миграция существующих деплоев

⚠️ **Breaking change**: `python gateway.py` без флагов теперь
запускается в test-режиме (раньше — в prod). Все существующие prod-деплои
**обязаны** явно указать профиль:

```yaml
# docker-compose.yml / k8s / systemd
environment:
  - NANOBOT_PROFILE=prod
```

или:

```bash
# CLI
python gateway.py --profile=prod
```

### Health-check (рекомендуется)

Добавьте в продовый мониторинг алерт на `Starting nanobot gateway …
profile=`. Если в проде видите `profile=test` — это ошибка деплоя.

## Что НЕ изолируется профилем

Эти вещи намеренно **общие** между prod и test (read-only):

- DSN (`channels.postgres.dsn`) — общая БД.
- Домен скилла (`oarb.audits`, `oarb.audit_reports` и т.п.) — read-only.
- Vector-storage (`oarb.audit_vectors`) — read-only.
- Реестры (`agent_predefined_scripts`) — read-only.

Если потребуется их изолировать (FAISS-пути, DuckDB-кеш, Streamlit-файлы,
cron-файл) — это **отдельная задача**. Текущий план их не затрагивает.

## Что меняется в runtime

**Меняется:**

| Слой | Prod (явно) | Test (default/явно) |
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

Merge-order тесты (см. `tests/test_config_resolver.py`) доказывают:

1. Профиль побеждает `session_manager.json` и `config.json`.
2. `validate_profile_overlay` блокирует посторонние ключи.
3. `validate_runtime_isolation` блокирует неправильные имена таблиц.
4. Отсутствие `profiles/test.jsonc` при mode=test — fail-fast.
5. Невалидное имя профиля (`foo bar`) — fail-fast.

## Архитектурная гарантия

> **Никто ниже `ConfigurationResolver` не знает слово "profile" /
> "test" / "prod".**

Можно проверить:

```bash
grep -rn 'profile.*==.*"test"\|profile.*==.*"prod"' lib/ workspace/
# Должно быть 0 результатов (или только в Resolver/CLI-parser)
```

## Резюме

```text
startup
   │
   ▼
mode = CLI --profile > NANOBOT_PROFILE env > default=test
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
SETTINGS (AttrDict)
   │
   ▼
ApplicationContext / Channel / Session / Logging / Agent / Skills
   ↓
Никто здесь не знает слово "profile"
```