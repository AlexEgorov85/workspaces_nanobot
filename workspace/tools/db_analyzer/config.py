"""Загрузка конфигурации db_analyzer из config.json агента.

Два источника:
  1. tools.db_analyzer — настройки самого инструмента (БД, эмбеддинги, режимы).
     Читаются напрямую из JSON, закешированы с проверкой mtime.
  2. Агентский конфиг (providers, model) — для LLM-провайдера.
     Использует штатный load_config() из nanobot.
"""

import json
import re
from pathlib import Path
from typing import Any

from nanobot.config.loader import load_config, resolve_config_env_vars
from nanobot.config.paths import get_config_path as _get_nanobot_config_path

# ---------------------------------------------------------------------------
# Кеш для сырого config.json (инструментальные настройки)
# ---------------------------------------------------------------------------
_RAW_CACHE: dict[str, Any] | None = None
_RAW_MTIME: float = 0.0


def _load_raw_config(*, force: bool = False) -> dict[str, Any]:
    """Прочитать config.json как dict (с mtime-кешем)."""
    global _RAW_CACHE, _RAW_MTIME

    cfg_path: Path = _get_nanobot_config_path()
    current_mtime: float = cfg_path.stat().st_mtime if cfg_path.exists() else 0.0

    if force or _RAW_CACHE is None or current_mtime != _RAW_MTIME:
        _RAW_CACHE = json.loads(cfg_path.read_text(encoding="utf-8"))
        _RAW_MTIME = current_mtime

    return _RAW_CACHE


def refresh_config():
    """Принудительно перечитать config.json и сбросить оба кеша."""
    _load_raw_config(force=True)
    _load_agent_config(force=True)


# ---------------------------------------------------------------------------
# Инструментальные настройки (tools.db_analyzer)
# ---------------------------------------------------------------------------

def get_tool_config() -> dict[str, Any]:
    """Вернуть весь блок tools.db_analyzer из config.json."""
    raw = _load_raw_config()
    return raw.get("tools", {}).get("db_analyzer", {})


def load_db_config() -> dict[str, Any]:
    """Вернуть параметры подключения к БД (host, port, user, password, database)."""
    cfg = get_tool_config().get("database", {})
    cs = cfg.get("connection_string", "postgresql://localhost:5432/postgres")
    m = re.match(
        r"postgresql(?:s)?://"
        r"(?:(?P<user>[^:@]+)(?::(?P<password>[^@]*))?@)?"
        r"(?P<host>[^:/]+)"
        r"(?::(?P<port>\d+))?"
        r"/(?P<database>.+)?",
        cs,
    )
    if m:
        return {
            "host": m.group("host") or "localhost",
            "port": int(m.group("port")) if m.group("port") else 5432,
            "database": m.group("database") or "postgres",
            "user": m.group("user") or "postgres",
            "password": m.group("password") or "",
        }
    return {"host": "localhost", "port": 5432, "database": "postgres", "user": "postgres", "password": ""}


def get_db_schema() -> str:
    """Имя схемы БД."""
    return get_tool_config().get("database", {}).get("schema", "oarb")


def get_vector_index_path() -> str:
    """Путь к FAISS-индексу из tools.db_analyzer.modes.vector.index_path."""
    vec = get_tool_config().get("modes", {}).get("vector", {})
    return vec.get("index_path", str(Path.home() / ".nanobot" / "vectors" / "audit_index"))


def get_embedding_config() -> dict[str, Any]:
    """Настройки эмбеддингов (base_url, model, dimension)."""
    return get_tool_config().get("embedding", {})


def get_embedding_model() -> str:
    """Модель для эмбеддингов."""
    return get_embedding_config().get("model", "mxbai-embed-large:latest")


# ---------------------------------------------------------------------------
# Агентский конфиг (LLM-провайдер и пр.) — штатный load_config из nanobot
# ---------------------------------------------------------------------------

_AGENT_CONFIG = None
_AGENT_MTIME: float = 0.0


def load_agent_config(*, force: bool = False):
    """Загрузить (из кеша или с диска) pydantic-конфиг агента.

    Используется в llm.py для создания LLM-провайдера.
    """
    global _AGENT_CONFIG, _AGENT_MTIME

    cfg_path: Path = _get_nanobot_config_path()
    current_mtime: float = cfg_path.stat().st_mtime if cfg_path.exists() else 0.0

    if force or _AGENT_CONFIG is None or current_mtime != _AGENT_MTIME:
        _AGENT_CONFIG = resolve_config_env_vars(load_config())
        _AGENT_MTIME = current_mtime

    return _AGENT_CONFIG
