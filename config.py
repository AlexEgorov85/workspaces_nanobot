import json
import os
import re
from pathlib import Path
from typing import Any

_CONFIG_FILE = Path(__file__).parent / "config.json"
_PROJECT_FILE = Path(__file__).parent / "project.json"
_SECRETS_FILE = Path(__file__).parent / ".secrets.env"
_SESSION_MANAGER_FILE = _PROJECT_FILE.parent / "session_manager.json"
_PROFILES_DIR = _PROJECT_FILE.parent / "profiles"


_SUPPORTED_PROFILES = frozenset({"prod", "test"})


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
    env_file = Path(path or _ENV_FILE)
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

    Поддерживает комментарии ``//`` и ``/* */`` (JSONC) — проект использует их
    в project.json. Стандартный JSON (config.json) парсится как и раньше.
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
#   1. project.json                    — база
#   2. session_manager.json (если есть) — per-deploy override (pool/timeouts)
#   3. config.json                     — nanobot-настройки
#   4. profiles/<mode>.jsonc           — профиль (если mode != prod)
#   5. .secrets.env (${VAR})           — резолв env refs
#   6. validate_runtime_isolation()    — hard-fail
#
# Ключевое: profile overlay идёт ПОСЛЕДНИМ, поэтому profile-owned runtime-ключи
# (channels.postgres.{table_name,messages_table,meta_table,claims_table} и
# logging.db.{table_name,question_runs_table}) — immutable после применения
# профиля. Даже если session_manager.json или config.json содержат prod-имена,
# profile их перетирает.
# ---------------------------------------------------------------------------

PROFILE_OWNED_RUNTIME_KEYS = frozenset({
    ("channels", "postgres", "table_name"),
    ("channels", "postgres", "messages_table"),
    ("channels", "postgres", "meta_table"),
    ("channels", "postgres", "claims_table"),
    ("logging",  "db",       "table_name"),
    ("logging",  "db",       "question_runs_table"),
})

EXPECTED_RUNTIME_TABLE_NAMES: dict[str, dict[str, str]] = {
    "prod": {
        "conversation_messages": "agent_conversation_messages",
        "session_messages":      "agent_session_messages",
        "session_meta":          "agent_session_meta",
        "worker_claims":         "agent_worker_claims",
        "gateway_logs":          "agent_gateway_logs",
        "question_runs":         "agent_question_runs",
    },
    "test": {
        "conversation_messages": "agent_conversation_messages_test",
        "session_messages":      "agent_session_messages_test",
        "session_meta":          "agent_session_meta_test",
        "worker_claims":         "agent_worker_claims_test",
        "gateway_logs":          "agent_gateway_logs_test",
        "question_runs":         "agent_question_runs_test",
    },
}


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


def _merge_profile_overlay(cfg: dict, mode: str) -> None:
    """Применить profiles/<mode>.jsonc как ПОСЛЕДНИЙ шаг перед валидацией.

    Для prod — no-op (prod это чистый project.json).
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

      * все 6 profile-owned runtime-ключей ОБЯЗАНЫ присутствовать;
      * никаких посторонних ключей (только эти 6 разрешены).

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
            f"Все 6 ключей обязательны: {sorted(allowed)}."
        )

    extra = found - allowed
    if extra:
        raise ConfigurationError(
            f"profiles/{mode}.jsonc содержит ключи, которые профиль "
            f"не имеет права менять: {sorted(extra)}. "
            f"Разрешены только 6 profile-owned runtime-ключей: "
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
        "worker_claims":         (pg.get("claims_table", "") if isinstance(pg, dict) else ""),
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
      1. project.json                    — база
      2. session_manager.json (если есть) — per-deploy override
      3. config.json                     — nanobot-настройки
      4. .secrets.env                     — секреты (``DATABASE_URL``,
                                          провайдерские ``api_key``)
      5. profiles/<mode>.jsonc           — профиль (если mode != prod)
      6. ${VAR} резолв через os.environ
      7. validate_runtime_isolation()    — hard-fail
    """
    cfg: dict = {}
    if _PROJECT_FILE.exists():
        project_data = load_config_json(_PROJECT_FILE)
        if isinstance(project_data, AttrDict):
            project_data = dict(project_data)
        _deep_merge(cfg, project_data)

    _deep_merge(cfg, _load_session_manager_override())

    if _CONFIG_FILE.exists():
        config_data = load_config_json(_CONFIG_FILE)
        if isinstance(config_data, AttrDict):
            config_data = dict(config_data)
        _deep_merge(cfg, config_data)

    # Секреты из .secrets.env после config.json (чтобы могли перекрыть
    # то, что в config.json, при необходимости). До профиля (профиль —
    # только runtime-таблицы; секреты идут в cfg как обычные ключи).
    _deep_merge(cfg, _load_secrets_override())

    # Экспорт secrets в os.environ ДО _resolve_env_refs — чтобы ${VAR}
    # в project.json/config.json нашли свои значения.
    _export_secrets_to_env(cfg)

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
    для backward-compat с существующим кодом (``streamlit_app.py``,
    ``history_search_tool.py`` и т.п.).
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
    # (canonical API) независимо от того, что лежит в project.json.
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
    с callers типа ``subprocess_manager.SubprocessManager.__init__``
    (запускается из ``gateway.py:_run`` **после** ``_initialize_settings``,
    но исторически модуль импортировался с module-level ``SETTINGS``;
    default-fallback защищает от случайного вызова в неправильном
    lifecycle context).

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
    """Строгий доступ к ключам SETTINGS (единственный источник правды — project.json).

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
