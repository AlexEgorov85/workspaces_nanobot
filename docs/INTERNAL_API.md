# ⚙️ Внутренний API и конфигурация

> Навигационный индекс каталога `docs/` — в [`README.md`](README.md). Этот документ —
> самодостаточное описание подсистемы.

## ⚙️ Конфигурация `tools.exec` (запуск команд)

Секция `tools.exec` в `config.json` управляет инструментом `exec` (запуск shell-команд).
Реализация — `nanobot/agent/tools/shell.py` (`ExecTool`, `ExecToolConfig`), точка запуска
процесса — `ExecTool._spawn()` (shell.py:515), сборка окружения — `_build_env()`
(shell.py:695).

### Как процесс реально запускается

- **Windows**: `exec` не наследует окружение родителя целиком — запускается
  `pwsh`/`powershell` через `asyncio.create_subprocess_exec(..., env=env)` (shell.py:548).
  `env` — **минимальный** набор: `SYSTEMROOT`, `COMSPEC`, `USERPROFILE`, `TEMP`, `PATHEXT`,
  `PATH`, `PYTHONUNBUFFERED` и т.д. (shell.py:706), плюс переменные из `allowedEnvKeys`
  (shell.py:726).
- **Linux**: запускается `bash -c "<command>"` (shell.py:556). Linux-ветка `_build_env()`
  (shell.py:731) передаёт **ещё меньше**: только `HOME`, `LANG`, `TERM`, `PYTHONUNBUFFERED`
  + `allowedEnvKeys`. Родительский `PATH` и прочие переменные **не пробрасываются**.
- Привязка к конкретному Python-окружению — через `pathPrepend` + `allowedEnvKeys`
  (на Linux), либо `login: true` для pyenv/conda (bash с `-l` прочитает профиль юзера,
  shell.py:559).

### Параметры (JSONC `config.json`, `tools.exec`)

| Ключ | Тип / дефолт | Назначение |
|---|---|---|
| `enable` | `bool` (`true`) | Включает/отключает `exec`. `false` — модель не запускает команды (`ExecTool.enabled`, shell.py:176). |
| `timeout` | `int` (`60`) | Жёсткий таймаут команды в секундах; `0` — без лимита (`_resolve_timeout`, shell.py:400). Таймаут по вызову модели капится до 600 (shell.py:247), конфиговый капки не имеет. |
| `pathPrepend` | `str` (`""`) | Дополняет `PATH` в **начале**. Linux: инъекция `export PATH="<prepend>:$PATH"` в команду (`_wrap_path_export`, shell.py:502); Windows: дописывает в `env["PATH"]` (`_compose_path`, shell.py:474). |
| `pathAppend` | `str` (`""`) | Дополняет `PATH` в **конце** (`$NANOBOT_PATH_APPEND`). |
| `sandbox` | `str` (`""`) | Обёртка команды в песочницу через `wrap_command` (shell.py:31, 467). На Windows не поддерживается — логируется warning, запуск без песочницы (shell.py:460). |
| `allowedEnvKeys` | `list[str]` (`[]`) | Какие переменные из окружения родителя дописать в минимальное `env` субпроцесса. На Linux почти ничего не наследуется, поэтому сюда передают `VIRTUAL_ENV`, `PYTHONPATH`, секреты (`DATABASE_URL`) и т.д. Секреты вне списка в субпроцесс не попадают (изоляция, shell.py:703). |
| `allowPatterns` | `list[str]` (`[]`) | Regex-паттерны команд, **явно разрешённые**. Приоритет над `denyPatterns`. Если задан — команда выполняется только когда **каждый** топ-сегмент (`&&`, `||`, `;`, `|`) матчится под один из паттернов (shell.py:761). |
| `denyPatterns` | `list[str]` (`[]`) | Regex-паттерны запрещённых команд (RE-search по команде в нижнем регистре, shell.py:766). Добавляются к жёстко зашитому дефолтному списку (`rm -rf`, `del /f`, `mkfs`, `dd if=`, `shutdown`, fork bomb и т.д., shell.py:214-232). |

### Пример: привязка к конкретному venv (Linux)

```json
"exec": {
  "enable": true,
  "timeout": 120,
  "pathPrepend": "/home/user/venv/bin",
  "pathAppend": "",
  "sandbox": "",
  "allowedEnvKeys": ["DATABASE_URL", "VIRTUAL_ENV", "PYTHONPATH"],
  "allowPatterns": [],
  "denyPatterns": []
}
```

