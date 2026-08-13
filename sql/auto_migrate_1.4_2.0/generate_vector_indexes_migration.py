"""
Генератор SQL для миграции vector_indexes v1.4 → v2.0.

Запуск без параметров (из корня .nanobot):
    python sql/auto_migrate_1.4_2.0/generate_vector_indexes_migration.py

Выходной файл (рядом со скриптом):
    vector_indexes_migration.sql   — SQL upsert для public.agent_vector_index_config

DDL (CREATE TABLE, ADD COLUMN, индексы, переименования) НЕ генерируется —
это отдельный шаг (created_tables.sql + ручные ALTER/RENAME/CREATE INDEX).

Читает vector_indexes из (первый найденный):
    <корень>/v15_vector_indexes.json
    <корень>/data_store/cache/migration_v14/v15_vector_indexes.json
    <корень>/data_store/cache/migration_v14/vector_indexes.json
    <корень>/vector_indexes.json

GP 6.5 / PG 9.4+ совместимо (без ON CONFLICT — через DO-блок IF EXISTS).
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]  # корень .nanobot при запуске из sql/auto_migrate_1.4_2.0/

_SQL_OUT = _HERE / "vector_indexes_migration.sql"


def _log(m: str) -> None:
    print(f"[vector_indexes_migration] {m}", file=sys.stderr)


_VECTOR_INDEXES_CANDIDATES = [
    _ROOT / "v15_vector_indexes.json",
    _ROOT / "data_store" / "cache" / "migration_v14" / "v15_vector_indexes.json",
    _ROOT / "data_store" / "cache" / "migration_v14" / "vector_indexes.json",
    _ROOT / "vector_indexes.json",
]


def _load_vector_indexes() -> dict:
    for p in _VECTOR_INDEXES_CANDIDATES:
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                _log(f"vector_indexes загружены: {p} ({len(data)} индексов)")
                return data
            except Exception as e:
                _log(f"WARN: {p} не удалось распарсить: {e}")
    _log("vector_indexes: ни один файл не найден — SQL будет пустым")
    return {}


def _sql_str(v: str) -> str:
    return "'" + v.replace("'", "''") + "'"


def _sql_lit(v: str) -> str:
    tag = "sql"
    while f"${tag}$" in v:
        tag += "x"
    return f"${tag}${v}${tag}$"


def _normalize_row(index_name: str, cfg: dict) -> dict:
    content_cols = cfg.get("content_columns", [])
    embedding_cols = cfg.get("embedding_columns", content_cols)
    return {
        "index_name":    index_name,
        "source_table":  cfg.get("source_table", ""),
        "src_table":     cfg.get("table", ""),
        "pk_column":     cfg.get("pk", "id"),
        "content_cols":  content_cols,
        "embedding_cols": embedding_cols,
        "track_column":  cfg.get("track_column", "updated_at"),
        "enabled":       bool(cfg.get("enabled", True)),
    }


def _upsert_block(index_name: str, row: dict) -> str:
    name = _sql_str(index_name)
    content_cols_lit = _sql_lit(json.dumps(row["content_cols"], ensure_ascii=False))
    embedding_cols_lit = _sql_lit(json.dumps(row["embedding_cols"], ensure_ascii=False))
    enabled = "TRUE" if row["enabled"] else "FALSE"
    return f"""DO $upsert$
BEGIN
    IF EXISTS (
        SELECT 1 FROM public.agent_vector_index_config
        WHERE index_name = {name}
    ) THEN
        UPDATE public.agent_vector_index_config SET
            source_table  = {_sql_str(row['source_table'])},
            src_table     = {_sql_str(row['src_table'])},
            pk_column     = {_sql_str(row['pk_column'])},
            content_cols  = {content_cols_lit},
            embedding_cols= {embedding_cols_lit},
            track_column  = {_sql_str(row['track_column'])},
            enabled       = {enabled},
            updated_at    = NOW()
        WHERE index_name = {name};
    ELSE
        INSERT INTO public.agent_vector_index_config
            (index_name, source_table, src_table, pk_column,
             content_cols, embedding_cols, track_column, enabled,
             created_at, updated_at)
        VALUES
            ({name},
             {_sql_str(row['source_table'])},
             {_sql_str(row['src_table'])},
             {_sql_str(row['pk_column'])},
             {content_cols_lit},
             {embedding_cols_lit},
             {_sql_str(row['track_column'])},
             {enabled},
             NOW(), NOW());
    END IF;
END
$upsert$;""".rstrip()


def render_sql(raw: dict) -> str:
    parts: list[str] = []
    parts.append("-- =====================================================================")
    parts.append("-- vector_indexes_migration.sql — перенос данных vector_indexes v1.4 → v2.0")
    parts.append("-- Сгенерировано автоматически. Совместимо с Greenplum 6.5 / PostgreSQL 9.4+")
    parts.append("-- Требует, чтобы public.agent_vector_index_config уже была создана.")
    parts.append("-- Применение:  psql -d <db> -f vector_indexes_migration.sql")
    parts.append("-- =====================================================================")
    parts.append("")

    if not raw:
        parts.append("-- vector_indexes: нет данных — нечего переносить.")
        return "\n".join(parts)

    parts.append("-- ===== Upsert vector_indexes → public.agent_vector_index_config =====")
    parts.append("")
    for index_name, cfg in raw.items():
        if not isinstance(cfg, dict):
            continue
        row = _normalize_row(index_name, cfg)
        parts.append(f"-- {index_name} → {row['src_table']}")
        parts.append(_upsert_block(index_name, row))
        parts.append("")

    return "\n".join(parts)


def main() -> int:
    _log(f"ROOT = {_ROOT}")
    raw = _load_vector_indexes()
    _SQL_OUT.write_text(render_sql(raw), encoding="utf-8")
    _log(f"OK → {_SQL_OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
