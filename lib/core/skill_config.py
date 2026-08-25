"""Runtime API для skill'ов: конфигурация, таблицы, embedding, FAISS.

Параметризован по ``skill_name``. Каждый skill вызывает функции со своим
именем (например, ``get_db_tables("audit_analyzer")``). Это единая точка
для всех skill'ов — никакой копипасты между skill'ами.

Реализация читает секцию ``project.json::skills.<name>`` через
``config.SETTINGS`` и табличный реестр через ``lib.services.table_registry``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _skills() -> dict[str, Any]:
    """Секция ``skills.*`` из project.json."""
    from config import SETTINGS

    return SETTINGS.get("skills") or {}


def _skill_cfg(skill_name: str) -> dict[str, Any]:
    cfg = _skills().get(skill_name)
    if not isinstance(cfg, dict):
        raise KeyError(f"skill {skill_name!r} не найден в project.json::skills")
    return cfg


def _tables_list(skill_name: str) -> list[dict]:
    cfg = _skill_cfg(skill_name)
    raw = cfg.get("tables") or []
    return [t if isinstance(t, dict) else {"name": t} for t in raw]


def _vector_indexes_list(skill_name: str) -> list[dict]:
    cfg = _skill_cfg(skill_name)
    raw = cfg.get("vector_indexes") or []
    return [v for v in raw if isinstance(v, dict)]


def get_db_tables(skill_name: str) -> list[str]:
    """Доменные таблицы skill'а для LLM-схемы.

    Возвращает имена таблиц из ``tables[]`` без ``label`` — это доменные
    таблицы, которые попадают в описание схемы для LLM. Таблицы с
    ``label`` (реестры метаданных) — в схему не попадают и доступны
    только через ``TableRegistry.resources_by_label(label)``.
    """
    out: list[str] = []
    for t in _tables_list(skill_name):
        name = t.get("name")
        if name and not t.get("label"):
            out.append(name)
    return out


def get_db_schema(skill_name: str) -> str:
    """Схема skill'а (по первой таблице в ``tables[]``)."""
    tables = _tables_list(skill_name)
    if not tables:
        raise ValueError(
            f"skill {skill_name!r}: skills.{skill_name}.tables пуст"
        )
    first = tables[0].get("name", "")
    if "." in first:
        return first.split(".", 1)[0]
    raise ValueError(
        f"skill {skill_name!r}: первая таблица {first!r} не fully qualified "
        "(ожидается 'schema.table')"
    )


def get_predefined_scripts_table(skill_name: str) -> str:
    """Имя таблицы реестра предопределённых SQL-скриптов (``label='scripts_registry'``).

    Lookup идёт через ``TableRegistry.resources_by_label`` — это runtime
    состояние, не сырой конфиг.
    """
    from lib.services.table_registry import table_registry

    rs = table_registry.resources_by_label("scripts_registry")
    if rs:
        return rs[0].name

    raise ValueError(
        f"skill {skill_name!r}: ни один skill не зарегистрировал ресурс "
        "с label='scripts_registry'. Запустите через gateway "
        "(ApplicationContext)."
    )


def load_db_config(skill_name: str) -> dict[str, Any]:
    return {"schema": get_db_schema(skill_name), "tables": get_db_tables(skill_name)}


def get_llm_config(skill_name: str) -> dict[str, Any]:
    from lib.services.llm_config import resolve_llm_config

    return resolve_llm_config(overrides=_skill_cfg(skill_name))


def get_tool_config(skill_name: str) -> dict[str, Any]:
    return dict(_skill_cfg(skill_name))


def get_cli_config(skill_name: str) -> dict[str, Any]:
    cfg = _skill_cfg(skill_name)
    cli_cfg = cfg.get("cli") or {}
    return {
        "default_mode": cli_cfg.get("default_mode", "predefined"),
        "default_format": cli_cfg.get("default_format", "json"),
        "max_retries": int(cli_cfg.get("max_retries", 3)),
        "timeout_sec": int(cli_cfg.get("timeout_sec", 60)),
    }


def get_max_retries(skill_name: str) -> int:
    cfg = _skill_cfg(skill_name)
    cli_cfg = cfg.get("cli") or {}
    return int(cli_cfg.get("max_retries", 3))


def get_in_memory_config(skill_name: str, skill_root: Path | str) -> dict[str, Any]:
    """Конфиг in-memory кэша (путь к DuckDB-снапшоту + engine/enabled)."""
    from lib.services.table_registry import table_registry

    cfg = _skill_cfg(skill_name)
    cache_cfg = cfg.get("cache") or {}
    workspace_root = Path(skill_root).parent.parent
    return {
        "enabled": bool(cache_cfg.get("enabled", True)),
        "engine": cache_cfg.get("engine", "duckdb"),
        "cache_path": str(table_registry.snapshot_path(workspace_root)),
    }


def is_in_memory_enabled(skill_name: str) -> bool:
    cfg = _skill_cfg(skill_name)
    cache_cfg = cfg.get("cache") or {}
    return bool(cache_cfg.get("enabled", True))


def get_vector_index_path(skill_name: str, skill_root: Path | str) -> str:
    """Путь к FAISS-индексу: ``<default_root>/<index_name>``.

    Берёт первый индекс из ``vector_indexes[]``. Путь относительный —
    резолвится относительно ``skill_root`` (``workspace/skills/<name>``).
    """
    from config import SETTINGS

    vi_list = _vector_indexes_list(skill_name)
    vi_first = vi_list[0] if vi_list else {}
    name = vi_first.get("name", "")
    if not name:
        return ""
    vi_cfg = SETTINGS.get("gateway", {}).get("vector_index") or {}
    root = vi_cfg.get("default_root") or "data_store/vectors"
    p = Path(root) / name
    return str(p) if p.is_absolute() else str(Path(skill_root) / p)


def get_vector_db_table(skill_name: str) -> str:
    """Имя таблицы-хранилища векторов.

    Источник — ``gateway.vector_index.storage_table``. Fallback —
    ``tables[type="vector"]`` (для standalone-утилит без ``gateway.*``).
    """
    from config import SETTINGS

    vi_cfg = SETTINGS.get("gateway", {}).get("vector_index") or {}
    storage_table = vi_cfg.get("storage_table") or ""
    if storage_table:
        return storage_table

    for t in _tables_list(skill_name):
        if t.get("type") == "vector" and t.get("name"):
            return t["name"]
    return ""


def get_vector_store_table() -> str:
    """Имя таблицы serialized FAISS-индексов (инфраструктурная константа)."""
    return "public.agent_vector_index_store"


def build_cache_provider(skill_name: str, skill_root: Path | str) -> Any:
    from lib.services.cache_provider_impl import build_cache_provider as _build

    return _build(_skill_cfg(skill_name), str(skill_root))


def get_vector_indexes(skill_name: str) -> dict[str, Any]:
    """Метаданные индексов из ``public.agent_vector_index_config``."""
    from lib.services.cache_provider_impl import read_vector_index_config

    return read_vector_index_config(_skill_cfg(skill_name))


def get_embedding_config(skill_name: str) -> dict[str, Any]:
    from lib.services.cache_provider_impl import read_embedding_config

    return read_embedding_config(_skill_cfg(skill_name))


def get_embedding_model(skill_name: str) -> str:
    return get_embedding_config(skill_name).get("model", "mxbai-embed-large:latest")
