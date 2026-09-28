"""Аудит контрактов nanobot: AST нашего кода vs реальные символы nanobot 0.3.5.

Что делает:
  1. AST-обход всех ``.py`` в ``lib/``, ``workspace/``, ``tools/``.
  2. Собирает ``from nanobot.X import Y`` / ``import nanobot.X.Y`` /
     контекстный ``var.attr`` (где ``var`` — нанотип).
  3. Для каждого символа пытается зарезолвить через ``importlib`` +
     ``getattr`` (с авто-линковкой ``parent.child = submod``).
  4. Печатает MISSING — символы, которые наш код использует, но
     в реальном nanobot 0.3.5 не существуют (или доступны иначе).

Запуск:
  python tools/audit_nanobot_contracts.py [--json out.json]

Используется как первая линия обороны при апгрейдах nanobot:
  «наш код тянет ``nanobot.X`` — а есть ли ``X`` в новой версии?»
"""
from __future__ import annotations

import argparse
import ast
import dataclasses
import importlib
import inspect
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = [ROOT / "lib", ROOT / "workspace", ROOT / "tools"]
SKIP_DIRS = {"__pycache__", ".git", "node_modules", "data_store", "cache", ".venv"}


def _iter_py_files() -> list[Path]:
    import os
    out: set[Path] = set()
    for root in SCAN_DIRS:
        if not root.exists():
            continue
        for dp, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for n in files:
                if n.endswith(".py"):
                    out.add(Path(dp) / n)
    return sorted(out)


def _walk_dotted(dotted: str) -> object | None:
    """Идёт ``nanobot → agent → loop → AgentLoop → from_config``."""
    parts = dotted.split(".")
    cur: object = None
    for i in range(1, len(parts) + 1):
        full_so_far = ".".join(parts[:i])
        try:
            mod = importlib.import_module(full_so_far)
        except ImportError:
            if cur is None:
                return None
            try:
                mod = getattr(cur, parts[i - 1])
            except AttributeError:
                return None
        if i > 1 and cur is not None and not hasattr(cur, parts[i - 1]):
            try:
                setattr(cur, parts[i - 1], mod)
            except (AttributeError, TypeError):
                pass
        cur = mod
    return cur


def _kind(obj) -> str:
    if obj is None:
        return "MISSING"
    if inspect.ismodule(obj):
        return "module"
    if inspect.isclass(obj):
        return "class"
    if inspect.isfunction(obj):
        return "function"
    if inspect.ismethod(obj) or inspect.isbuiltin(obj):
        return "method"
    if dataclasses.is_dataclass(obj):
        return "dataclass"
    return type(obj).__name__


def audit() -> list[dict]:
    findings: list[dict] = []
    for path in _iter_py_files():
        src = path.read_text(encoding="utf-8", errors="ignore")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue

        rel = path.relative_to(ROOT).as_posix()

        imports: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("nanobot"):
                for n in node.names:
                    local = n.asname or n.name
                    imports[local] = f"{node.module}.{n.name}"
            elif isinstance(node, ast.Import):
                for n in node.names:
                    if n.name.startswith("nanobot"):
                        local = n.asname or n.name.split(".")[0]
                        imports[local] = n.name

        for local, dotted in imports.items():
            obj = _walk_dotted(dotted)
            findings.append({
                "file": rel,
                "kind": "import",
                "name": local,
                "dotted": dotted,
                "ok": obj is not None,
                "kind_of": _kind(obj),
            })

        for fn_node in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            local_imports = dict(imports)
            for node in ast.walk(fn_node):
                if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("nanobot"):
                    for n in node.names:
                        local_imports[n.asname or n.name] = f"{node.module}.{n.name}"

            local_new: dict[str, str] = {}
            for sub in ast.walk(fn_node):
                if isinstance(sub, ast.Assign) and isinstance(sub.value, ast.Call) and isinstance(sub.value.func, ast.Name):
                    cls_name = sub.value.func.id
                    if cls_name in local_imports:
                        for tgt in sub.targets:
                            if isinstance(tgt, ast.Name):
                                local_new[tgt.id] = local_imports[cls_name]

            for sub in ast.walk(fn_node):
                if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name):
                    base = sub.value.id
                    attr = sub.attr
                    dotted_base = local_new.get(base) or local_imports.get(base)
                    if dotted_base is None:
                        continue
                    full = dotted_base + "." + attr
                    obj = _walk_dotted(full)
                    findings.append({
                        "file": rel,
                        "line": sub.lineno,
                        "kind": "attr_access",
                        "name": f"{base}.{attr}",
                        "dotted": full,
                        "ok": obj is not None,
                        "kind_of": _kind(obj),
                    })
    return findings


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", type=str, default=None)
    args = parser.parse_args()

    findings = audit()
    import_count = sum(1 for f in findings if f["kind"] == "import")
    access_count = sum(1 for f in findings if f["kind"] != "import")
    missing = [f for f in findings if not f["ok"]]
    print(f"Imports: {import_count}")
    print(f"Attribute accesses: {access_count}")
    print(f"MISSING: {len(missing)}")
    print()
    if missing:
        seen: set[str] = set()
        print("=" * 80)
        print("REAL MISSING — check это вручную (audit имеет False Positives")
        print("на dataclass fields / private instance attrs)")
        print("=" * 80)
        for f in missing:
            key = f["dotted"]
            if key in seen:
                continue
            seen.add(key)
            print(f"  [{f['kind']:11s}] {f['dotted']:70s}  ({f['kind_of']})")
            print(f"    in {f['file']}:{f.get('line', '?')}")

    if args.json:
        Path(args.json).write_text(
            json.dumps(findings, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
