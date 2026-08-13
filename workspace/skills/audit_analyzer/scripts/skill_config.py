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
    # Навык по умолчанию использует ТУ ЖЕ LLM, что и агент (agents.defaults +
    # providers.<provider> из config.json). Специфичные для навыка llm_* ключи
    # (project.json → skills.audit_analyzer) приоритетнее и могут перекрывать.
    # Таким образом смена провайдера/модели/ключа агента автоматически меняет
    # и LLM навыка, без дублирования секретов. Логика вынесена в общий модуль
    # lib/services/llm_config.py и переиспользуется бенчмарком.
    from lib.services.llm_config import resolve_llm_config

    return resolve_llm_config(overrides=_CFG)


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


def get_predefined_scripts_table() -> str:
    """
    Имя таблицы (схема.имя) с реестром предопределённых SQL-скриптов.

    Источник истины: project.json → skills.audit_analyzer.predefined_scripts_table.
    Используется:
      - db_loader.load_registry()  — читает реестр из БД
      - tools/generate_predefined_scripts_sql.py — генерирует INSERT в эту таблицу
    """
    return _CFG.get("predefined_scripts_table", "public.agent_predefined_scripts")


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
        path = _CFG.get("vector_index_default_path", "data_store/vectors/audits_index")
    p = Path(path)
    return str(p) if p.is_absolute() else str(_SKILL_ROOT / path)


def get_vector_db_table() -> str:
    return _CFG.get("mode_vector_db_table", "")


def get_vector_store_table() -> str:
    return _CFG.get("mode_vector_store_table", "public.agent_vector_index_store")


def build_cache_provider() -> Any:
    """Построить универсального провайдера данных (lib/services) для навыка.

    Конфигурируется из skills.audit_analyzer: DuckDB-кэш (in_memory_*)
    и векторные индексы (mode_vector_*). Используется CLI навыка напрямую —
    без промежуточных обёрток.
    """
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
    return {
        "default_mode": _CFG.get("cli_default_mode", "predefined"),
        "default_format": _CFG.get("cli_default_format", "json"),
        "max_retries": int(_CFG.get("cli_max_retries", 3)),
        "timeout_sec": int(_CFG.get("cli_timeout_sec", 60)),
    }


def get_max_retries() -> int:
    return int(_CFG.get("cli_max_retries", 3))