- `pathPrepend` → `python`/`pip`/`activate` резолвятся из `/home/user/venv/bin`.
- `allowedEnvKeys` → в субпроцесс попадают `VIRTUAL_ENV`, `PYTHONPATH`, `DATABASE_URL`.
- Для pyenv/conda, где PATH собирается в профиле, надёжнее `login: true` (bash с `-l`,
  shell.py:559) — но отдельного конфиг-ключа нет, нужна правка `ExecToolConfig`.

### Примеры `allowPatterns` / `denyPatterns`

- `allowPatterns`: `["^git .*", "^python .*", "^ls .*"]` — пропускает цепочки вида
  `git add . && python run.py` (оба сегмента матчатся); `python run.py && rm -rf x`
  **заблокируется**, т.к. `rm` нет в allowlist.
- `denyPatterns`: `["rm -rf /", "drop database", "curl http://"]` — запрещает конкретные
  команды в дополнение к встроенному списку.

---

## 🛠 Кастомные tool'ы (`workspace/tools/*.py`)

Кастомные tool'ы проекта следуют конвенциям **встроенного nanobot** —
без отдельного базового класса и без своей обёртки над `Tool`. Это
намеренно: чтобы добавить новый tool, нужно скопировать шаблон и
переименовать класс — как и для встроенных `ExecTool`/`ImageGenerationTool`.

### Контракт tool-класса

* Наследник `nanobot.agent.tools.base.Tool`.
* `config_key = "<name>"` — секция в `config.json` (`tools.<name>.*`).
* `config_cls()` возвращает pydantic-модель секции (`BaseModel`).
* `enabled(ctx)` читает `ctx.config.<name>.enable`.
* `create(ctx)` собирает инстанс через DI из `ToolContext` (см. ниже).
* `name` / `description` / `parameters` — стандартные абстрактные проперти.
* `async execute(...)` возвращает `str` или `ToolResult.error(...)`.

Reference: `nanobot/agent/tools/image_generation.py`
(`ImageGenerationTool` — самый полный пример) и
`workspace/tools/example.py` (минимальный шаблон).

### Где живут tool'ы

| Источник | Как подхватывается |
|---|---|
| **`workspace/tools/*.py`** | `RuntimePatcher.patch_project_tools` — auto-discover через `pkgutil.iter_modules` + `importlib.util.spec_from_file_location` (модуль грузится под именем `workspace.tools.<name>`, без зависимости от наличия `__init__.py`). |
| **Внешние pip-плагины** | `entry_points(group="nanobot.tools")` в `pyproject.toml` пакета. Встроенный `ToolLoader._discover_plugins` (`nanobot/agent/tools/loader.py:62`) подхватывает их автоматически. |
| **Тесты/явная регистрация** | `agent.tools.register(MyTool(...))` напрямую (для unit-тестов или особых сценариев DI). |

### `ToolContext` и DI

`RuntimePatcher.patch_project_tools` собирает `ToolContext` из полей
`AgentLoop` тем же способом, что `AgentLoop._register_default_tools`
(`loop.py:597-630`):

```python
ctx = ToolContext(
    config=agent.tools_config,                   # секции config.tools.*
    workspace=str(agent.workspace),
    bus=agent.bus,
    subagent_manager=agent.subagents,
    cron_service=agent.cron_service,
    exec_session_manager=agent._exec_session_manager,
    sessions=agent.sessions,
    file_state_store=agent.file_states,
    provider_snapshot_loader=agent.provider_snapshot_loader,
    image_generation_provider_configs=agent._image_generation_provider_configs,
    timezone=agent.context.timezone or "UTC",
    workspace_sandbox=agent.workspace_scopes.sandbox_status,
    runtime_events=agent.runtime_events,
)
setattr(ctx, "_agent_ref", agent)   # для tool'ов, которым нужен AgentLoop
```

В вашей версии nanobot `ToolContext.__init__` **не принимает `metadata`**,
поэтому `agent` пробрасывается отдельным атрибутом `_agent_ref`. Tool
получает его через `getattr(ctx, "_agent_ref", None)` (пример —
`CompactContextTool.create`).

### Окружение и лимиты

У tool'а **нет** своих встроенных политик (timeout, env-фильтр, sandbox)
— это in-process Python-coroutine в event-loop gateway/CLI. Если нужны
лимиты, оборачивайте вручную:

