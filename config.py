import json
import os
import re
from pathlib import Path
from typing import Any

_CONFIG_FILE = Path(__file__).parent / "config.json"
_PROJECT_FILE = Path(__file__).parent / "project.json"
_SECRETS_FILE = Path(__file__).parent / ".secrets.env"


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

# Порядок мержей (поздний перекрывает ранний):
#   project.json → config.json → .secrets.env
#
#   project.json   — проектные секции (channels.*, skills.*, cli, benchmark,
#                    streamlit, gateway, logging.db) в формате JSONC.
#   config.json    — настройки nanobot (агенты, провайдеры, API, gateway).
#   .secrets.env   — секреты (API-ключи, DATABASE_URL) в провайдер-скоупинг
#                    формате; подставляются в ${VAR} после резолва.
SETTINGS = AttrDict()
if _PROJECT_FILE.exists():
    _deep_merge(SETTINGS, load_config_json(_PROJECT_FILE))
if _CONFIG_FILE.exists():
    _deep_merge(SETTINGS, load_config_json(_CONFIG_FILE))
if _SECRETS_FILE.exists():
    _deep_merge(SETTINGS, load_env(_SECRETS_FILE))


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
    if "${" not in val:
        os.environ.setdefault(key, val)

# Резолв ${VAR} в проектных настройках (config.json читается сырым,
# поэтому мост сам подставляет секреты из os.environ).
SETTINGS = _resolve_env_refs(SETTINGS)

# Доэкспорт резолвнутых значений (setdefault: внешние env сохраняют приоритет).
for key, val in _flatten_env(SETTINGS).items():
    os.environ.setdefault(key, val)
