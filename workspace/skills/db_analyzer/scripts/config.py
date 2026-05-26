"""
Конфигурация db_analyzer.

Читает skills/db_analyzer/config.json, кеширует при первом вызове.
Все getter-функции возвращают значения из конкретных секций конфига.

Пример config.json:
    {
      "llm": {
        "provider": "mistral",
        "model": "mistral-large-latest",
        "api_base": "https://api.mistral.ai/v1",
        "api_key": "секретный-ключ",
        "max_tokens": 8192,
        "temperature": 0.1
      },
      "database": {
        "connection_string": "postgresql://postgres:1@localhost:5432/postgres",
        "schema": "oarb",
        "tables": ["audit_reports", "audits", "report_items", "violations"]
      },
      "embedding": {
        "base_url": "http://localhost:11434/api/embed",
        "model": "mxbai-embed-large:latest",
        "dimension": 1024
      },
      "modes": {
        "predefined": { "enabled": true },
        "vector": {
          "enabled": true,
          "index_path": "data/vector",
          "top_k": 5,
          "threshold": 0.75
        },
        "sql": { "enabled": true }
      },
      "cli": {
        "default_mode": "predefined",
        "default_format": "json",
        "max_retries": 3,
        "timeout_sec": 60
      }
    }
"""

import json
import os
import re
from pathlib import Path
from typing import Any

# Путь к config.json — родительская директория scripts/
_CONFIG_DIR = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _CONFIG_DIR / "config.json"

# Кеш конфига: загружается один раз при старте
_CACHE: dict[str, Any] | None = None


def _load(*, force: bool = False) -> dict[str, Any]:
    """
    Загрузить config.json (с кешированием).

    Args:
        force: Принудительная перезагрузка (сбрасывает кеш).

    Returns:
        dict с содержимым config.json.

    Пример:
        >>> cfg = _load()
        >>> cfg["database"]["schema"]
        'oarb'
    """
    global _CACHE
    if force or _CACHE is None:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            _CACHE = json.load(f)
    return _CACHE


def refresh_config():
    """
    Принудительно перечитать config.json (сброс кеша).

    Пример:
        >>> refresh_config()  # перезагрузить после изменения файла
    """
    _load(force=True)


def get_llm_config() -> dict[str, Any]:
    """
    Конфигурация LLM (секция 'llm').

    Returns:
        dict с ключами: provider, model, api_base, api_key, max_tokens, temperature.

    Пример:
        >>> get_llm_config()["model"]
        'mistral-large-latest'
    """
    return _load().get("llm", {})


def get_tool_config() -> dict[str, Any]:
    """
    Полный конфиг навыка (все секции).

    Returns:
        dict — весь config.json целиком.

    Пример:
        >>> cfg = get_tool_config()
        >>> cfg["modes"]["sql"]["enabled"]
        True
    """
    return _load()


def load_db_config() -> dict[str, Any]:
    """
    Конфигурация подключения к PostgreSQL.

    Парсит connection_string вида:
        postgresql://user:password@host:port/database

    Returns:
        dict с ключами: host, port, database, user, password.

    Пример:
        >>> load_db_config()
        {'host': 'localhost', 'port': 5432, 'database': 'postgres',
         'user': 'postgres', 'password': '1'}
    """
    cfg = _load().get("database", {})
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
    """
    Имя схемы БД (секция 'database.schema').

    Returns:
        Название схемы (по умолчанию 'oarb').

    Пример:
        >>> get_db_schema()
        'oarb'
    """
    return _load().get("database", {}).get("schema", "oarb")


def get_vector_index_path() -> str:
    """
    Путь к директории с FAISS-индексами (секция 'modes.vector.index_path').

    Если путь относительный — разрешается относительно директории config.json
    (корень навыка db_analyzer). Если абсолютный — используется как есть.

    Returns:
        Абсолютный путь к директории с .faiss и _metadata.json файлами.

    Пример:
        >>> get_vector_index_path()
        '.../db_analyzer/data/vector'
    """
    vec = _load().get("modes", {}).get("vector", {})
    path = vec.get("index_path", "")
    if not path:
        return str(Path.home() / ".nanobot" / "vectors" / "audits_index")
    p = Path(path)
    if p.is_absolute():
        return str(p)
    # Относительный путь — относительно корня навыка
    return str(_CONFIG_DIR / path)


def get_embedding_config() -> dict[str, Any]:
    """
    Конфигурация embedding-сервиса (секция 'embedding').

    Returns:
        dict с ключами: base_url, model, dimension.

    Пример:
        >>> get_embedding_config()
        {'base_url': 'http://localhost:11434/api/embed',
         'model': 'mxbai-embed-large:latest',
         'dimension': 1024}
    """
    return _load().get("embedding", {})


def get_embedding_model() -> str:
    """
    Модель эмбеддингов (секция 'embedding.model').

    Returns:
        Название модели (по умолчанию 'mxbai-embed-large:latest').

    Пример:
        >>> get_embedding_model()
        'mxbai-embed-large:latest'
    """
    return get_embedding_config().get("model", "mxbai-embed-large:latest")


def get_cli_config() -> dict[str, Any]:
    """
    Конфигурация CLI (секция 'cli').

    Returns:
        dict с ключами: default_mode, default_format, max_retries, timeout_sec.

    Пример:
        >>> get_cli_config()["max_retries"]
        3
    """
    return _load().get("cli", {})


def get_max_retries() -> int:
    """
    Максимальное количество ретраев (секция 'cli.max_retries').

    Returns:
        Число повторов при ошибке (по умолчанию 3).

    Пример:
        >>> get_max_retries()
        3
    """
    return get_cli_config().get("max_retries", 3)
