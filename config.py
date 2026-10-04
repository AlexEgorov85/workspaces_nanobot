"""Единая точка конфигурации агента.

Файл настроек ОДИН: ``config.json`` (строгий JSON — читается и штатным
``json.loads`` в ``ConfigService._pre_resolve_env_refs``, и схемой
nanobot). Бывший второй файл ``project.json`` (JSONC с комментариями)
ликвидирован; его секции переехали сюда, а его комментарии — сюда же, в
этот docstring: JSON без комментариев, объяснения живут в коде.

    Читается config.py и мержится в SETTINGS в порядке
    (поздний перекрывает ранний):

        config.json → session_manager.json → .secrets.env
                    → profiles/<mode>.jsonc → ${VAR} → hard-fail

    Значения вида ``${VAR}`` подставляются из ``os.environ``
    (секреты — из ``.secrets.env``, факты о запуске — из
    ``_export_runtime_env``).

    Правило добавления новой настройки:
      1. Объявить ключ в ``config.json`` (с дефолтом).
      2. В коде читать через ``get_setting(*keys, default=...)`` или
         ``SETTINGS.*``.
      3. Добавить запись в ``_required_keys()`` в
         ``tests/test_config_keys.py``.

ГДЕ ЛЕЖИТ СЕКЦИЯ
================

Схема nanobot (``nanobot.config.schema.Config``) отвергает неизвестные
ключи в КОРНЕ ``config.json``, но молча игнорирует их внутри ``channels``
и ``gateway``. Отсюда раскладка:

* ``agents``/``providers``/``api``/``transcription``/``tools``/
  ``modelPresets`` — нативные настройки nanobot, без изменений.
* ``channels.*`` — каналы связи (Postgres, Redis, пулы). Плюс общий для
  всех каналов ``document_text_threshold``: при превышении в
  user-промпт кладётся маркер
  ``[File: <basename> — text omitted (len=… > threshold=…); read at <path>]``
  вместо полного текста документа (защита от раздува контекста).
* ``gateway.*`` — сервер, перезапуск, subprocess'ы, память, compact,
  usage_store, session_cold_sync, vector, print_*.
  Ключей ``host``/``port`` здесь нет и быть не должно: gateway не поднимает
  HTTP-сервер. Health/Readiness — не эндпойнт, а вычисляемое по запросу
  состояние (``ctx.runtime_health`` / ``ctx.runtime_readiness``), см.
  ``lib/services/runtime_health.py``. Порт websocket-канала задаёт сам канал.
* ``gateway.agent.{project,cli,skills,logging,enterprise_mcp}`` — пять
  секций, которым в корне места нет (см. ``AGENT_SECTIONS``).
  ``_lift_agent_sections`` поднимает их в корень **до** всех остальных
  шагов merge, поэтому ``profiles/<mode>.jsonc`` и ``session_manager.json``
  видят обычный плоский вид и ничего не знают про namespace.

``channels.postgres``
---------------------

* ``unstick_interval`` — фоновая петля возврата зависших задач в
  ``processing`` в исходный статус. По дефолту
  ``max(60, processing_timeout/5)`` = 120 сек. Уменьшает нагрузку на БД
  на пустом столе (вместо SELECT+UPDATE каждые ``poll_interval``).
  ВНИМАНИЕ: ветка повтора в ``_claim_one`` недостижима — внешний
  ``AND status='pending'`` отсекает ``status='error'``. Настройка
  сохранена как контракт ``_mark_failed`` (см. CHANGELOG).
* Ветка захвата задачи: ``UPDATE ... RETURNING`` с внешним
  ``AND status = 'pending'`` — состояние захвата в самой строке задачи,
  отдельного lease-протокола нет.

``gateway.agent.skills.*``
--------------------------

Каждый skill объявляется одной JSON-секцией со стандартным набором
полей (``lib/core/project_settings.py::SkillSettings``). Никакого
``register.py`` и реестра ресурсов не требуется: снятый ``table_registry``
был владельцем этих объектов, а состав снимка и индексов объявляет платформа
(``mcp-platform/platform.json`` → ``audit.tables`` и ``vectors.indexes``).
Чтобы добавить новый skill:

  1. Добавить секцию ``gateway.agent.skills.<name>`` с массивом ``tables``.
  2. Если нужны векторные индексы — добавить ``vector_indexes``.
  3. Готово: skill подхватится на старте gateway без правок кода.

Секции (все OPTIONAL): ``enabled`` (default: true), ``tables``,
``vector_indexes``, ``cli.*`` (параметры CLI навыка), ``llm.*``
(execution policy). Выбор модели/провайдера — в настройках nanobot,
вне ``skills.*``.

ГРАНИЦА ``skills.*``: только то, что меняется при смене ДОМЕНА skill'а.
Общая runtime-инфраструктура — снаружи: FAISS backend/storage/root →
``gateway.vector.index.*``; storage-таблицы индексов — там же. Правило
описано в TARGET_ARCHITECTURE §skills.* boundary.

Структура ``tables`` (``project_settings.py::TableEntry``):

* ``name`` — ОБЯЗАТЕЛЬНО, формат ``"schema.table"``.
* ``type`` — ``"table"`` (по умолчанию) | ``"vector"``.
* ``label`` — OPTIONAL opaque-метка. Таблица с label НЕ попадает в
  описание схемы для LLM. Типичный кейс — реестры метаданных из других схем
  (``public.agent_predefined_scripts`` с ``label="scripts_registry"``). В
  агенте метку больше никто не читает: разбор объявления делает платформа,
  ``mcp-platform/servers/enterprise/server.py::_audit_config``, и запись с
  меткой уходит в каталог скриптов, а не в доменные таблицы. Runtime-sync
  игнорирует.
* ``tracking_column`` — OPTIONAL колонка для инкрементального поллинга;
  дефолт ``updated_at`` для ``type="table"``, ``id`` для ``type="vector"``.
* Элемент может быть строкой ``"schema.table"`` или объектом с полями
  выше. Неизвестные ключи в объекте запрещены (``extra="forbid"``) —
  fail-fast на опечатках.

ВНИМАНИЕ: ``skills.<name>`` имеет ``extra="forbid"``. Любой неизвестный
ключ в skill-секции вызовет ``ConfigurationError`` на старте gateway
(опечатка ``tablse`` или оставшийся от старой версии ``embedding``) —
сознательное ужесточение контракта.

Секции ``gateway.sync.*`` (поллинг, очередь записей, reconnect-бэкофф)
УДАЛЕНЫ: фоновой синхронизации больше нет, загрузка кэша — разовая
операция при старте процесса.

``skills.<name>.vector_indexes[*]``: ``name`` — логическое имя индекса
(как его видит tool ``vector_search``). Source-таблица берётся из объявления
индекса (``config.json → gateway.vector.index.indexes``); прежний PG-реестр
``public.agent_vector_index_config`` кодом больше не читается (см. ниже про
``vector.*``), backend и путь хранения — ``gateway.vector.index.*``.

``skills.audit_analyzer``: навык tool-only (никакого CLI), поэтому
секции ``cli.*``/``llm.*`` ему не нужны. ``legal_summarizer`` — секция
удалена 2026-10-02 (фаза 11, п. 11.6): домен целиком живёт на платформе
(``mcp-platform/libs/legal_summarizer`` и capability
``mcp-platform/servers/enterprise/capabilities/legal_summarizer``), агент
ходит в него операцией ``query_operation``, объявленной в белом списке
``config.json → tools.mcpServers.enterprise.enabled_tools``. Собственного
tool'а-обёртки у агента больше нет: ``workspace/tools/legal_summarizer_query.py``
снят change'ом ``2026-10-03-mcp-native-tools`` (п. D6), поэтому настройки
``tools.legal_summarizer_query`` в ``config.json`` тоже сняты — читать их
было некому. Настройки домена живут в ``mcp-platform/platform.json`` →
``legal_summarizer``.

``gateway.*``
-------------

* ``storage`` — ``auto`` (холодное зеркало PostgreSQL при наличии dsn,
  иначе только JSONL) | ``postgres`` (зеркало обязательно, без dsn — ошибка) |
  ``file`` (только JSONL, dsn игнорируется). Менеджер сессий во всех режимах —
  класс библиотеки ``SessionManager`` поверх ``SanitizingSessionStore``;
  PostgreSQL обслуживает подсистема зеркала сессий
  (``lib/gateway/mirror/``), а не сам
  менеджер.
* ``tool_result_limits.*`` — секция УДАЛЕНА вместе с патчами ``exec_limits``
  и ``tool_limits`` (``lib/services/runtime_patcher.py``). Потолки вывода
  инструментов вернулись к дефолтам nanobot; настраивать их в конфиге
  больше нечем, поэтому ключи оставлены бы без читателя. Что именно
  изменилось — в докстринге ``lib/services/runtime_patcher.py``.
* ``compact.*`` — ручное сжатие контекста сессии
  (``lib/services/context_compaction.py``,
  ``workspace/tools/compact_context.py``).
* ``usage_store.*`` — upstream ``LLMUsageStore`` (metadata-only usage);
  дефолтный путь ``<get_runtime_subdir("usage")>/usage.db``,
  ``enabled=false`` отключает observer (graceful degradation).
* ``repeat_guard.*`` (``lib/hooks/repeat_guard_hook.py``) — защитник от
  вырожденных циклов одинаковых tool-вызовов, секция полностью
  опциональна: без неё хук работает в ``mode="off"``. Ключи:
  ``mode`` (off|warn|block), ``window_size``,
  ``max_repeats_in_window``, ``exempt_tools`` (ТОЧНЫЕ имена, без * и ?).
  ``mode=block`` поднимает ``RepeatGuardBlocked``, которую перехватывает
  патч ``repeat_guard_block`` и превращает в обычный синтетический
  tool-результат — оборот при этом не падает
  (``openspec/specs/runtime/anti-loop/spec.md``).
* ``session_cold_sync.*`` — фоновый mirror upstream JSONL → PG;
  ``enabled=false`` отключает sync (escape hatch для multi-instance).
* ``error_messages.*`` — заготовленные ответы при internal-ошибке
  ``AgentLoop._process_message``
  (``openspec/specs/runtime/error-fallback``): ``internal_error`` — текст
  вместо upstream-литерала "Sorry, I encountered an error." (детали
  исключения в content НЕ попадают, только в ``agent_gateway_logs``),
  ``log_to_db`` — писать ли ``event_type="turn_failed"`` (default: true).
  Дефолты — ``ErrorMessagesSettings`` и ``_DEFAULT_INTERNAL_ERROR_TEXT``
  в ``lib/services/runtime_patcher.py``.
* ``vector.*`` — общая runtime-инфраструктура эмбеддингов и индексов.
  PG-реестр ``public.agent_vector_index_config`` больше НЕ читается
  кодом. Параметры подключения к эмбеддеру (Ollama ``/api/embed``,
  ``EMBED_TOKEN`` в ``.secrets.env``) — константы capability ``llm`` на
  платформе, прямые значения в конфиге не нужны.
  ``index.storage_table`` — единая PG-таблица-хранилище сырых
  эмбеддингов (``register_infra``). ``index.default_root`` —
  DEPRECATED (FAISS собирается в памяти из снапшота storage_table).
  ``index.signature_table`` УДАЛЁН (change
  ``remove-vector-index-store``). Ключи опциональны; дефолты — в
  ``VectorIndexSettings``/``VectorIndexConfig``.
  УСТАРЕВШИЙ ПУТЬ ``gateway.vector_index.*`` УДАЛЁН — используйте
  ``gateway.vector.index.*`` (единственный канонический путь).

``gateway.agent.logging.db`` — структурированный журнал агента
(``DbLoggingService``); таблицы — profile-owned (см.
``PROFILE_OWNED_RUNTIME_KEYS``).

``gateway.agent.enterprise_mcp``
--------------------------------

Объявление читает клиент агента
(``lib/services/enterprise_mcp_client.py``) — для фоновых служб, которые
ходят в платформу ВНЕ оборота: воркер очереди канала, зеркало сессий на
холодное хранилище, журнал через ``log_events`` и запись о сжатии контекста.
Там нужен клиент, а не инструмент модели: оборота, а значит и
``mcp_enterprise_*``, в этот момент нет.

ВТОРОЕ объявление того же сервера — ``config.json → tools.mcpServers``:
его читает штатный провайдер MCP, и по нему операции попадают к модели как
``mcp_enterprise_*`` с их настоящими ``inputSchema``. Поэтому процессов
платформы два, и это объявлено, а не вышло случайно. Объединить их можно
снятием клиента агента — отдельной задачей, а не правкой конфига.

Штатный провайдер зовёт ``session.call_tool(name, arguments=kwargs)`` и не
умеет передавать ``_meta``: параметра ``meta=`` в этом вызове нет и штатной
точки, чтобы его добавить, тоже нет. Поэтому личность вызова доезжает плоскими
ключами в аргументах, а платформа читает их переходным путём
(``platform.json → execution.require_call_meta = false``) и вырезает до
вызова домена. Подстановкой занимается ``McpIdentityHook``.

``${NANOBOT_PYTHON}`` и ``${NANOBOT_PROJECT_ROOT}`` подставляются из
``os.environ`` (``_export_runtime_env``): в конфиге нет ни одного пути
конкретной машины, и сервер поднимается тем же Python, в котором
установлены его зависимости. Имя контура и порог журнала едут в дочерний
процесс значениями ``NANOBOT_ENTERPRISE_MCP_PROFILE`` и
``NANOBOT_ENTERPRISE_MCP_LOG_MIN_LEVEL`` (``_export_platform_process_env``).

``DATABASE_URL`` серверу НЕ нужен из окружения агента: capability ``data``
собирает DSN сам из ``mcp-platform/.secrets.env``. Именно поэтому объявление
в ``tools.mcpServers`` передаёт дочернему процессу минимальный ``env`` — при
``env=None`` MCP SDK отдаёт серверу безопасное подмножество окружения, и
``NANOBOT_WORKSPACE`` пришлось бы объявить явно.
"""

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

