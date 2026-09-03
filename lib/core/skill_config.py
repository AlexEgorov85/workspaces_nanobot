"""Runtime API для skill'ов: конфигурация, таблицы, FAISS.

Параметризован по ``skill_name``. Каждый skill вызывает функции со своим
именем (например, ``get_db_tables("audit_analyzer")``). Это единая точка
для всех skill'ов — никакой копипасты между skill'ами.

Реализация читает секцию ``project.json::skills.<name>`` через
``config.SETTINGS`` и табличный реестр через ``lib.services.table_registry``.

Embedding-конфиг (``get_embedding_config``, ``get_embedding_model``)
больше НЕ параметризован по ``skill_name``: после commit «skill
configuration boundary» embedding — общая runtime-инфраструктура
(``gateway.vector.embedding``), а не свойство skill-домена. Эти функции
читают напрямую из ``table_registry.embedding_config()`` (положен туда
на старте gateway через ``register_embedding_config``).
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


def get_chunking_config(skill_name: str) -> dict[str, Any]:
    """Параметры map-reduce чанкинга из ``skills.<name>.chunking.*``.

    Дефолты согласованы с прежней реализацией навыка ``legal_summarizer``
    (chunk 100 000 симв., overlap 2 000, single-call threshold 20 000) и
    с дефолтами ``lib.services.text_splitter.split_text`` для коротких
    текстов (там ``chunk_size=500`` — но для LLM-prompt обычно
    крупнее). ``chunk_size_input_ratio`` - доля от контекстного окна
    LLM (``agents.defaults.contextWindowTokens``); если задана, skill
    пересчитывает ``chunk_size`` динамически от контекста.

    ``brief_truncate_chars_per_block``: ограничение по символам для
    представления каждого chunk'а в brief-режиме (null = без обрезки).
    """

    cfg = _skill_cfg(skill_name)
    chunking_cfg = cfg.get("chunking") or {}
    ratio = chunking_cfg.get("chunk_size_input_ratio")
    brief_truncate = chunking_cfg.get("brief_truncate_chars_per_block")
    return {
        "chunk_size": int(chunking_cfg.get("chunk_size", 100000)),
        "chunk_overlap": int(chunking_cfg.get("chunk_overlap", 2000)),
        "single_call_threshold": int(
            chunking_cfg.get("single_call_threshold", 20000)
        ),
        "chunk_size_input_ratio": float(ratio) if ratio is not None else None,
        "brief_truncate_chars_per_block": (
            int(brief_truncate) if brief_truncate is not None else None
        ),
    }


def get_in_memory_cache_path(skill_root: Path | str) -> str:
    """Путь к единому DuckDB-снапшоту runtime-кэша.

    Снимок общий для всех skill'ов (``workspace/data_store/duckdb/cache.duckdb``,
    см. ``TableRegistry.snapshot_path()``) — это свойство runtime-инфраструктуры,
    а не skill-домена. Поэтому функция НЕ параметризована ``skill_name``.

    Заменила ранее существовавшую ``get_in_memory_config(skill_name,
    skill_root)``, которая возвращала ещё ``enabled`` / ``engine`` из
    ``skills.<name>.cache.*``. Эти поля были мёртвыми (``enabled``
    нигде не проверялся, ``engine`` нигде не использовался,
    ``max_age_sec`` / ``refresh_interval_sec`` не пробрасывались в
    ``PostgresDuckDbProvider``). См. commit «skill configuration boundary».
    """
    from lib.services.table_registry import table_registry

    workspace_root = Path(skill_root).parent.parent
    return str(table_registry.snapshot_path(workspace_root))


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
    vector_cfg = SETTINGS.get("gateway", {}).get("vector") or {}
    index_cfg = vector_cfg.get("index") or {}
    root = index_cfg.get("default_root") or "data_store/vectors"
    p = Path(root) / name
    return str(p) if p.is_absolute() else str(Path(skill_root) / p)


def get_vector_db_table(skill_name: str) -> str:
    """Имя таблицы-хранилища векторов.

    Источник — ``gateway.vector.index.storage_table``. Fallback —
    ``tables[type="vector"]`` (для standalone-утилит без ``gateway.*``).
    """
    from config import SETTINGS

    vector_cfg = SETTINGS.get("gateway", {}).get("vector") or {}
    index_cfg = vector_cfg.get("index") or {}
    storage_table = index_cfg.get("storage_table") or ""
    if storage_table:
        return storage_table

    for t in _tables_list(skill_name):
        if t.get("type") == "vector" and t.get("name"):
            return t["name"]
    return ""


def get_vector_store_table() -> str:
    """Имя таблицы serialized FAISS-индексов (из runtime-настроек).

    Источник — ``gateway.vector.index.signature_table`` (см.
    ``VectorIndexSettings.signature_table``; дефолт —
    ``_DEFAULT_VECTOR_INDEX_STORE_TABLE`` в ``cache_provider_impl``,
    DDL в ``sql/vectors/create_vector_index_store.sql``).
    """
    from lib.services.cache_provider_impl import read_vector_store_table

    return read_vector_store_table()


def build_cache_provider(skill_name: str, skill_root: Path | str) -> Any:
    from lib.services.cache_provider_impl import build_cache_provider as _build

    return _build(_skill_cfg(skill_name), str(skill_root))


def get_vector_indexes(skill_name: str) -> dict[str, Any]:
    """Метаданные индексов из PG-реестра (``read_vector_index_config_table()``,
    см. ``VectorIndexSettings.config_table``)."""
    from lib.services.cache_provider_impl import read_vector_index_config

    return read_vector_index_config(_skill_cfg(skill_name))


def get_embedding_config() -> dict[str, Any]:
    """Embedding-конфиг из общего runtime-реестра.

    Источник — ``gateway.vector.embedding`` (положен в ``TableRegistry``
    на старте gateway через ``register_embedding_config``). Не
    параметризовано ``skill_name``: embedding — общая инфраструктура.
    """
    from lib.services.table_registry import table_registry

    return table_registry.embedding_config()


def get_embedding_model() -> str:
    return get_embedding_config().get("model", "mxbai-embed-large:latest")
