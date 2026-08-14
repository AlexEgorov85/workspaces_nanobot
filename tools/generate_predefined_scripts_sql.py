"""
Генератор SQL для переноса реестра предопределённых SQL-скриптов
в таблицу, заданную в project.json → skills.audit_analyzer.predefined_scripts_table
(по умолчанию: public.agent_predefined_scripts).

Источник данных (выбирается флагом):
  --from-db    — читает SELECT из БД (через utils.db.fetch)
  --from-file  — импортирует SCRIPTS_REGISTRY из Python-файла
  --from-stdin — принимает JSON через stdin

Примеры:
  python tools/generate_predefined_scripts_sql.py --from-db
  python tools/generate_predefined_scripts_sql.py --from-file \\
      --source workspace/skills/audit_analyzer/scripts/predefined_scripts.py
  python tools/generate_predefined_scripts_sql.py --from-db --out migration.sql
  python tools/generate_predefined_scripts_sql.py --from-db --truncate

Чтобы сменить имя таблицы — отредактируйте
  project.json → skills.audit_analyzer.predefined_scripts_table
или передайте --table <schema.table> при запуске (перекрывает конфиг).
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

# Принудительно UTF-8 для stdout/stderr (на Windows cp866 ломает кириллицу)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# Подключаем корень проекта и workspace/ в sys.path — там лежат utils.db, config и т.д.
_ROOT = Path(__file__).resolve().parents[1]  # корень проекта
_WORKSPACE = _ROOT / "workspace"
for p in (str(_WORKSPACE), str(_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

# =============================================================================
# КОНФИГУРАЦИЯ
# =============================================================================
# Значение по умолчанию; перекрывается:
#   1) --table <schema.table> в CLI
#   2) project.json → skills.audit_analyzer.predefined_scripts_table
# Чтобы сменить «имя таблицы» — правьте project.json, см. skill_config.py.
FALLBACK_TABLE = "public.agent_predefined_scripts"


def get_target_table() -> str:
    """Прочитать имя таблицы из skill_config (читает project.json)."""
    try:
        from workspace.skills.audit_analyzer.scripts.skill_config import (
            get_predefined_scripts_table,
        )
        return get_predefined_scripts_table()
    except (ImportError, ModuleNotFoundError) as e:
        print(f"[WARN] skill_config не найден ({e}); используем {FALLBACK_TABLE!r}",
              file=sys.stderr)
        return FALLBACK_TABLE


# =============================================================================
# ИСТОЧНИКИ ДАННЫХ
# =============================================================================

def load_from_db() -> List[Dict[str, Any]]:
    """
    SELECT из PostgreSQL.
    DSN берётся через utils.db.resolve_dsn().
    JSONB из PG автоматически становится dict в Python.
    """
    from utils.db import fetch  # noqa: WPS433

    sql = f"""
        SELECT name, description, returns, long_description,
               sql_template, parameters, max_rows_default
        FROM {get_target_table()}
        ORDER BY name
    """
    rows = fetch(sql)
    # На всякий случай: если JSONB пришёл строкой
    for r in rows:
        if isinstance(r.get("parameters"), str):
            r["parameters"] = json.loads(r["parameters"])
    return rows


def load_from_file(path: Path) -> List[Dict[str, Any]]:
    """
    Импорт SCRIPTS_REGISTRY из Python-файла.
    Файл должен содержать:
        from scripts_registry import ParamDefinition, ScriptDefinition
        SCRIPTS_REGISTRY: Dict[str, ScriptDefinition] = { ... }

    Подразумевается, что `scripts_registry.py` лежит в одной папке
    с исходным файлом (или в sys.path уже добавлены нужные директории).
    """
    if not path.exists():
        raise FileNotFoundError(f"Source file not found: {path}")

    # scripts_registry.py лежит рядом с исходным файлом
    spec_dir = path.parent
    sys.path.insert(0, str(spec_dir))

    spec = importlib.util.spec_from_file_location("_predefined_src", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load spec from {path}")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except ModuleNotFoundError as e:
        raise ModuleNotFoundError(
            f"Failed to import {path}. The file probably depends on "
            f"`scripts_registry.py` — make sure it's in the same directory. "
            f"Original error: {e}"
        ) from e

    try:
        registry: Dict[str, Any] = mod.SCRIPTS_REGISTRY
    except AttributeError:
        raise AttributeError(
            f"{path} doesn't define SCRIPTS_REGISTRY. "
            f"Expected Dict[str, ScriptDefinition]."
        )

    rows: List[Dict[str, Any]] = []
    for name, sd in registry.items():
        params: Dict[str, Any] = {}
        for pname, pdef in (sd.parameters or {}).items():
            # dataclass → dict через __dict__, чтобы снять типизацию Literal
            params[pname] = {
                k: v for k, v in pdef.__dict__.items()
            }
        rows.append({
            "name": name,
            "description": sd.description,
            "returns": sd.returns or "",
            "long_description": sd.long_description or "",
            "sql_template": sd.sql_template,
            "parameters": params,
            "max_rows_default": sd.max_rows_default,
        })
    return rows


def load_from_stdin() -> List[Dict[str, Any]]:
    """Читает JSON-список скриптов из stdin."""
    data = json.load(sys.stdin)
    if not isinstance(data, list):
        raise ValueError("stdin JSON must be a list of script objects")
    return data


# =============================================================================
# ГЕНЕРАЦИЯ SQL
# =============================================================================

def _sql_literal(value: Any) -> str:
    """Python-значение → SQL-литерал через psycopg2-стиль."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def render_insert(
    rows: Iterable[Dict[str, Any]],
    table: str = FALLBACK_TABLE,
    on_conflict_update: bool = True,
) -> str:
    """
    Генерирует блок INSERT ... VALUES (...) для всех скриптов.

    Использует $tag$ ... $tag$ dollar-quoting для multi-line SQL/JSONB,
    чтобы не возиться с escape'ом кавычек внутри SQL-шаблонов.
    """
    rows = list(rows)
    if not rows:
        return "-- no rows\n"

    parts: List[str] = []
    parts.append("-- ============================================================================")
    parts.append(f"-- INSERT: {len(rows)} предопределённых скриптов в {table}")
    parts.append("-- Сгенерировано tools/generate_predefined_scripts_sql.py")
    parts.append("-- ============================================================================")
    parts.append("")
    parts.append(
        f"INSERT INTO {table} (name, description, returns, long_description, sql_template, parameters, max_rows_default) VALUES"
    )

    values_rows: List[str] = []
    for r in rows:
        sql_t = _dollar_quote(str(r["sql_template"]))
        params = r["parameters"] or {}
        jsonb = _dollar_quote(json.dumps(params, ensure_ascii=False, indent=4))
        values_rows.append(
            f"({_sql_literal(r['name'])},\n"
            f" {_sql_literal(r['description'])},\n"
            f" {_sql_literal(r['returns'] or '')},\n"
            f" {_sql_literal(r['long_description'] or '')},\n"
            f" {sql_t},\n"
            f" {jsonb}::jsonb,\n"
            f" {int(r['max_rows_default'])})"
        )

    parts.append(",\n".join(values_rows))
    if on_conflict_update:
        parts.append("")
        parts.append("ON CONFLICT (name) DO UPDATE SET")
        parts.append("    description      = EXCLUDED.description,")
        parts.append("    returns          = EXCLUDED.returns,")
        parts.append("    long_description = EXCLUDED.long_description,")
        parts.append("    sql_template     = EXCLUDED.sql_template,")
        parts.append("    parameters       = EXCLUDED.parameters,")
        parts.append("    max_rows_default = EXCLUDED.max_rows_default;")
    else:
        parts.append(";")

    return "\n".join(parts) + "\n"


