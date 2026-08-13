"""
Генератор SQL для миграции реестра предопределённых SQL-скриптов v1.4 → v2.0.

Запуск без параметров (из корня .nanobot):
    python sql/auto_migrate_1.4_2.0/generate_predefined_scripts_migration.py

Выходной файл (рядом со скриптом):
    predefined_scripts_migration.sql   — SQL DELETE+INSERT для public.agent_predefined_scripts

DDL (CREATE TABLE) НЕ генерируется — отдельный шаг (created_tables.sql).

Источник v1.4 (парсится через AST, без exec/import — безопасно):
    <корень>/data_store/cache/migration_v14/workspace/skills/audit_analyzer/scripts/scripts_registry.py

GP 6.5 / PG 9.4+ совместимо (без ON CONFLICT — DELETE+INSERT).
"""
from __future__ import annotations
import ast
import json
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]

_DEFAULT_SOURCE = (
    _ROOT
    / "data_store"
    / "cache"
    / "migration_v14"
    / "workspace"
    / "skills"
    / "audit_analyzer"
    / "scripts"
    / "scripts_registry.py"
)

_SQL_OUT = _HERE / "predefined_scripts_migration.sql"


def _log(m: str) -> None:
    print(f"[predefined_scripts_migration] {m}", file=sys.stderr)


_UNSET = object()


def _parse_literal(node: ast.AST) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _parse_literal(node.operand)
        if isinstance(inner, (int, float)):
            return -inner
    if isinstance(node, ast.List):
        return [_parse_literal(x) for x in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_parse_literal(x) for x in node.elts)
    if isinstance(node, ast.Dict):
        return {_parse_literal(k): _parse_literal(v) for k, v in zip(node.keys, node.values)}
    return _UNSET


def _extract_param_def(node: ast.Call) -> dict[str, Any] | None:
    out: dict[str, Any] = {}
    for kw in node.keywords:
        if kw.arg is None:
            continue
        val = _parse_literal(kw.value)
        if val is _UNSET:
            return None
        out[kw.arg] = val
    return out


def parse_v14_registry(source: Path) -> list[dict[str, Any]]:
    if not source.exists():
        raise FileNotFoundError(f"v1.4 registry не найден: {source}")

    tree = ast.parse(source.read_text(encoding="utf-8"))
    scripts: list[dict[str, Any]] = []

    for node in ast.iter_child_nodes(tree):
        target_id = None
        if isinstance(node, ast.Assign):
            if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
                continue
            target_id = node.targets[0].id
        elif isinstance(node, ast.AnnAssign):
            if not isinstance(node.target, ast.Name):
                continue
            target_id = node.target.id
        else:
            continue
        if target_id != "SCRIPTS_REGISTRY":
            continue
        if not isinstance(node.value, ast.Dict):
            continue

        for key_node, value_node in zip(node.value.keys, node.value.values):
            if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, str):
                continue
            if not isinstance(value_node, ast.Call):
                continue

            kwargs: dict[str, Any] = {}
            for kw in value_node.keywords:
                if kw.arg is None:
                    continue
                kwargs[kw.arg] = kw.value

            name = _parse_literal(kwargs.get("name")) or key_node.value
            description = _parse_literal(kwargs.get("description")) or ""
            sql_template = _parse_literal(kwargs.get("sql_template"))
            if not isinstance(sql_template, str):
                _log(f"WARN: {name}: sql_template не str — пропуск")
                continue
            max_rows_default = _parse_literal(kwargs.get("max_rows_default", 1000))
            returns = _parse_literal(kwargs.get("returns", "")) or ""
            long_description = _parse_literal(kwargs.get("long_description", "")) or ""

            parameters: dict[str, Any] = {}
            params_node = kwargs.get("parameters")
            if isinstance(params_node, ast.Dict):
                for pkey, pval in zip(params_node.keys, params_node.values):
                    if not isinstance(pkey, ast.Constant) or not isinstance(pkey.value, str):
                        continue
                    if not isinstance(pval, ast.Call):
                        continue
                    pdef = _extract_param_def(pval)
                    if pdef is None:
                        pdef = {"type": "exact", "required": False}
                    parameters[pkey.value] = pdef

            scripts.append({
                "name": name,
                "description": description.strip(),
                "sql_template": sql_template,
                "parameters": parameters,
                "max_rows_default": int(max_rows_default) if max_rows_default is not None else 1000,
                "returns": returns.strip(),
                "long_description": long_description.strip(),
            })

    return scripts


def _sql_str(v: str) -> str:
    return "'" + v.replace("'", "''") + "'"


def _sql_lit(v: str) -> str:
    tag = "sql"
    while f"${tag}$" in v:
        tag += "x"
    return f"${tag}${v}${tag}$"


def _json_lit(v: dict) -> str:
    text = json.dumps(v, ensure_ascii=False, indent=4, sort_keys=True)
    tag = "json"
    while f"${tag}$" in text:
        tag += "x"
    return f"${tag}${text}${tag}$::jsonb"


def render_sql(scripts: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    parts.append("-- =====================================================================")
    parts.append("-- predefined_scripts_migration.sql — перенос реестра SQL-скриптов v1.4 → v2.0")
    parts.append(f"-- Сгенерировано автоматически: {len(scripts)} скриптов")
    parts.append("-- Совместимо с Greenplum 6.5 / PostgreSQL 9.4+ (без ON CONFLICT)")
    parts.append("-- Требует, чтобы public.agent_predefined_scripts уже была создана.")
    parts.append("-- Применение:  psql -d <db> -f predefined_scripts_migration.sql")
    parts.append("-- =====================================================================")
    parts.append("")

    if not scripts:
        parts.append("-- скрипты не найдены — нечего переносить.")
        return "\n".join(parts)

    table = "public.agent_predefined_scripts"

    names = sorted(s["name"] for s in scripts)
    name_list = ", ".join(_sql_str(n) for n in names)
    parts.append("-- удалить старые записи с этими именами (если есть)")
    parts.append(f"DELETE FROM {table} WHERE name IN ({name_list});")
    parts.append("")

    parts.append(f"INSERT INTO {table} (")
    parts.append("    name,")
    parts.append("    description,")
    parts.append("    sql_template,")
    parts.append("    parameters,")
    parts.append("    max_rows_default,")
    parts.append("    returns,")
    parts.append("    long_description")
    parts.append(") VALUES")
    parts.append("")

    rows: list[str] = []
    for s in scripts:
        params = s.get("parameters", {})
        values = [
            "    " + _sql_str(s["name"]),
            "    " + _sql_str(s["description"]),
            "    " + _sql_lit("\n" + s["sql_template"].strip("\n") + "\n"),
            "    " + _json_lit(params),
            f"    {int(s['max_rows_default'])}",
            "    " + _sql_str(s["returns"]),
            "    " + _sql_str(s["long_description"]),
        ]
        rows.append("(\n" + ",\n".join(values) + "\n)")

    parts.append(",\n\n".join(rows))
    parts.append(";")
    parts.append("")
    return "\n".join(parts)


def main() -> int:
    _log(f"ROOT = {_ROOT}")
    source = _DEFAULT_SOURCE
    if not source.exists():
        _log(f"ERROR: источник не найден: {source}")
        return 1

    scripts = parse_v14_registry(source)
    _log(f"распарсено скриптов: {len(scripts)}")
    for s in scripts:
        n_params = len(s.get("parameters", {}))
        _log(f"  - {s['name']}: {n_params} param(s), max_rows={s['max_rows_default']}")

    _SQL_OUT.write_text(render_sql(scripts), encoding="utf-8")
    _log(f"OK → {_SQL_OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
