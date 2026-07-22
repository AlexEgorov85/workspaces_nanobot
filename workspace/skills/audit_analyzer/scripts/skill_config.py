import json
from pathlib import Path
from typing import Any

_SKILL_ROOT = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _SKILL_ROOT.parents[2]  # workspace/ → .nanobot/

import sys
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config import SETTINGS

_CFG = SETTINGS.get("skills", {}).get("audit_analyzer", {})


def get_llm_config() -> dict[str, Any]:
    return {
        "provider": _CFG.get("llm_provider", "mistral"),
        "model": _CFG.get("llm_model", "mistral-large-latest"),
        "api_base": _CFG.get("llm_api_base", "https://api.mistral.ai/v1"),
        "api_key": _CFG.get("llm_api_key", ""),
        "max_tokens": int(_CFG.get("llm_max_tokens", 8192)),
        "temperature": float(_CFG.get("llm_temperature", 0.1)),
    }


def get_tool_config() -> dict[str, Any]:
    return dict(_CFG)


def load_db_config() -> dict[str, Any]:
    db_schema = _CFG.get("db_schema", "oarb")
    tables = get_db_tables()
    return {"schema": db_schema, "tables": tables}


def get_db_tables() -> list[str]:
    val = _CFG.get("db_tables", [])
    return list(val) if isinstance(val, (list, tuple)) else []


def get_db_schema() -> str:
    return _CFG.get("db_schema", "oarb")


def get_in_memory_config() -> dict[str, Any]:
    path = _CFG.get("in_memory_cache_path", "cache/audit_cache.duckdb")
    p = Path(path)
    if not p.is_absolute():
        path = str(_SKILL_ROOT / path)
    return {
        "enabled": bool(_CFG.get("in_memory_enabled", True)),
        "engine": _CFG.get("in_memory_engine", "duckdb"),
        "cache_path": path,
    }


def is_in_memory_enabled() -> bool:
    return bool(_CFG.get("in_memory_enabled", True))


def get_vector_index_path() -> str:
    path = _CFG.get("mode_vector_index_path", "")
    if not path:
        return str(Path.home() / ".nanobot" / "vectors" / "audits_index")
    p = Path(path)
    return str(p) if p.is_absolute() else str(_SKILL_ROOT / path)


def get_vector_db_table() -> str:
    return _CFG.get("mode_vector_db_table", "")


def get_vector_indexes() -> dict[str, Any]:
    from utils.db import fetch
    try:
        rows = fetch(
            "SELECT index_name, source_table, src_table, pk_column, "
            "content_cols, embedding_cols, track_column, enabled "
            "FROM oarb.vector_index_config ORDER BY index_name"
        )
        if rows:
            result = {}
            for r in rows:
                ec = r["embedding_cols"]
                if isinstance(ec, str):
                    ec = json.loads(ec)
                result[r["index_name"]] = {
                    "table": r["src_table"],
                    "pk": r["pk_column"],
                    "source_table": r["source_table"],
                    "content_columns": list(r["content_cols"]) if isinstance(r.get("content_cols"), (list, tuple)) else [],
                    "embedding_columns": ec,
                    "track_column": r["track_column"],
                    "enabled": r["enabled"],
                }
            return result
    except Exception:
        pass
    val = _CFG.get("vector_indexes", {})
    return dict(val) if isinstance(val, dict) else {}


def get_embedding_config() -> dict[str, Any]:
    return {
        "base_url": _CFG.get("embedding_base_url", "http://localhost:11434/api/embed"),
        "model": _CFG.get("embedding_model", "mxbai-embed-large:latest"),
        "dimension": int(_CFG.get("embedding_dimension", 1024)),
    }


def get_embedding_model() -> str:
    return get_embedding_config().get("model", "mxbai-embed-large:latest")


def get_cli_config() -> dict[str, Any]:
    return {
        "default_mode": _CFG.get("cli_default_mode", "predefined"),
        "default_format": _CFG.get("cli_default_format", "json"),
        "max_retries": int(_CFG.get("cli_max_retries", 3)),
        "timeout_sec": int(_CFG.get("cli_timeout_sec", 60)),
    }


def get_max_retries() -> int:
    return int(_CFG.get("cli_max_retries", 3))
