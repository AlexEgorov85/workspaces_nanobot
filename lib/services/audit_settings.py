"""
Единый источник правды для настроек навыка audit_analyzer.

Все пути/таблицы/параметры векторной синхронизации живут в project.json
(секция ``skills.audit_analyzer``) и читаются ТОЛЬКО отсюда через
``require_setting``. Код не должен дублировать эти значения литералами —
в противном случае изменение конфига «уходит» от кода и наступает
рассинхрон (например, «индексы не работают из БД»).

Потребители: gateway.py, application_context, AuditSyncService,
AuditMemoryStore, cache_provider_impl, tools/build_vectors.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List

from config import require_setting

_SECTION = ("skills", "audit_analyzer")


def normalize_additional_tables(value: Any) -> List[str]:
    """Привести db_additional_tables к списку ``"schema.table"`` строк.

    Допустимые форматы (как в ``cache_provider_impl._normalize_additional_tables``):
      - [["public", "agent_predefined_scripts"], ...]
      - [{"schema": "public", "table": "agent_predefined_scripts"}, ...]
      - ["public.agent_predefined_scripts", ...]

    Возвращает полные имена ``schema.table`` для передачи в AuditSyncService
    и AuditMemoryStore (оба умеют разбирать ``_split_table``).
    """
    out: List[str] = []
    if not value:
        return out
    for item in value:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            sch, tbl = item
            if sch and tbl:
                out.append(f"{sch}.{tbl}")
        elif isinstance(item, dict) and item.get("schema") and item.get("table"):
            out.append(f"{item['schema']}.{item['table']}")
        elif isinstance(item, str) and "." in item:
            sch, tbl = item.split(".", 1)
            if sch and tbl:
                out.append(item)
    return out


@dataclass(frozen=True)
class AuditVectorSettings:
    """Проектные настройки навыка audit_analyzer (из project.json)."""

    # --- база данных ---
    db_schema: str
    db_tables: List[str]
    db_additional_tables: List[List[str]]
    predefined_scripts_table: str

    # --- векторные таблицы / индексы ---
    mode_vector_db_table: str          # оарb.audit_vectors (сырые векторы, REAL[])
    mode_vector_store_table: str       # public.agent_vector_index_store (FAISS-blob BYTEA)
    mode_vector_index_config_table: str  # public.agent_vector_index_config
    vector_index_default_path: str     # путь к FAISS-индексам

    # --- эмбеддинги ---
    embedding_base_url: str
    embedding_model: str
    embedding_dimension: int
    embedding_http_timeout_sec: float

    # --- синхронизация (AuditSyncService) ---
    poll_interval_sec: float
    full_resync_every: int
    sync_max_queue_size: int
    reconnect_backoff_sec: float
    reconnect_backoff_max_sec: float

    # --- in-memory кэш (DuckDB) ---
    in_memory_enabled: bool
    in_memory_engine: str
    in_memory_cache_path: str
    cache_max_age_sec: int
    cache_refresh_interval_sec: int

    @property
    def vector_table_name(self) -> str:
        """Имя PostgreSQL-таблицы векторов в формате ``schema.table``."""
        return self.mode_vector_db_table

    @property
    def vector_schema_table(self) -> tuple[str, str]:
        """Разбить ``oarb.audit_vectors`` на ``(schema, table)``."""
        if "." in self.mode_vector_db_table:
            s, t = self.mode_vector_db_table.split(".", 1)
            return s, t
        return self.db_schema, self.mode_vector_db_table


def _require(keys: List[str]):
    return require_setting(*(_SECTION + tuple(keys)))


def audit_vector_settings() -> AuditVectorSettings:
    """Прочитать настройки audit_analyzer строго из project.json.

    Любой обязательный ключ, отсутствующий в конфиге, приводит к
    ``ConfigurationError`` — ошибка конфигурации видна сразу, а не
    маскируется подставным значением.
    """
    db_schema = _require(["db_schema"])
    db_tables = [t for t in (_require(["db_tables"]) or []) if t]
    additional = _require(["db_additional_tables"]) or []
    additional = [[str(s), str(t)] for s, t in additional] if isinstance(
        additional, list
    ) else []

    return AuditVectorSettings(
        db_schema=db_schema,
        db_tables=db_tables,
        db_additional_tables=additional,
        predefined_scripts_table=_require(["predefined_scripts_table"]),
        mode_vector_db_table=_require(["mode_vector_db_table"]),
        mode_vector_store_table=_require(["mode_vector_store_table"]),
        mode_vector_index_config_table=_require(["mode_vector_index_config_table"]),
        vector_index_default_path=_require(["vector_index_default_path"]),
        embedding_base_url=_require(["embedding_base_url"]),
        embedding_model=_require(["embedding_model"]),
        embedding_dimension=int(_require(["embedding_dimension"])),
        embedding_http_timeout_sec=float(_require(["embedding_http_timeout_sec"])),
        poll_interval_sec=float(_require(["poll_interval_sec"])),
        full_resync_every=int(_require(["full_resync_every"])),
        sync_max_queue_size=int(_require(["sync_max_queue_size"])),
        reconnect_backoff_sec=float(_require(["reconnect_backoff_sec"])),
        reconnect_backoff_max_sec=float(_require(["reconnect_backoff_max_sec"])),
        in_memory_enabled=bool(_require(["in_memory_enabled"])),
        in_memory_engine=_require(["in_memory_engine"]),
        in_memory_cache_path=_require(["in_memory_cache_path"]),
        cache_max_age_sec=int(_require(["cache_max_age_sec"])),
        cache_refresh_interval_sec=int(_require(["cache_refresh_interval_sec"])),
    )