def _dollar_quote(text: str) -> str:
    """
    Dollar-quoting для PostgreSQL: выбирает уникальный тег $tag$,
    чтобы тело с апострофами и $$ не сломало парсер.
    """
    tag = "src"
    while f"${tag}$" in text:
        tag += "x"
    return f"${tag}$\n{text}\n${tag}$"


def render_full_migration(
    rows: Iterable[Dict[str, Any]],
    table: str = FALLBACK_TABLE,
    on_conflict_update: bool = True,
    include_drop: bool = False,
) -> str:
    """Генерирует полный SQL-файл: пояснения + INSERT (без CREATE TABLE)."""
    rows_list = list(rows)
    parts: List[str] = []
    parts.append("-- ============================================================================")
    parts.append(f"-- Миграция: {len(rows_list)} предопределённых скриптов")
    parts.append(f"-- Таблица: {table}")
    parts.append("-- Сгенерировано: tools/generate_predefined_scripts_sql.py")
    parts.append("-- ============================================================================")
    parts.append("")
    parts.append(
        "-- Чтобы сменить таблицу, отредактируйте project.json → "
        "skills.audit_analyzer.predefined_scripts_table"
    )
    parts.append(
        "-- или передайте --table <schema.table> при запуске (перекрывает конфиг)."
    )
    parts.append("")

    if include_drop:
        parts.append(f"DROP TABLE IF EXISTS {table};")
        parts.append("")

    parts.append(
        render_insert(rows_list, table=table, on_conflict_update=on_conflict_update)
    )
    return "\n".join(parts)


# =============================================================================
# CLI
# =============================================================================

def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Генератор SQL для реестра предопределённых SQL-скриптов.",
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--from-db", action="store_true",
                     help="Читать из PostgreSQL (нужен DSN)")
    src.add_argument("--from-file", type=Path, metavar="PATH",
                     help="Импортировать SCRIPTS_REGISTRY из Python-файла")
    src.add_argument("--from-stdin", action="store_true",
                     help="Читать JSON-список из stdin")

    p.add_argument(
        "--table",
        default=None,
        help=(
            "Имя целевой таблицы (schema.table). По умолчанию берётся из "
            "project.json → skills.audit_analyzer.predefined_scripts_table; "
            "если не задано — 'public.agent_predefined_scripts'."
        ),
    )
    p.add_argument("--out", type=Path, default=None,
                   help="Куда записать SQL (по умолчанию: stdout)")
    p.add_argument("--truncate", action="store_true",
                   help="Добавить DROP TABLE IF EXISTS в начало")
    p.add_argument("--no-upsert", action="store_true",
                   help="Не добавлять ON CONFLICT DO UPDATE")
    return p.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)

    if args.from_db:
        rows = load_from_db()
    elif args.from_file:
        rows = load_from_file(args.from_file)
    else:
        rows = load_from_stdin()

    print(f"[INFO] loaded {len(rows)} scripts from source", file=sys.stderr)

    table = args.table or get_target_table()
    print(f"[INFO] target table: {table}", file=sys.stderr)

    sql = render_full_migration(
        rows,
        table=table,
        on_conflict_update=not args.no_upsert,
        include_drop=args.truncate,
    )

    if args.out:
        args.out.write_text(sql, encoding="utf-8")
        print(f"[OK] written: {args.out} ({len(sql)} bytes)", file=sys.stderr)
    else:
        # Buffered stdout для UTF-8 в Windows
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
        sys.stdout.write(sql)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