* `asyncio.wait_for(...)` — таймаут.
* Обрезка длинного вывода — общий `ContextGovernor.normalize_tool_result`
  (патч `patch_context_governor`) с лимитом `gateway.tool_result_limits.*`
  (см. `project.json` → `gateway.tool_result_limits.*`).
* Sandbox/allow-deny — если tool дёргает subprocess, наследуйте политики
  `tools.exec.*` через явный `subprocess.run` с собственными аргументами.

### Конфликты имён

`patch_project_tools` пропускает tool, если `agent.tools.get(name)`
уже возвращает не-`None` (т.е. встроенный loader его зарегистрировал
первым через `_register_default_tools`). Это страхует от случайного
затирания встроенных tool'ов.

### Пример: минимальный tool

```python
# workspace/tools/my_tool.py
from nanobot.agent.tools.base import Tool, tool_parameters
from pydantic import BaseModel


class MyToolConfig(BaseModel):
    enable: bool = True
    max_chars: int = 8_000


@tool_parameters({
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
})
class MyTool(Tool):
    config_key = "my_tool"

    @classmethod
    def config_cls(cls): return MyToolConfig

    @classmethod
    def enabled(cls, ctx): return ctx.config.my_tool.enable

    @classmethod
    def create(cls, ctx): return cls(config=ctx.config.my_tool)

    def __init__(self, *, config: MyToolConfig) -> None:
        self.config = config

    @property
    def name(self) -> str: return "my_tool"

    @property
    def description(self) -> str: return "Что делает tool (LLM видит это)."

    async def execute(self, *, text: str, **_kwargs):
        result = text.upper()
        return result[:self.config.max_chars]
```

Конфиг в `config.json`:

```jsonc
{
  "tools": {
    "myTool": {            // config_key="my_tool" → tools.my_tool в config
      "enable": true,
      "maxChars": 8000
    }
  }
}
```

### Отладка

`RuntimePatcher.apply_all` пишет результат `patch_project_tools` в
`PatchReport` (логируется через loguru): `"3 project tools
registered: foo, bar, baz; skipped: qux (disabled by config)"`.

Если tool не регистрируется — проверьте:
1. `cls.__module__` начинается с `workspace.tools.` (имена в
   `importlib.util.spec_from_file_location`).
2. `cls.enabled(ctx)` возвращает `True` для текущего конфига.
3. `agent.tools.get(tool.name)` возвращает `None` (нет коллизии
   с встроенным tool).
4. У класса нет `__abstractmethods__` (все абстрактные методы
   `Tool` реализованы).

### Зарегистрированные tool'ы проекта

| Tool | Файл | Действие | Конфиг |
|---|---|---|---|
| `compact_context` | `workspace/tools/compact_context.py` | ручное сжатие контекста | `gateway.compact.*` (project.json) |
| `duckdb_query` | `workspace/tools/duckdb_query_tool.py` | read-only SELECT-запрос в DuckDB-кэш | `gateway.duckdb_query.*` (project.json) |
| `vector_search` | `workspace/tools/vector_search_tool.py` | семантический поиск по FAISS-индексу | `gateway.vector_search.*` (project.json) |
| `history_search` | `workspace/tools/history_search_tool.py` | generic-поиск по журналу `agent_gateway_logs` (переживает context compaction) | `tools.history_search.*` (config.json; если секция не задана — дефолты модели `HistorySearchConfig`) |
| `legal_summarizer_query` | `workspace/tools/legal_summarizer_query.py` | follow-up по saved `operation_id` для `legal_summarizer` | `tools.legal_summarizer_query.*` (config.json) |
| `example_tool` | `workspace/tools/example.py` | шаблон (по умолчанию `enable=false`) | `tools.example.*` (config.json) |

`duckdb_query` и `vector_search` — generic infrastructure tools, не знают
конкретных Skills. Они используются skill'ом `audit_analyzer` через
процедуру, описанную в `SKILL.md` (см. TARGET_ARCHITECTURE.md §5, §6, §8).

`audit_run_predefined_script` / `audit_search_vector` / `audit_generate_sql`
**удалены** в рефакторинге `refactor/skills-tools-cleanup`
(коммиты `593d509`, `7d8f6b0`). Они нарушали §3, §22.1, §22.2
TARGET_ARCHITECTURE.md (импортировали skill через `importlib`); заменены на:

