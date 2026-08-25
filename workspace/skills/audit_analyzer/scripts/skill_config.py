from pathlib import Path
from typing import Any

_SKILL_ROOT = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _SKILL_ROOT.parents[2]  # workspace/ → корень проекта

import sys  # noqa: E402

if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config import SETTINGS  # noqa: E402

_CFG = SETTINGS.get("skills", {}).get("audit_analyzer", {})


def _tables_list() -> list[dict]:
    """``_CFG["tables"]`` as list of dicts (pydantic already validated)."""
    raw = _CFG.get("tables") or []
    return [t if isinstance(t, dict) else {"name": t} for t in raw]


def _vector_indexes_list() -> list[dict]:
    """``_CFG["vector_indexes"]`` as list of dicts."""
    raw = _CFG.get("vector_indexes") or []
    return [v for v in raw if isinstance(v, dict)]


def get_llm_config() -> dict[str, Any]:
    from lib.services.llm_config import resolve_llm_config
    return resolve_llm_config(overrides=_CFG)


def get_tool_config() -> dict[str, Any]:
    return dict(_CFG)


def load_db_config() -> dict[str, Any]:
    return {"schema": get_db_schema(), "tables": get_db_tables()}


def get_db_tables() -> list[str]:
    """Доменные таблицы skill'а для LLM-описания схемы.

    Возвращает имена таблиц из ``tables[]`` **без** ``label`` —
    это доменные таблицы, которые попадают в описание схемы для LLM.
    Таблицы с ``label`` (например, ``public.agent_predefined_scripts``
    с ``label="scripts_registry"``) — реестры метаданных; они доступны
    через ``TableRegistry.resources_by_label(label)`` и в описание схемы
    не попадают, чтобы не путать LLM.
    """
    out: list[str] = []
    for t in _tables_list():
        name = t.get("name")
        if name and not t.get("label"):
            out.append(name)
    return out


def get_db_schema() -> str:
    tables = _tables_list()
    if not tables:
        raise ValueError(
            "skill audit_analyzer: skills.audit_analyzer.tables пуст "
            "(нет ни одной таблицы)"
        )
    first = tables[0].get("name", "")
    if "." in first:
        return first.split(".", 1)[0]
    raise ValueError(
        f"skill audit_analyzer: первая таблица '{first}' не fully qualified "
        "(ожидается 'schema.table')"
    )


def get_predefined_scripts_table() -> str:
    from lib.services.table_registry import table_registry

    rs = table_registry.resources_by_label("scripts_registry")
    if rs:
        return rs[0].name

    raise ValueError(
        "skill audit_analyzer: ни один skill не зарегистрировал ресурс "
        "с label='scripts_registry' (ожидалось из "
        "project.json::skills.<name>.tables[].label='scripts_registry'). "
        "Запустите через gateway (ApplicationContext)."
    )


def get_in_memory_config() -> dict[str, Any]:
    from lib.services.table_registry import table_registry

    workspace_root = _SKILL_ROOT.parent.parent
    path = str(table_registry.snapshot_path(workspace_root))

    cache_cfg = _CFG.get("cache") or {}
    return {
        "enabled": bool(cache_cfg.get("enabled", True)),
        "engine": cache_cfg.get("engine", "duckdb"),
        "cache_path": path,
    }


def is_in_memory_enabled() -> bool:
    cache_cfg = _CFG.get("cache") or {}
    return bool(cache_cfg.get("enabled", True))


def get_vector_index_path() -> str:
    vi_list = _vector_indexes_list()
    vi_first = vi_list[0] if vi_list else {}
    name = vi_first.get("name", "")
    if not name:
        return ""
    vi_cfg = SETTINGS.get("gateway", {}).get("vector_index") or {}
    root = vi_cfg.get("default_root") or "data_store/vectors"
    p = Path(root) / name
    return str(p) if p.is_absolute() else str(_SKILL_ROOT / p)


def get_vector_db_table() -> str:
    """Имя таблицы-хранилища векторов (для ``PostgresDuckDbProvider.vector_db_table``).

    Источник: ``gateway.vector_index.storage_table``. Storage-таблица —
    общая инфраструктура, не относится к конкретному навыку.
    """
    vi_cfg = SETTINGS.get("gateway", {}).get("vector_index") or {}
    storage_table = vi_cfg.get("storage_table") or ""
    if storage_table:
        return storage_table

    for t in _tables_list():
        if t.get("type") == "vector" and t.get("name"):
            return t["name"]
    return ""


def get_vector_store_table() -> str:
    return "public.agent_vector_index_store"


def build_cache_provider() -> Any:
    from lib.services.cache_provider_impl import build_cache_provider as _build
    return _build(_CFG, str(_SKILL_ROOT))


def get_vector_indexes() -> dict[str, Any]:
    from lib.services.cache_provider_impl import read_vector_index_config
    return read_vector_index_config(_CFG)


def get_embedding_config() -> dict[str, Any]:
    from lib.services.cache_provider_impl import read_embedding_config
    return read_embedding_config(_CFG)


def get_embedding_model() -> str:
    return get_embedding_config().get("model", "mxbai-embed-large:latest")


def get_cli_config() -> dict[str, Any]:
    cli_cfg = _CFG.get("cli") or {}
    return {
        "default_mode": cli_cfg.get("default_mode", "predefined"),
        "default_format": cli_cfg.get("default_format", "json"),
        "max_retries": int(cli_cfg.get("max_retries", 3)),
        "timeout_sec": int(cli_cfg.get("timeout_sec", 60)),
    }


def get_max_retries() -> int:
    cli_cfg = _CFG.get("cli") or {}
    return int(cli_cfg.get("max_retries", 3))