_ROOT_DIR = Path(__file__).parent
#: ЕДИНСТВЕННЫЙ файл настроек агента: и настройки nanobot, и агентские
#: секции. Отдельного ``project.json`` больше нет (см. модульный docstring).
_CONFIG_FILE = _ROOT_DIR / "config.json"
_SECRETS_FILE = _ROOT_DIR / ".secrets.env"
_SESSION_MANAGER_FILE = _ROOT_DIR / "session_manager.json"
_PROFILES_DIR = _ROOT_DIR / "profiles"

#: Секции агента, которым в корне ``config.json`` места нет.
#:
#: Корневой объект ``config.json`` разбирает СХЕМА nanobot
#: (``nanobot.config.loader.load_config``), и она отвергает любой
#: неизвестный ключ верхнего уровня. Проверено на 0.3.5: ``logging``,
#: ``cli``, ``skills``, ``project``, ``enterprise_mcp`` в корне дают
#: ``ConfigLoadError: Unknown setting``, то есть ломают
#: ``ConfigService.load()`` (а значит и старт gateway/CLI). Агенту нужна
#: ровно одна копия каждой секции, поэтому эти пять живут в файле под
#: ``gateway.agent.*`` — неизвестные ключи ВНУТРИ ``gateway`` схема
#: игнорирует молча (extra="ignore" у ``nanobot.config_base.Base``) — и
#: поднимаются обратно в корень функцией :func:`_lift_agent_sections`.
#:
#: Пути в ``SETTINGS`` при этом не меняются (``SETTINGS["cli"]``,
#: ``SETTINGS["logging"]``, ``SETTINGS["skills"]``,
#: ``SETTINGS["project"]``, ``SETTINGS["enterprise_mcp"]``), поэтому ни
#: один потребитель кроме самого ``config.py`` не правится.
AGENT_SECTIONS: frozenset[str] = frozenset({
    "project",
    "cli",
    "skills",
    "logging",
    "enterprise_mcp",
    # Корень файлов сессии. Секция названа так же, как и единственный
    # потребитель объявления — ``lib/services/session_files.py``, — потому что
    # читатель и раздел объявления обязаны называться одним: раздел без
    # читателя — настройка без владельца, а читатель без раздела — корень,
    # посчитанный агентом в обход объявления платформы.
    "session_files",
})

