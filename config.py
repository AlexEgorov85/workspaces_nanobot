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
        if line_stripped.startswith("#") and "=" not in line_stripped:
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
# Главный принцип: режим (test/prod) существует только во время разрешения
# конфигурации. После получения SETTINGS режим исчезает из runtime-модели.
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


def _resolve_mode(profile_arg: str | None = None) -> str:
    """Определить активный профиль. Приоритет: CLI > env > default=test.

    default=test — fail-safe: разработчик, набравший `python gateway.py`
    без флагов, попадает в изолированный test, а не в прод.
    """
    mode = profile_arg
    if mode is None:
        env_value = os.environ.get("NANOBOT_PROFILE", "").strip()
        mode = env_value or "test"
    if not re.fullmatch(r"[a-z0-9_-]+", mode):
        raise ConfigurationError(
            f"NANOBOT_PROFILE={mode!r} недопустим: "
            f"только [a-z0-9_-]+"
        )
    return mode


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
            f"NANOBOT_PROFILE={mode!r}, но profiles/{mode}.jsonc не найден. "
            f"Создайте profiles/{mode}.jsonc."
        )
    overlay_cfg = load_config_json(overlay)
    if isinstance(overlay_cfg, AttrDict):
        overlay_cfg = dict(overlay_cfg)
    validate_profile_overlay(overlay_cfg, mode)
    _deep_merge(cfg, overlay_cfg)


def validate_profile_overlay(overlay_cfg: dict, mode: str) -> None:
    """Hard-fail: profiles/<mode>.jsonc может содержать ТОЛЬКО
    6 profile-owned runtime-ключей. Никакого DSN, skill data, vector storage.
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


def resolve_application_config(profile: str | None = None) -> AttrDict:
    """Единая точка формирования SETTINGS.

    Используется всеми entry points (cli_agent.py, gateway.py,
    streamlit_app.py). Возвращает AttrDict — тот же тип, который
    существующий код уже импортирует через ``config.SETTINGS``.

    Порядок merge: project.json → session_manager.json → config.json →
    profiles/<mode>.jsonc → ${VAR} → validate.
    """
    mode = _resolve_mode(profile)

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

    _merge_profile_overlay(cfg, mode)

    cfg = _resolve_env_refs(cfg)

    validate_runtime_isolation(cfg, mode)

    return AttrDict(cfg)


def get_active_profile() -> str:
    """Вернуть профиль, с которым построен модульный ``SETTINGS``.

    Возвращает ``_ACTIVE_PROFILE`` — значение, зафиксированное при
    import-time. Это гарантирует, что ``SETTINGS`` и
    ``get_active_profile()`` согласованы между собой (а не два
    независимых чтения env, которые могут разойтись).

    Для динамического определения профиля в runtime (например, в
    ApplicationContext.create(profile=...)) используйте
    ``_resolve_mode(profile)`` напрямую.
    """
    return _ACTIVE_PROFILE

# Порядок мержей (поздний перекрывает ранний) — через ConfigurationResolver:
#   1. project.json                    — база
#   2. session_manager.json (если есть) — per-deploy override (pool/timeouts)
#   3. config.json                     — nanobot-настройки
#   4. profiles/<mode>.jsonc           — профиль (если mode != prod)
#   5. .secrets.env (${VAR})           — резолв env refs
#   6. validate_runtime_isolation()    — hard-fail
#
# Глобальный SETTINGS строится ОДИН РАЗ через ConfigurationResolver —
# это ЕДИНСТВЕННЫЙ загрузчик конфигурации в проекте. Раньше здесь был
# отдельный «старый» bootstrap (project.json → config.json → .secrets.env),
# что создавало второй путь формирования конфигурации, расходящийся с
# profile-aware путём через Resolver. Теперь оба пути объединены.
_ACTIVE_PROFILE = _resolve_mode()
SETTINGS = resolve_application_config(profile=_ACTIVE_PROFILE)


def _flatten_env(d: dict, prefix: str = "") -> dict[str, str]:
    result = {}
    for k, v in d.items():
        p = f"{prefix}_{k}" if prefix else k
        if isinstance(v, dict):
            result.update(_flatten_env(v, p))
        else:
            result[_(p).upper()] = str(v)
    return result


def _(s: str) -> str:
    return s.replace(" ", "_").replace("-", "_")


class ConfigurationError(ValueError):
    """Ошибка конфигурации: обязательный ключ отсутствует или некорректен.

    В отличие от ``get_setting`` (возвращает переданный ``default``),
    ``require_setting`` выбрасывает эту ошибку, чтобы отсутствие настройки
    не маскировалось подставным значением. Единственный источник правды —
    project.json.
    """


def get_setting(*keys: str, default=None):
    """Безопасный доступ к вложенным ключам SETTINGS.

    Принимает путь из имён ключей: ``get_setting("channels", "postgres", "poll_interval", default=2.0)``.
    Возвращает ``default`` (по умолчанию ``None``), если любого уровня нет
    или значение — лист/скаляр, который не пройти дальше как dict.

    Используется в коде, где требуется значение по умолчанию при
    отсутствии ключа.
    """
    node: object = SETTINGS
    for k in keys:
        if isinstance(node, dict) and k in node:
            node = node[k]
        else:
            return default
    return node


def require_setting(*keys: str):
    """Строгий доступ к ключам SETTINGS (единственный источник правды — project.json).

    Возвращает значение по пути ``keys`` или поднимает ``ConfigurationError``,
    если ключ (на любом уровне) отсутствует. Не возвращает fallback-литерал:
    отсутствие настройки — ошибка, а не молчаливая подстановка.
    """
    node: object = SETTINGS
    for k in keys:
        if isinstance(node, dict) and k in node:
            node = node[k]
        else:
            raise ConfigurationError(
                "Отсутствует обязательный ключ конфига: " + ".".join(keys)
            )
    return node


# Приоритет: провайдер "llm" (из .secrets.env: секция "# providers: llm") —
# это основной ключ LLM. Затем остальные провайдеры по порядку.
_providers = SETTINGS.get("providers", {}) or {}
_candidates: list[tuple[str, Any]] = []
if isinstance(_providers.get("llm"), dict):
    _candidates.append(("llm", _providers["llm"]))
_candidates += [(_n, _c) for _n, _c in _providers.items() if isinstance(_c, dict)]
for _prov_name, _prov_cfg in _candidates:
    _key = _prov_cfg.get("api_key") or _prov_cfg.get("apiKey")
    if _key and isinstance(_key, str) and not _key.startswith("${"):
        os.environ.setdefault("LLM_API_KEY", _key)

# Экспорт без ${...}: эти ключи не меняются при резолве и не должны
# затирать уже выставленные переменные окружения (setdefault).
for key, val in _flatten_env(SETTINGS).items():
    if "${" not in str(val):
        os.environ.setdefault(key, val)

# Резолв ${VAR} теперь полностью внутри ConfigurationResolver
# (resolve_application_config → _resolve_env_refs). Повторный вызов
# _resolve_env_refs на уровне модуля больше не нужен и был удалён —
# SETTINGS уже разрешён к моменту первого использования.

# Доэкспорт резолвнутых значений (setdefault: внешние env сохраняют приоритет).
for key, val in _flatten_env(SETTINGS).items():
    os.environ.setdefault(key, val)