- predefined — CLI-режим skill'а (`scripts/cli.py --mode predefined`);
- vector search — tool `vector_search` (с указанием `index_name`);
- NL→SELECT — skill workflow с tool `duckdb_query` (см.
  `references/sql_guidance.md`);
- runtime-context providers (`providers.py`, инъекция схемы/predefined в system
  prompt) — удалены полностью: схема БД и списки скриптов теперь доступны
  по требованию через tool `duckdb_query` / `predefined/scripts.py`
  (реестр `REGISTRY` в памяти skill'а, не в PostgreSQL).

---

## 🚀 CLI навыка: режимы

Точка входа: `python scripts/cli.py` (кросс-платформенный).

```
audit_analyze --mode {predefined,generated_sql,vector} [опции]
```

| Режим | Назначение | Ключевые флаги |
|-------|-----------|----------------|
| `predefined` | Выполнение готовых SQL-шаблонов из реестра | `--script`, `--params` |
| `generated_sql` | Генерация SELECT через LLM по текстовому запросу | `--query`, `--context` |
| `vector` | Семантический поиск по FAISS-индексу | `--query`, `--index-name`, `--top-k`, `--threshold` |

Примеры:

```bash
# predefined — готовый шаблон с параметрами
audit_analyze --mode predefined --script violations_by_period --params '{"date_from": "2024-01-01", "date_to": "2024-12-31"}'

# generated_sql — генерация SQL через LLM и выполнение
audit_analyze --mode generated_sql --query 'сколько аудитов было в 2024 по месяцам'

# vector — топ-3 по схожести
audit_analyze --mode vector --query 'пожарная безопасность' --index-name audits_index --top-k 3

# vector — всё выше порога 0.7
audit_analyze --mode vector --query 'статусы аудитов' --index-name audits_index --threshold 0.7
```

**Как выбирается бэкенд запросов:** CLI строит провайдера
(`build_cache_provider()`), открывает опубликованный gateway DuckDB-снапшот
(путь через `table_registry.snapshot_path()`) на чтение и работает по нему.
Прямого PostgreSQL-бэкенда у CLI нет (см. [DATABASE.md](DATABASE.md)). Кеш создаёт и обновляет
**gateway** (см. [DATABASE.md](DATABASE.md#-жизненный-цикл-кеша)); CLI про это не знает. Если файла
кеша нет — CLI завершается с `FileNotFoundError`: «Кеш создаёт и обновляет
gateway автоматически — запустите его (python gateway.py)».

Векторный поиск — параметр `--index-name` (по умолчанию `audits_index`).
Строковые параметры predefined-скриптов передаются как есть (после
валидации `ParameterValidator`), без семантического резолва через векторный
поиск.

---

## 🛠 tools/ — инфраструктурные утилиты

В корне `tools/` живут CLI-утилиты, **отдельные от навыков** — инфраструктура, не аналитика.

### `tools/build_vectors.py`

Перестроение векторных индексов из PostgreSQL-данных. **Полная документация — в [Векторная индексация](VECTOR_INDEXES.md)**, включая:

- как добавить/обновить/удалить индекс,
- формат `embedding_cols` (с чанкованием и без),
- алгоритм сборки и классификации NEW/CHANGED/DELETED,
- типичные проблемы и их решения,
- мониторинг через SQL-запросы.

Краткая шпаргалка по флагам:

```bash
# Статус без изменений
python tools/build_vectors.py --status

# Полная перестройка всех индексов (осторожно: долго + нагрузка на Ollama)
python tools/build_vectors.py --full-rebuild

# Только проверка сигнатуры (COUNT + MAX track_column) — для cron
python tools/build_vectors.py --check

# Один индекс
python tools/build_vectors.py --index audits_index

# Dry-run без записи в БД
python tools/build_vectors.py --dry-run

# Параметры эмбеддинга (пауза между запросами + ожидание перед повтором при ошибке)
python tools/build_vectors.py --batch-size 32 --chunk-size 500 --chunk-overlap 80
python tools/build_vectors.py --pause-sec 3 --embedding-retry-wait 5

# Другая таблица векторов
python tools/build_vectors.py --db-table my_app.vectors

# Подробный лог (DEBUG): конфиг, каждый чанк/строка — для диагностики
python tools/build_vectors.py --verbose
```

| Флаг | Дефолт | Описание |
|------|--------|----------|
| *(без флагов)* | — | Инкрементальная синхронизация (NEW / CHANGED / DELETED) |
| `--full-rebuild` | — | Полная перестройка (TRUNCATE индекса + все строки) |
| `--check` | — | Сравнить сигнатуру (count distinct pk + max track); синхронизировать только при diff |
| `--status` | — | Сводное состояние индексов без синхронизации |
| `--dry-run` | — | План без записей в БД |
| `--index <name>` | все | Собрать только индекс `name` |
| `--db-table` | `oarb.audit_vectors` | Таблица сырых векторов |
| `--batch-size` | 10 | Батч эмбеддинга |
| `--chunk-size` | 500 | Размер чанка в символах |
| `--chunk-overlap` | 80 | Перекрытие чанков |
| `--pause-sec` | 5.0 | Пауза между батчами эмбеддинга (сек) |
| `--embedding-retry-wait` | 5 | При ошибке получения эмбеддинга: ждать это время (сек) и повторить один раз |
| `--verbose` | — | Подробный лог каждого чанка/строки (уровень DEBUG) |

**Логирование.** Все сообщения идут через `loguru` в stderr (без ANSI-цветов,
удобно при `>> build.log 2>&1`) и разбиты по этапам: конфиг → состояние
БД/источника → классификация (новые/изменённые/удалённые) → удаление →
чанки → эмбеддинг с прогрессом → пересборка FAISS → итог. Ошибка любого
этапа печатается с traceback, поэтому падение без причины маловероятно;
сбой отдельного индекса не роняет весь прогон (фиксируется в сводке `ИТОГО`).

**Гарантии инкрементальной сборки:**
- `pk_value` сравнивается как строка (`TEXT` в БД vs числовой PK в источнике
  нормализуются через `_norm_pk`) — детект CHANGED/DELETED работает, а не
  переписывает индекс на каждом запуске.
- CHANGED-строки: сначала вставляются новые чанки, старые удаляются
  **после** успешной вставки (`DELETE ... content_hash <> <new>`). Если
  эмбеддинг упал — старый вектор сохраняется (без потери данных).
- Быстрая проверка `--check` использует `COUNT(DISTINCT pk_value)`, поэтому
  чанкование (несколько чанков на строку) не заставляет `--check` всегда
  запускать синхронизацию.

**Важно:** при первом запуске проверить, что установлены зависимости FAISS:
`pip install faiss-cpu numpy`. Без них вектора вставляются в `audit_vectors`,
но `public.agent_vector_index_store` остаётся пустой, и `--mode vector` поиск
через `lib/services/cache_provider_impl.py` не работает.

**Типичные сценарии:**
- **После изменений в DDL таблиц** — `--full-rebuild`.
- **Проверка готовности системы** (cron / healthcheck) — `--check`.
- **Мониторинг без записи** — `--status`.
- **Большой источник + экономия памяти Ollama** — `--batch-size 8` + `--chunk-size 300`.

## ➕ Добавление новой настройки

Если вы вводите новый параметр, который раньше был литералом в коде, следуйте правилу:

1. **Объявите ключ в `project.json`** (JSONC, с дефолтом и комментарием) — в подходящей секции (`channels.*`, `skills.*`, `cli`, `gateway`, `logging.db` и т.п.).
2. **Для обязательных настроек навыка `audit_analyzer` используйте
   `config.py` (`require_setting` → `ConfigurationError`)**
   — это единый источник правды без литералов в коде. Для необязательных —
   `config.get_setting(*keys, default=...)`. **Не хардкодьте литерал.**
3. **Добавьте ключ в `REQUIRED_KEYS` в `tests/test_config_keys.py`** — иначе CI не поймает случайное удаление/переименование.
4. **Перезапустите gateway / CLI** после правки `project.json`.

Пример (вынос `max_stuck_retries`):

```json
// project.json
"channels": {
  "postgres": {
    "max_stuck_retries": 3   // Лимит retry зависшего сообщения
  }
}
```

```python
# lib/channels/postgres_channel.py
from config import get_setting
max_retries = get_setting("channels", "postgres", "max_stuck_retries", default=3)
```

```python
# tests/test_config_keys.py → REQUIRED_KEYS
("channels.postgres.max_stuck_retries", 3),
```

Используйте `get_setting()` для вложенных ключей с дефолтом; `SETTINGS.x.y.z` — для горячего чтения без дефолта (если ключ гарантированно есть). Избегайте `cfg.get("key", "default")` без явного пути — это маскирует orphan-ключи.