#: Путь внутри ``config.json``, где физически лежат ``AGENT_SECTIONS``.
AGENT_SECTIONS_PATH: tuple[str, ...] = ("gateway", "agent")


_SUPPORTED_PROFILES = frozenset({"prod", "test"})

#: Путь к порогу журнала в дереве настроек. Объявлен здесь, а не у потребителя,
#: потому что читателей стало трое: писатель журнала агента
#: (``lib/core/application_context.py``), клиент MCP фоновых служб
#: (``lib/services/enterprise_mcp_client.py``) и экспорт для второго процесса
#: платформы, который поднимает нанобот (``_export_platform_process_env``).
#: Ключ поднят в корень из ``gateway.agent`` функцией ``_lift_agent_sections`` —
#: читать надо корень ``logging``, а не ``gateway.agent.logging``: второго пути
#: к тому же значению быть не должно.
#: Страж единого источника — ``tests/test_journal_threshold_single_source.py``.
JOURNAL_MIN_LEVEL_PATH: tuple[str, ...] = ("logging", "db", "min_level")


class AttrDict(dict):
    def __getattr__(self, name):
        try:
            val = self[name]
            return AttrDict(val) if isinstance(val, dict) else val
        except KeyError:
            raise AttributeError(name)

    def __setattr__(self, name, val):
        self[name] = val


def _parse_value(val: str):
    if not val or not isinstance(val, str):
        return val
    v = val.strip()
    if v.lower() in ("true", "yes"):
        return True
    if v.lower() in ("false", "no"):
        return False
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    if v.startswith(("{", "[")):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            pass
    if "," in v:
        parts = [p.strip() for p in v.split(",") if p.strip()]
        if len(parts) > 1:
            return parts
    return v


def _header_to_prefix(header: str) -> list[str]:
    text = header.lstrip("#").strip().lower().replace("-", "_")
    return [p.strip() for p in text.split(":", 1) if p.strip()]


