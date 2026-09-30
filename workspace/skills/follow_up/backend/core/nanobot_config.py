"""Настройки агента: модель, база, схема и модели — там же, где у соседей.

У нанобота навык не заводит свою модель. `audit_analyzer` берёт её у агента:
`agents.defaults` (провайдер и модель) плюс `providers.<provider>`
(`apiBase`, `apiKey`), а собственная секция `skills.<навык>.llm_*` эти
значения перекрывает — см. `lib/services/llm_config.resolve_llm_config` в их
репозитории. Смысл в том, что администратор меняет модель в одном месте, и
за ней идут все навыки разом.

Мы живём отдельным процессом, поэтому импортировать их `config.py` не
можем. Значит читаем те же файлы и теми же правилами:

    project.json → session_manager.json → config.json → .secrets.env
                                                  (позднее перекрывает)

потом подстановка `${VAR}` из окружения. Правила разбора скопированы по
поведению, а не по коду: JSONC-комментарии не должны съедать `https://`
внутри строк, а в `.secrets.env` ключ `agents__defaults__model` — это путь
в дереве, а не имя с подчёркиваниями.

Путь к их корню приходит переменной `NANOBOT_HOME` — её ставит лаунчер
навыка (`workspace/skills/follow_up/scripts/follow_up_mcp`), вычисляя от
своего расположения. Нет переменной — работаем по своему `.env`, как
раньше: это режим собственного интерфейса и разработки.

`profiles/<режим>.jsonc` не читается: профиль меняет только имена
runtime-таблиц шины и журналов, к модели, базе и схеме отношения не имеет.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlsplit

logger = logging.getLogger(__name__)

HOME_ENV_VAR = "NANOBOT_HOME"
SKILL_SECTION = "follow_up"
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


# ──────────────────────────────────────────────────────────────────
# Разбор их файлов
# ──────────────────────────────────────────────────────────────────

def strip_jsonc_comments(text: str) -> str:
    """Убрать `//` и `/* */`, не тронув содержимое строк.

    Наивная замена по регулярке съела бы `https://` в каждом `apiBase` —
    и конфиг перестал бы разбираться ровно там, где важен.
    """
    out: List[str] = []
    i, n = 0, len(text)
    in_string = in_block = False
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


def _parse_value(raw: str) -> Any:
    """Значение из env-файла: true/false, число, JSON, список через запятую."""
    v = (raw or "").strip()
    if v.lower() in ("true", "yes"):
        return True
    if v.lower() in ("false", "no"):
        return False
    for cast in (int, float):
        try:
            return cast(v)
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


def _load_env_file(path: Path) -> Dict[str, Any]:
    """`.secrets.env` в дерево.

    Две тонкости их формата. Строка-заголовок `# providers: openai` задаёт
    префикс для последующих ключей — заголовок отличает **двоеточие**:
    обычный комментарий без него префикс не трогает (так с их `5d31289`,
    16.09.2026; до этого любая `#`-строка без `=` считалась заголовком и
    уводила ключи под ней в несуществующую ветку). Двойное подчёркивание в
    имени — разделитель уровней: `agents__defaults__model` это
    `{"agents": {"defaults": {"model": ...}}}`.
    """
    if not path.exists():
        return {}
    tree: Dict[str, Any] = {}
    prefix: List[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#") and "=" not in s:
            if ":" in s:
                head = s.lstrip("#").strip().lower().replace("-", "_")
                prefix = [p.strip() for p in head.split(":", 1) if p.strip()]
            continue
        if "=" not in s or s.startswith("#"):
            continue
        key, _, raw = s.partition("=")
        keys = prefix + key.strip().split("__")
        node = tree
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node[keys[-1]] = _parse_value(raw)
    return tree


def _load_json_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(strip_jsonc_comments(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"[nanobot] {path.name} не разобран: {e}")
        return {}
    return data if isinstance(data, dict) else {}


def _deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> None:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def _flatten_env(tree: Dict[str, Any], prefix: str = "") -> Dict[str, str]:
    """Дерево настроек → имена переменных окружения, как у них.

    `providers.openai.apiKey` становится `PROVIDERS_OPENAI_APIKEY`, а
    верхнеуровневый `LLM_API_KEY` из `.secrets.env` — самим собой. Значения
    с `${` пропускаются: заглушка в окружении перекрыла бы настоящее значение
    на следующем проходе.
    """
    out: Dict[str, str] = {}
    for k, v in tree.items():
        name = f"{prefix}_{k}" if prefix else str(k)
        if isinstance(v, dict):
            out.update(_flatten_env(v, name))
        elif "${" not in str(v):
            out[name.replace(" ", "_").replace("-", "_").upper()] = str(v)
    return out


def _resolve_env_refs(value: Any, env: Optional[Dict[str, str]] = None) -> Any:
    """`${VAR}` из окружения. Неизвестное оставляем как есть — как у них.

    `env` — откуда брать значения; по умолчанию `os.environ`.
    """
    src = os.environ if env is None else env
    if isinstance(value, str):
        return _ENV_REF.sub(lambda m: src.get(m.group(1), m.group(0)), value)
    if isinstance(value, dict):
        return {k: _resolve_env_refs(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env_refs(v, env) for v in value]
    return value


def load_settings(home: Path) -> Dict[str, Any]:
    """Настройки нанобота: файлы в их порядке плюс подстановка `${VAR}`.

    Подстановка идёт не только из `os.environ`, но и из самих файлов: их
    `config.py` перед резолвом экспортирует слитое дерево в окружение
    (`_export_secrets_to_env`), и `${LLM_API_KEY}` в `config.json` находит
    `LLM_API_KEY=…` из `.secrets.env`. Без этого зеркала наш процесс —
    дочерний, с урезанным окружением от MCP-клиента — видел бы вместо ключа
    литерал `${LLM_API_KEY}` и уносил его в заголовок Authorization.

    Внешнее окружение приоритетнее файлов, как и у них (`setdefault`).
    Своё `os.environ` не трогаем: мы библиотека, а не точка входа.
    """
    settings: Dict[str, Any] = {}
    # Тот же порядок, что у их config.py: session_manager.json — поправки
    # конкретного развёртывания между базой и настройками фреймворка.
    for name in ("project.json", "session_manager.json", "config.json"):
        _deep_merge(settings, _load_json_file(home / name))
    _deep_merge(settings, _load_env_file(home / ".secrets.env"))
    env = _flatten_env(settings)
    env.update(os.environ)
    return _resolve_env_refs(settings, env)


# ──────────────────────────────────────────────────────────────────
# Модель
# ──────────────────────────────────────────────────────────────────

def resolve_llm(settings: Dict[str, Any],
                section: str = SKILL_SECTION) -> Dict[str, Any]:
    """Провайдер, модель, адрес и ключ — по правилам соседнего навыка.

    Дефолт от агента, `skills.<section>.llm_*` перекрывает. Возвращает
    только то, что удалось определить: недостающее решает вызывающий, а не
    подстановка «разумного» значения. Молча подставленная чужая модель — это
    ответы, за которые мы не отвечаем.
    """
    over = ((settings.get("skills") or {}).get(section) or {})
    defaults = ((settings.get("agents") or {}).get("defaults") or {})
    provider = over.get("llm_provider") or defaults.get("provider") or ""
    provider_cfg = ((settings.get("providers") or {}).get(provider) or {})

    # Две формы переопределения. Плоские `llm_*` — как в их
    # `lib/services/llm_config.resolve_llm_config` и в шаблоне бриджа.
    # Вложенная `llm: {max_tokens, temperature}` — то, что пропускает
    # pydantic-схема секции навыка (`SkillSettings`, `extra="forbid"`):
    # плоские ключи там не объявлены, и `project.json` с ними не пройдёт
    # валидацию на старте gateway. Значит для `project.json` доступна только
    # вложенная форма, и не читать её — значит не читать единственный
    # рабочий способ.
    nested = over.get("llm") if isinstance(over.get("llm"), dict) else {}

    out: Dict[str, Any] = {"provider": provider}
    model = over.get("llm_model") or defaults.get("model")
    api_base = over.get("llm_api_base") or provider_cfg.get("apiBase")
    api_key = over.get("llm_api_key") or provider_cfg.get("apiKey")
    max_tokens = (over.get("llm_max_tokens") or nested.get("max_tokens")
                  or defaults.get("maxTokens"))
    temperature = over.get("llm_temperature")
    if temperature is None:
        temperature = nested.get("temperature")
    if temperature is None:
        temperature = defaults.get("temperature")

    if model:
        out["model"] = str(model)
    if api_base:
        out["api_base"] = str(api_base)
    if api_key:
        out["api_key"] = str(api_key)
    if max_tokens:
        out["max_tokens"] = int(max_tokens)
    if temperature is not None:
        out["temperature"] = float(temperature)
    return out


def home() -> Optional[Path]:
    """Корень нанобота, если нас запустил он."""
    raw = (os.environ.get(HOME_ENV_VAR) or "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    return p if p.is_dir() else None


def adopt_agent_llm(cfg) -> Dict[str, Any]:
    """Взять модель агента в наши настройки. Возвращает что применили.

    Пустой словарь — не применяли ничего: либо нас запустил не нанобот,
    либо в его конфиге не нашлось ни модели, ни адреса.

    Ключ и адрес не логируем: конфиг агента резолвит `${LLM_API_KEY}` в
    настоящее значение, и строка в журнале — это утечка.
    """
    if getattr(cfg, "llm_ignore_nanobot", False):
        logger.info("[nanobot] LLM_IGNORE_NANOBOT=true — модель берём свою")
        return {}

    root = home()
    if root is None:
        return {}

    llm = resolve_llm(load_settings(root))
    if not llm.get("model") or not llm.get("api_base"):
        logger.warning(
            f"[nanobot] В конфиге {root} не нашлись модель и адрес "
            f"(agents.defaults + providers.*) — остаёмся на своих настройках")
        return {}

    applied: Dict[str, Any] = {}
    # Провайдер агента — всегда OpenAI-совместимый HTTP: и vLLM, и minimax,
    # и openai ходят одним протоколом. Режим gigachat сюда не приезжает
    # никогда: у нанобота такого провайдера нет вовсе.
    cfg.llm_mode = "api"
    cfg.llm_base_url = llm["api_base"]
    cfg.llm_model_name = llm["model"]
    applied["model"] = llm["model"]
    applied["provider"] = llm.get("provider") or "?"
    key = llm.get("api_key")
    if key and "${" in key:
        # Нерешённая заглушка — не ключ. Оставляем свой: он хотя бы наш.
        logger.warning("[nanobot] apiKey у агента остался заглушкой "
                       f"{key!r} — секрет не найден ни в файлах, ни в "
                       "окружении; ключ оставляем свой")
        key = None
    if key:
        cfg.llm_api_key = key
        applied["api_key"] = "задан"
    if llm.get("max_tokens"):
        cfg.llm_max_tokens = llm["max_tokens"]
        applied["max_tokens"] = llm["max_tokens"]
    if llm.get("temperature") is not None:
        cfg.llm_temperature = llm["temperature"]
        applied["temperature"] = llm["temperature"]

    logger.info(
        f"[nanobot] Модель взята у агента: {applied['model']} "
        f"(провайдер {applied['provider']}), настройки из {root}")
    return applied


# ──────────────────────────────────────────────────────────────────
# Greenplum
# ──────────────────────────────────────────────────────────────────
#
# В публичном коде навыка нет ни адреса Greenplum, ни базы, ни схемы — и не
# должно быть. Всё это уже есть у самого агента: его шина с Единым рабочим
# местом лежит в той же базе и в той же схеме, что и наши таблицы t_fu_*.
# Берём оттуда — тем же правилом, что модель: явное значение Follow Up
# (`site_defaults.env`, `.env`, переменная окружения) важнее агентского.

_GP_FIELDS = (("gp_host", "host"), ("gp_port", "port"), ("gp_db", "db"),
              ("gp_user", "user"), ("gp_password", "password"),
              ("gp_write_schema", "schema"))

# Схемы, которые не бывают нашими: в них навык не создаёт таблиц, даже если
# шина агента живёт там. `public` — схема по умолчанию в шаблоне агента.
_NOT_OUR_SCHEMAS = {"public", "pg_catalog", "information_schema"}


def resolve_gp(settings: Dict[str, Any]) -> Dict[str, Any]:
    """Подключение и схема — из канала шины агента (`channels.postgres`).

    Возвращает только то, что удалось определить. DSN в форме URL; иная
    форма (ключ=значение) не разбирается, и тогда подключение не берётся —
    лучше честно остаться без GP, чем подключиться не туда.
    """
    pg = ((settings.get("channels") or {}).get("postgres") or {})
    dsn = str(pg.get("dsn") or settings.get("DATABASE_URL") or "").strip()
    out: Dict[str, Any] = {}
    if dsn and "${" not in dsn:
        parts = urlsplit(dsn)
        if parts.scheme in ("postgres", "postgresql") and parts.hostname:
            out["host"] = parts.hostname
            out["port"] = parts.port or 5432
            db = parts.path.lstrip("/")
            if db:
                out["db"] = db
            if parts.username:
                out["user"] = unquote(parts.username)
            if parts.password:
                out["password"] = unquote(parts.password)
    schema = str(pg.get("schema") or "").strip()
    if schema and "${" not in schema and schema.lower() not in _NOT_OUR_SCHEMAS:
        out["schema"] = schema
    return out


def _probe_kind() -> str:
    """Что за база по адресу: greenplum | postgresql | unknown.

    Наш DDL гринпламовский (`appendonly`, `DISTRIBUTED BY`), и в обычном
    PostgreSQL он не создастся. А шина агента бывает и там: у разработчиков
    на своей машине и в пульте запуска под Windows. Включить GP по такой
    шине значит засыпать журнал ошибками создания таблиц и записать наши
    таблицы в чужую базу для разработки.

    Если база не ответила — `unknown`: в проме GP бывает недоступен
    минуту, и из-за этого выключать корпус на весь сеанс нельзя.
    """
    try:
        from backend.storage import gp
        conn = gp._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT version()")
                ver = str(cur.fetchone()[0] or "")
        finally:
            conn.close()
    except Exception as e:                                  # noqa: BLE001
        logger.warning(f"[nanobot] База агента не ответила ({type(e).__name__}) "
                       f"— считаем Greenplum, проверится при первом запросе")
        return "unknown"
    return "greenplum" if "greenplum" in ver.lower() else "postgresql"


def adopt_agent_gp(cfg) -> Dict[str, Any]:
    """Взять подключение к Greenplum и схему у агента. Возвращает, что взяли.

    Поле, заданное у Follow Up явно, не трогаем — ни адрес, ни схему, ни
    `GP_ENABLED`. Поэтому `GP_ENABLED=false` в `.env` по-прежнему означает
    локальный режим, даже когда нас запустил нанобот.

    Пароль в журнал не пишется; адрес пишется только хостом.
    """
    root = home()
    if root is None:
        return {}
    gp = resolve_gp(load_settings(root))
    explicit = set(getattr(cfg, "model_fields_set", ()) or ())
    has_host = bool(gp.get("host")) or ("gp_host" in explicit and cfg.gp_host)
    has_schema = bool(gp.get("schema")) or ("gp_write_schema" in explicit
                                            and cfg.gp_write_schema)
    if not has_host or not has_schema:
        why = ("нет подключения к базе (channels.postgres.dsn)" if not has_host
               else "нет своей схемы (public не в счёт) — задайте GP_WRITE_SCHEMA "
                    "в .env папки навыка")
        logger.warning(f"[nanobot] Greenplum не подключаю: в настройках агента "
                       f"({root}) {why}")
        return {}

    applied: Dict[str, Any] = {}
    for field, key in _GP_FIELDS:
        if field in explicit or key not in gp:
            continue
        setattr(cfg, field, gp[key])
        applied[field] = "задан" if field == "gp_password" else gp[key]

    if "gp_enabled" not in explicit and not getattr(cfg, "gp_enabled", False):
        kind = _probe_kind()
        if kind == "postgresql":
            logger.info("[nanobot] Шина агента в обычном PostgreSQL, не в "
                        "Greenplum — общий корпус не подключаю, работаю по "
                        "локальному")
        else:
            cfg.gp_enabled = True
            applied["gp_enabled"] = True

    if applied:
        logger.info(
            f"[nanobot] Greenplum взят у агента: {cfg.gp_host} / "
            f"{cfg.gp_db} / схема {cfg.gp_write_schema}"
            f"{'' if cfg.gp_enabled else ' (выключен)'}")
    return applied


# ──────────────────────────────────────────────────────────────────
# Модели
# ──────────────────────────────────────────────────────────────────

def models_dir(root: Path) -> Path:
    """Каталог моделей у навыков нанобота — там же их держат соседние навыки."""
    return root / "workspace" / "data_store" / "cache" / "caches_pipelines"


def adopt_agent_models(cfg) -> Dict[str, Any]:
    """Найти модели в каталоге нанобота, если рядом с нами их нет.

    Реранкер тот же, что у соседних навыков, — `bge-reranker-v2-m3`, и
    второй копии на сервер не нужно: берём их. Эмбеддер свой
    (`bge-m3-russian-legal`), векторы корпуса в GP посчитаны именно им —
    подменять его другим bge-m3 нельзя, выдача станет случайной. Его туда
    кладёт одноразовый скрипт площадки из репозитория Follow Up.
    """
    root = home()
    if root is None:
        return {}
    explicit = set(getattr(cfg, "model_fields_set", ()) or ())
    cache = models_dir(root)
    applied: Dict[str, Any] = {}
    for field in ("bge_model_path", "reranker_model_path"):
        if field in explicit:
            continue
        current = Path(str(getattr(cfg, field)))
        if current.exists():
            continue
        candidate = cache / current.name
        if candidate.exists():
            setattr(cfg, field, str(candidate))
            applied[field] = str(candidate)
    if applied:
        logger.info(f"[nanobot] Модели из каталога нанобота: "
                    f"{', '.join(Path(v).name for v in applied.values())}")
    return applied