def load_env(path: str | Path | None = None) -> AttrDict:
    env_file = Path(path or _SECRETS_FILE)
    if not env_file.exists():
        return AttrDict()

    tree = {}
    prefix: list[str] = []

    for line in env_file.read_text(encoding="utf-8").splitlines():
        line_stripped = line.strip()
        if not line_stripped:
            continue
        if (
            line_stripped.startswith("#")
            and "=" not in line_stripped
            and ":" in line_stripped.lstrip("#")
        ):
            prefix = _header_to_prefix(line_stripped)
            continue
        if "=" not in line_stripped or line_stripped.startswith("#"):
            continue
        key, _, raw = line_stripped.partition("=")
        keys = prefix + key.strip().split("__")
        d = tree
        for k in keys[:-1]:
            d = d.setdefault(k, {})
        d[keys[-1]] = _parse_value(raw.strip())

    return AttrDict(tree)


def _strip_jsonc_comments(text: str) -> str:
    """Удалить ``//`` и ``/* */`` комментарии из JSON (JSONC), не трогая строки.

    Сохраняет содержимое строковых литералов (включая ``https://...``),
    корректно обрабатывает экранирование ``\\"``.
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    in_block = False
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if in_block:
            if c == "*" and nxt == "/":
                in_block = False
                i += 2
                continue
            i += 1
            continue
        if in_string:
            out.append(c)
            if c == "\\" and nxt:
                out.append(nxt)
                i += 2
                continue
            if c == '"':
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
            out.append(c)
            i += 1
            continue
        if c == "/" and nxt == "/":
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if c == "/" and nxt == "*":
            in_block = True
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def load_config_json(path: str | Path | None = None) -> AttrDict:
    """Загрузить JSON/JSONC-файл в AttrDict; несуществующий/битый файл → пустой AttrDict.

    Поддерживает комментарии ``//`` и ``/* */`` (JSONC) — их используют
    ``profiles/<mode>.jsonc``. ``config.json`` — строгий JSON (его же
    читает штатный ``json.loads`` в ``ConfigService``), но парсится тем
    же кодом.
    """
    config_file = Path(path or _CONFIG_FILE)
    if not config_file.exists():
        return AttrDict()
    try:
        raw = config_file.read_text(encoding="utf-8")
    except OSError:
        return AttrDict()
    raw = _strip_jsonc_comments(raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return AttrDict()
    return AttrDict(data) if isinstance(data, dict) else AttrDict()


def _lift_agent_sections(cfg: dict) -> None:
    """Поднять ``gateway.agent.*`` из ``config.json`` в корень ``cfg``.

    Вызывается сразу после загрузки ``config.json`` и ДО остальных шагов
    merge, поэтому ``session_manager.json``, ``.secrets.env`` и
    ``profiles/<mode>.jsonc`` видят обычный плоский вид и ничего не знают
    про namespace.

    Неизвестная секция внутри ``gateway.agent`` — ``ConfigurationError``:
    иначе опечатка в имени секции молча игнорировалась бы, а это ровно
    тот класс дефекта (настройка объявлена, но не действует), который
    ликвидация второго файла и устраняет.
    """
    node: Any = cfg
    for part in AGENT_SECTIONS_PATH[:-1]:
        node = node.get(part) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            return
    agent = node.pop(AGENT_SECTIONS_PATH[-1], None)
    if not isinstance(agent, dict):
        return
    unknown = sorted(set(agent) - AGENT_SECTIONS)
    if unknown:
        raise ConfigurationError(
            f"{'.'.join(AGENT_SECTIONS_PATH)} в config.json содержит "
            f"неизвестные секции: {unknown}. Разрешены только "
            f"{sorted(AGENT_SECTIONS)}."
        )
    for name, value in agent.items():
        if name in cfg:
            raise ConfigurationError(
                f"config.json объявляет секцию {name!r} дважды: в корне и "
                f"под {'.'.join(AGENT_SECTIONS_PATH)}. Копия настроек "
                f"должна быть ровно одна."
            )
        cfg[name] = value


def _deep_merge(base: dict, override: dict) -> None:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


# ---------------------------------------------------------------------------
# ConfigurationResolver — единая точка формирования SETTINGS
# ---------------------------------------------------------------------------
#
# Главный принцип: профиль (``prod``/``test``) задаётся явно через
# ``_initialize_settings(profile)``, вызванный из application entrypoint
# (argv ``--profile``). После успешной инициализации ``SETTINGS``
# доступен через mapping-proxy ``_LazySettings``. До инициализации
# доступ к ``SETTINGS`` бросает ``ConfigurationError`` — никаких
# дефолтов и module-level ``SETTINGS = ...``.
#
# Порядок merge (поздний перекрывает ранний):
#   1. config.json                     — база (и настройки nanobot,
#                                         и агентские секции)
#   2. session_manager.json (если есть) — per-deploy override (pool/timeouts)
#   3. .secrets.env (${VAR})           — секреты
#   4. profiles/<mode>.jsonc           — профиль (если mode != prod)
#   5. validate_runtime_isolation()    — hard-fail
#
# Ключевое: profile overlay идёт ПОСЛЕДНИМ, поэтому profile-owned runtime-ключи
# (channels.postgres.{table_name,messages_table,meta_table} и
# logging.db.{table_name,question_runs_table}) — immutable после применения
# профиля. Даже если session_manager.json содержит prod-имена, профиль их
# перетирает.
# ---------------------------------------------------------------------------

PROFILE_OWNED_RUNTIME_KEYS = frozenset({
    ("channels", "postgres", "table_name"),
    ("channels", "postgres", "messages_table"),
    ("channels", "postgres", "meta_table"),
    ("logging",  "db",       "table_name"),
    ("logging",  "db",       "question_runs_table"),
    # NB (change ``unify-cli-gateway-architecture``, design D11/Stage 7):
    # ``gateway.cache.local_path`` MUST NOT быть profile-owned ключом —
    # это shared runtime resource, единый физический путь для gateway
    # и CLI вне зависимости от профиля. Per-profile override ловится
    # ``validate_profile_overlay`` через reject «extra keys» ниже.
})

EXPECTED_RUNTIME_TABLE_NAMES: dict[str, dict[str, str]] = {
    "prod": {
        "conversation_messages": "agent_conversation_messages",
        "session_messages":      "agent_session_messages",
        "session_meta":          "agent_session_meta",
        "gateway_logs":          "agent_gateway_logs",
        "question_runs":         "agent_question_runs",
    },
    "test": {
        "conversation_messages": "agent_conversation_messages_test",
        "session_messages":      "agent_session_messages_test",
        "session_meta":          "agent_session_meta_test",
        "gateway_logs":          "agent_gateway_logs_test",
        "question_runs":         "agent_question_runs_test",
    },
}


def runtime_table(role: str, profile: str = "prod") -> str:
    """Имя runtime-таблицы по роли — читать отсюда, а не писать литералом.

    ``role`` — ключ из :data:`EXPECTED_RUNTIME_TABLE_NAMES`
    (``conversation_messages``, ``session_messages``, ``session_meta``,
    ``gateway_logs``, ``question_runs``).

    Единственная причина существования функции: имя таблицы меняется в
    конфигурации, и код/тесты, зашившие литерал, молча поедут мимо нового
    имени. Здесь — чтение объявления, а не вторая копия.

    Неизвестный профиль или роль — ``ConfigurationError``: подставлять
    «что-нибудь» здесь нельзя, это ровно тот класс дефекта, который
    функция закрывает.
    """
    tables = EXPECTED_RUNTIME_TABLE_NAMES.get(profile)
    if tables is None:
        raise ConfigurationError(
            f"runtime_table: неизвестный профиль {profile!r}. "
            f"Допустимые: {sorted(EXPECTED_RUNTIME_TABLE_NAMES)}."
        )
    name = tables.get(role)
    if not name:
        raise ConfigurationError(
            f"runtime_table: роль {role!r} не объявлена для профиля "
            f"{profile!r}. Доступные: {sorted(tables)}."
        )
    return name


class ConfigurationError(ValueError):
    """Ошибка конфигурации: обязательный ключ отсутствует или некорректен.

    В отличие от ``get_setting`` (возвращает переданный ``default``),
    ``require_setting`` выбрасывает эту ошибку, чтобы отсутствие настройки
    не маскировалось подставным значением. Единственный источник правды —
    ConfigurationResolver.

    Объявлён ДО ``_initialize_settings`` / ``resolve_application_config`` —
    иначе ошибки конфигурации на module-level импорте превращались бы в
    ``NameError: ConfigurationError is not defined``.
    """


def _load_session_manager_override() -> dict:
    """Прочитать session_manager.json (если есть) ДО profile overlay.

    Это сохраняет историческую роль per-deploy override для pool/timeout,
    но НЕ ДАЁТ ему перетирать runtime-таблицы — профиль идёт позже
    (см. порядок merge в начале секции).
    """
    if not _SESSION_MANAGER_FILE.exists():
        return {}
    data = json.loads(_SESSION_MANAGER_FILE.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _load_secrets_override() -> dict:
    """Прочитать ``.secrets.env`` (если есть) — содержит секреты для
    ``${VAR}`` плейсхолдеров (``DATABASE_URL``, ``EMBED_TOKEN`` и т.п.)
    и провайдерские ``api_key`` через секцию ``# providers: llm``.

    Содержимое файла попадает в две точки:
      * в ``cfg`` через deep_merge (для ``providers.llm.api_key``,
        используемых кодом напрямую через SETTINGS);
      * в ``os.environ`` через ``_export_secrets_to_env`` (для резолва
        ``${VAR}`` в ``_resolve_env_refs``).

    Без этого шага все ``${DATABASE_URL}``/``${EMBED_TOKEN}`` и т.п.
    остались бы нерезолвнутыми — агенту был бы передан ``${VAR}`` literal.
    """
    if _SECRETS_FILE is None or not _SECRETS_FILE.exists():
        return {}
    data = load_env(_SECRETS_FILE)
    return dict(data) if isinstance(data, dict) else {}


def _export_secrets_to_env(cfg: dict) -> None:
    """Экспорт «плоских» значений из ``cfg`` в ``os.environ``.

    Делается ДО ``_resolve_env_refs`` — чтобы ``${VAR}`` нашёл свои
    значения. ``setdefault`` — внешние ``os.environ`` имеют приоритет.
    """
    for key, val in _flatten_env(cfg).items():
        if "${" not in str(val):
            os.environ.setdefault(key, val)


def _export_runtime_env() -> None:
    """Экспорт фактов о запуске в ``os.environ`` — для резолва ``${VAR}``.

    Нужны MCP-серверу, который запускает сам агент. Объявление сервера
    живёт в ``config.json`` в единственном экземпляре, и оттуда берутся
    интерпретатор и корень платформы. Без этого в конфиге пришлось бы
    зашить абсолютные пути конкретной машины, а сервер поднялся бы не
    тем Python, в котором установлены его зависимости.

    Третий факт — ``NANOBOT_WORKSPACE``, рабочий каталог агента, — нужен уже
    не объявлению сервера, а ``platform.json``: файлы сессии обязаны лежать
    внутри него, иначе граница файловых инструментов агента (а она включена
    по умолчанию в потоке записи) откажет в записи. Он выводится от корня
    проекта тем же приёмом, каким точками входа выводится ``workspace_dir``:
    иначе каталог, объявленный платформой, и каталог, куда реально пишет
    агент, разошлись бы на одном компьютере.

    ``setdefault`` — внешнее окружение имеет приоритет: другой
    интерпретатор может быть указан осознанно.
    """
    os.environ.setdefault("NANOBOT_PYTHON", sys.executable)
    os.environ.setdefault("NANOBOT_PROJECT_ROOT", str(_ROOT_DIR))
    os.environ.setdefault("NANOBOT_WORKSPACE", str(_ROOT_DIR / "workspace"))


def _export_platform_process_env(cfg: dict, profile: str) -> None:
    """Экспортировать в ``os.environ`` факты для дочернего процесса платформы.

    Нужны объявлению ``config.json → tools.mcpServers.enterprise``: его ``args``
    — это список, а не строка, и подстановкой ``${VAR}`` внутрь списка можно
    передать только ЗНАЧЕНИЕ. Поэтому имя контура и порог журнала едут в
    процесс платформы значениями, а не флагами, которые агент дописывает
    руками.

    **Это не второй источник профиля.** Значения выводятся из уже
    разрешённых ``profile`` и ``cfg`` и присваиваются БЕЗ ``setdefault``:
    внешнее окружение переопределить их не может, поэтому единственным
    источником остаётся ``--profile`` у application entrypoint (change
    ``remove-profile-environment-selection``). Историческое имя
    ``NANOBOT_PROFILE`` не воскрешается.

    Пустое значение — не мусор, а «флага нет»: платформа разбирает
    ``--profile <пусто>`` как отсутствие флага и берёт базовый контур
    (``mcp-platform/servers/enterprise/server.py::_profile_from_argv``
    возвращает ``None``). То же и с порогом журнала.
    """
    node: object = cfg
    for key in JOURNAL_MIN_LEVEL_PATH:
        node = node.get(key) if isinstance(node, dict) else None
        if node is None:
            break
    min_level = str(node).strip() if isinstance(node, str) else ""
    if profile and profile != "prod":
        os.environ["NANOBOT_ENTERPRISE_MCP_PROFILE"] = profile
    else:
        os.environ["NANOBOT_ENTERPRISE_MCP_PROFILE"] = ""
    os.environ["NANOBOT_ENTERPRISE_MCP_LOG_MIN_LEVEL"] = min_level


def _merge_profile_overlay(cfg: dict, mode: str) -> None:
    """Применить profiles/<mode>.jsonc как ПОСЛЕДНИЙ шаг перед валидацией.

    Для prod — no-op (prod это чистый config.json).
    Для test — требуется файл profiles/test.jsonc.
    """
    if mode == "prod":
        return
    overlay = _PROFILES_DIR / f"{mode}.jsonc"
    if not overlay.exists():
        raise ConfigurationError(
            f"profiles/{mode}.jsonc не найден. "
            f"Создайте profiles/{mode}.jsonc."
        )
    overlay_cfg = load_config_json(overlay)
    if isinstance(overlay_cfg, AttrDict):
        overlay_cfg = dict(overlay_cfg)
    validate_profile_overlay(overlay_cfg, mode)
    _deep_merge(cfg, overlay_cfg)


def validate_profile_overlay(overlay_cfg: dict, mode: str) -> None:
    """Hard-fail: profiles/<mode>.jsonc симметрично проверяется на:

      * все 5 profile-owned runtime-ключей ОБЯЗАНЫ присутствовать;
      * никаких посторонних ключей (только эти 5 разрешены).

    Симметричная проверка даёт чёткий контракт самого файла оверлея:
    невалидный profile.jsonc ловится здесь, а не только на
    финальной ``validate_runtime_isolation`` (которая страхует итог,
    но не сам файл).
    """
    allowed = PROFILE_OWNED_RUNTIME_KEYS

    def _walk(node: object, path: tuple, found: set) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                _walk(v, path + (k,), found)
        else:
            found.add(path)

    found: set = set()
    _walk(overlay_cfg, (), found)

    missing = allowed - found
    if missing:
        raise ConfigurationError(
            f"profiles/{mode}.jsonc не содержит обязательных "
            f"profile-owned runtime-ключей: {sorted(missing)}. "
            f"Все 5 ключей обязательны: {sorted(allowed)}."
        )

    extra = found - allowed
    if extra:
        raise ConfigurationError(
            f"profiles/{mode}.jsonc содержит ключи, которые профиль "
            f"не имеет права менять: {sorted(extra)}. "
            f"Разрешены только 5 profile-owned runtime-ключей: "
            f"{sorted(allowed)}."
        )


def validate_runtime_isolation(cfg: dict, mode: str) -> None:
    """Hard-fail: точное соответствие runtime-таблиц профилю."""
    if mode not in EXPECTED_RUNTIME_TABLE_NAMES:
        raise ConfigurationError(
            f"validate_runtime_isolation: неизвестный mode={mode!r}. "
            f"Допустимые: {list(EXPECTED_RUNTIME_TABLE_NAMES)}."
        )
    expected = EXPECTED_RUNTIME_TABLE_NAMES[mode]
    pg = cfg.get("channels", {}).get("postgres", {}) if isinstance(cfg, dict) else {}
    log = cfg.get("logging", {}).get("db", {}) if isinstance(cfg, dict) else {}
    actual = {
        "conversation_messages": (pg.get("table_name", "") if isinstance(pg, dict) else ""),
        "session_messages":      (pg.get("messages_table", "") if isinstance(pg, dict) else ""),
        "session_meta":          (pg.get("meta_table", "") if isinstance(pg, dict) else ""),
        "gateway_logs":          (log.get("table_name", "") if isinstance(log, dict) else ""),
        "question_runs":         (log.get("question_runs_table", "") if isinstance(log, dict) else ""),
    }
    bad = [(role, actual[role], expected[role])
           for role in expected if actual[role] != expected[role]]
    if bad:
        lines = "\n".join(
            f"  {role}: actual={a!r}, expected={e!r}"
            for role, a, e in bad
        )
        raise ConfigurationError(
            f"profile={mode!r}: runtime-таблицы не соответствуют ожидаемым:\n"
            f"{lines}\n"
            f"Возможная причина: profiles/{mode}.jsonc отсутствует или "
            f"содержит prod-имена, либо session_manager.json/config.json "
            f"перекрывают profile-owned ключи (это должно быть "
            f"невозможно после применения профиля)."
        )


ENV_REF_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _resolve_env_refs(value):
    """Рекурсивно заменить ``${VAR}`` на значение из os.environ.

    Неизвестная переменная оставляется как есть (ленивый режим, как
    resolve_env_refs у nanobot) — импорт не должен падать без секрета.
    """
    if isinstance(value, str):
        return ENV_REF_PATTERN.sub(
            lambda m: os.environ.get(m.group(1), m.group(0)), value
        )
    if isinstance(value, dict):
        return AttrDict({k: _resolve_env_refs(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_resolve_env_refs(v) for v in value]
    return value


def _flatten_env(d: dict, prefix: str = "") -> dict[str, str]:
    """Рекурсивно «расплющить» вложенный dict в плоский
    ``{KEY_CHILD_...: str(value)}`` для экспорта в ``os.environ``.

    Содержимое ``${VAR}``-плейсхолдеров пропускается (их нельзя
    выставлять в env как литералы — на следующем проходе резолва они
    могут перезаписать внешние значения).

    Объявлена ДО ``resolve_application_config`` — иначе вызов
    ``_export_secrets_to_env`` на module-level import падал бы с
    ``NameError: _flatten_env is not defined``.
    """
    result: dict = {}
    for k, v in d.items():
        p = f"{prefix}_{k}" if prefix else k
        if isinstance(v, dict):
            result.update(_flatten_env(v, p))
        else:
            result[_(p).upper()] = str(v)
    return result


def _(s: str) -> str:
    """Sanitize-преобразование имени env-переменной."""
    return s.replace(" ", "_").replace("-", "_")


def resolve_application_config(profile: str) -> AttrDict:
    """Единая точка формирования SETTINGS.

    Используется внутри ``_initialize_settings(profile)`` после проверки
    whitelist ``{"prod", "test"}``. Возвращает ``AttrDict``. Profile
    передаётся как **обязательный** явный аргумент — никакого env,
    default'а или mode=None. Это точка, в которой профиль становится
    частью runtime.

    Порядок merge (поздний перекрывает ранний):
      1. config.json                     — база: и настройки nanobot,
                                          и агентские секции; поднимается
                                          ``gateway.agent.*`` в корень
                                          (``_lift_agent_sections``)
      2. session_manager.json (если есть) — per-deploy override
      3. .secrets.env                     — секреты (``DATABASE_URL``,
                                          провайдерские ``api_key``)
      4. profiles/<mode>.jsonc           — профиль (если mode != prod)
      5. ${VAR} резолв через os.environ
      6. validate_runtime_isolation()    — hard-fail
    """
    cfg: dict = {}
    if _CONFIG_FILE.exists():
        config_data = load_config_json(_CONFIG_FILE)
        if isinstance(config_data, AttrDict):
            config_data = dict(config_data)
        _deep_merge(cfg, config_data)
        # Секции без места в корне схемы nanobot — наверх, до всех
        # остальных шагов merge (см. AGENT_SECTIONS).
        _lift_agent_sections(cfg)

    _deep_merge(cfg, _load_session_manager_override())

    # Секреты из .secrets.env после config.json (чтобы могли перекрыть
    # то, что в config.json, при необходимости). До профиля (профиль —
    # только runtime-таблицы; секреты идут в cfg как обычные ключи).
    _deep_merge(cfg, _load_secrets_override())

    # Экспорт secrets в os.environ ДО _resolve_env_refs — чтобы ${VAR}
    # в config.json нашли свои значения.
    _export_secrets_to_env(cfg)

    # Факты о запуске — тоже до резолва: ими заполняются ${NANOBOT_PYTHON}
    # и ${NANOBOT_PROJECT_ROOT} в объявлении MCP-сервера, а
    # ${NANOBOT_WORKSPACE} — в platform.json, дочерний процесс наследует
    # os.environ целиком.
    _export_runtime_env()

    # Контур и порог журнала для ВТОРОГО объявления того же сервера —
    # ``tools.mcpServers`` (нанобот поднимает процесс сам). Источник тот же,
    # что и у клиента агента, объявление — другое.
    _export_platform_process_env(cfg, profile)

    _merge_profile_overlay(cfg, profile)

    cfg = _resolve_env_refs(cfg)

    validate_runtime_isolation(cfg, profile)

    return AttrDict(cfg)


# ---------------------------------------------------------------------------
# Lifecycle-gate: _initialize_settings + _LazySettings proxy
#
# SETTINGS публикуется ТОЛЬКО через ``_initialize_settings(profile)``,
# вызванный из application entrypoint (argv ``--profile``). Никакого
# module-level ``SETTINGS = ...`` больше нет: ``import config`` ничего
# не инициализирует. До явного вызова ``SETTINGS`` отдаёт
# ``ConfigurationError`` на любой ``__getitem__``/``__getattr__``/``.get``.
#
# Контракт и архитектурное обоснование — в
# ``openspec/changes/config-profile-cli-flag`` (proposal/design/tasks).
# ---------------------------------------------------------------------------


class _LazySettings:
    """Compatibility proxy для ``SETTINGS``.

    Состояния: UNINITIALIZED (пустой ``_inner_dict``) и INITIALIZED
    (заполненный ``_inner_dict`` — ``AttrDict``, построенный
    ``resolve_application_config``). Переключение — только через
    ``_initialize_settings(profile)``; двойная инициализация бросает
    ``ConfigurationError``.

    Поддерживает mapping-access (``SETTINGS["profile"]``) для нового
    кода и attribute-access (``SETTINGS.profile`` — через ``__getattr__``)
    для backward-compat с существующим кодом
    (``history_search_tool.py`` и т.п.).
    """

    __slots__ = ("_inner_dict",)

    def __init__(self) -> None:
        self._inner_dict: AttrDict | None = None

    def _ensure_initialized(self) -> AttrDict:
        if self._inner_dict is None:
            raise ConfigurationError(
                "SETTINGS not initialized: call _initialize_settings(profile) "
                "from the application entrypoint"
            )
        return self._inner_dict

    def __getitem__(self, key: str) -> Any:
        return self._ensure_initialized()[key]

    def __setitem__(self, key: str, value: Any) -> None:
        """``SETTINGS[k] = v`` для legacy-тестов, мутирующих proxy in-place.

        Допустимо только в INITIALIZED state — UNINITIALIZED proxy не
        имеет inner_dict для мутации. Бросает ту же ``ConfigurationError``,
        что и ``__getitem__``, чтобы не маскировать lifecycle-ошибки.
        """
        self._ensure_initialized()[key] = value

    def __delitem__(self, key: str) -> None:
        self._ensure_initialized()
        del self._inner_dict[key]

    def __getattr__(self, name: str) -> Any:
        # ``__slots__`` доступ через object.__getattribute__; проксируем только
        # атрибуты дочернего dict (mapping-стиль), включая ``get``.
        if name == "_inner_dict":
            raise AttributeError(name)
        inner = self._ensure_initialized()
        try:
            return inner[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __contains__(self, key: str) -> bool:
        if self._inner_dict is None:
            return False
        return key in self._inner_dict

    def __iter__(self):
        return iter(self._ensure_initialized())

    def __len__(self) -> int:
        return len(self._ensure_initialized())

    def __repr__(self) -> str:
        if self._inner_dict is None:
            return "<_LazySettings UNINITIALIZED>"
        return f"<_LazySettings INITIALIZED profile={self._inner_dict.get('profile')!r}>"

    def get(self, key: str, default: Any = None) -> Any:
        """Mapping-style ``.get`` — используется в некоторых существующих путях
        (``SETTINGS.get('channels', {})``); при UNINITIALIZED бросает ту же
        ``ConfigurationError``, что и ``__getitem__``. ``default`` не
        маскирует ошибку инициализации (это несовместимо с
        инвариантом «профиль инициализируется entrypoint'ом до любого
        runtime-импорта»).
        """
        inner = self._ensure_initialized()
        return inner.get(key, default)

    def items(self):
        return self._ensure_initialized().items()

    def keys(self):
        return self._ensure_initialized().keys()

    def values(self):
        return self._ensure_initialized().values()


SETTINGS: Any = _LazySettings()


def _initialize_settings(profile: str) -> None:
    """Lifecycle-gate: единственная точка публикации ``SETTINGS``.

    Args:
        profile: ``"prod"`` или ``"test"`` (только whitelist).

    Raises:
        ConfigurationError:
            * если ``_initialize_settings`` уже был вызван в этом процессе
              — **сначала проверяется lifecycle state**, и только потом
              whitelist. Любое значение второго аргумента →
              ``"already initialized"`` (даже невалидный профиль вроде
              ``"dev"`` не пройдёт через эту проверку, чтобы не маскировать
              факт уже-инициализированного state — defensive programming);
            * если ``profile`` не из whitelist ``{"prod", "test"}``
              (выполняется ТОЛЬКО на первом вызове, до публикации SETTINGS);
            * если ``SETTINGS`` proxy corrupted (неожиданный тип
              — не должно случаться в runtime, диагностическая защита).

    Поведение при ошибке валидации merge/resolver
    (``profiles/<mode>.jsonc`` отсутствует, runtime-таблицы не
    соответствуют и т.п.) — также ``ConfigurationError`` (пробрасывается
    из ``resolve_application_config``), runtime-импорты не выполняются.
    """
    settings = SETTINGS
    if not isinstance(settings, _LazySettings):
        raise ConfigurationError(
            "SETTINGS proxy corrupted: expected _LazySettings instance"
        )
    if settings._inner_dict is not None:
        # Lifecycle wins: даже невалидный профиль ("dev", "foo bar")
        # даёт "already initialized", а не "is not supported". Это
        # предотвращает маскировку уже-инициализированного state за
        # ошибкой whitelist. См. spec § D.1 + tasks.md D.3.
        raise ConfigurationError(
            "SETTINGS already initialized: _initialize_settings(profile) "
            "may be called only once per process"
        )
    if not isinstance(profile, str) or profile not in _SUPPORTED_PROFILES:
        raise ConfigurationError(
            f"profile={profile!r} is not supported "
            f"(allowed: {', '.join(sorted(_SUPPORTED_PROFILES))})"
        )

    cfg = resolve_application_config(profile)
    # Профиль должен быть доступен в SETTINGS как ``SETTINGS["profile"]``
    # (canonical API) независимо от того, что лежит в config.json.
    if isinstance(cfg, dict):
        cfg["profile"] = profile
    settings._inner_dict = cfg

    # Провайдерский LLM_API_KEY setdefault'ом (см. исторический блок ниже).
    _providers = cfg.get("providers", {}) or {}
    if isinstance(_providers.get("llm"), dict):
        _llm_key = _providers["llm"].get("api_key") or _providers["llm"].get("apiKey")
        if _llm_key and isinstance(_llm_key, str) and not _llm_key.startswith("${"):
            os.environ.setdefault("LLM_API_KEY", _llm_key)


def is_settings_initialized() -> bool:
    """``True`` после успешного ``_initialize_settings(profile)``.

    Используется в ``tests/test_standalone_failfast.py`` и для
    diagnostic checks. Не должно читаться runtime-кодом как «профиль
    известен» — для этого есть ``SETTINGS["profile"]``.
    """
    return isinstance(SETTINGS, _LazySettings) and SETTINGS._inner_dict is not None


def get_active_profile() -> str:
    """Вернуть активный профиль (``SETTINGS["profile"]``).

    Бросает ``ConfigurationError``, если ``_initialize_settings`` ещё
    не вызван. Заменяет старую module-level ``_ACTIVE_PROFILE`` —
    единственный источник правды теперь живёт в ``SETTINGS["profile"]``.
    """
    return SETTINGS["profile"]


def get_setting(*keys: str, default=None):
    """Безопасный доступ к вложенным ключам SETTINGS.

    Принимает путь из имён ключей: ``get_setting("channels", "postgres", "poll_interval", default=2.0)``.
    Возвращает ``default`` (по умолчанию ``None``), если любого уровня нет
    или значение — лист/скаляр, который не пройти дальше как dict.

    Используется в коде, где требуется значение по умолчанию при
    отсутствии ключа.

    **Важно про lifecycle:** ``get_setting`` — это **compatibility helper**.
    Если SETTINGS не инициализирован (proxy UNINITIALIZED),
    ``get_setting`` возвращает ``default``, а НЕ поднимает
    ``ConfigurationError``. Это намеренное поведение для backward-compat
    с callers, которые читают настройки на уровне модуля, ещё до
    инициализации SETTINGS: default-fallback защищает от случайного вызова
    в неправильном lifecycle context.

    Для кода, который **требует** инициализированного SETTINGS
    (новый runtime-код, entrypoint'ы, ApplicationContext),
    используйте ``SETTINGS["..."]`` / ``SETTINGS.get("...")`` —
    они поднимают ``ConfigurationError("not initialized")`` если proxy
    UNINITIALIZED. См. design.md Decision 0/1 (lifecycle-gate).
    """
    try:
        node: object = SETTINGS._inner_dict if isinstance(SETTINGS, _LazySettings) else SETTINGS
        for k in keys:
            if isinstance(node, dict) and k in node:
                node = node[k]
            else:
                return default
        return node
    except ConfigurationError:
        return default


def require_setting(*keys: str):
    """Строгий доступ к ключам SETTINGS (единственный источник правды — config.json).

    Возвращает значение по пути ``keys`` или поднимает ``ConfigurationError``,
    если ключ (на любом уровне) отсутствует. Не возвращает fallback-литерал:
    отсутствие настройки — ошибка, а не молчаливая подстановка.
    """
    node: object = SETTINGS._inner_dict if isinstance(SETTINGS, _LazySettings) else SETTINGS
    for k in keys:
        if isinstance(node, dict) and k in node:
            node = node[k]
        else:
            raise ConfigurationError(
                "Отсутствует обязательный ключ конфига: " + ".".join(keys)
            )
    return node
