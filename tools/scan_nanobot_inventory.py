"""Одноразовый сканер nanobot-зависимостей: строит JSON-инвентарь.

Запуск: python tools/scan_nanobot_inventory.py [--out docs/architecture/nanobot-inventory.json]
Сканирует lib/, workspace/, gateway.py, cli_agent.py, streamlit_app.py.
Не трогает tests/, benchmarks/, .venv/.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SCAN_PATHS = ["lib", "workspace", "gateway.py", "cli_agent.py", "streamlit_app.py"]
EXCLUDE = ("__pycache__", ".venv", "tests", "benchmarks", "data_store")

IMPORT_RE = re.compile(r"^\s*(?:from (nanobot[\w.]*) import ([^\n#]+)|import (nanobot[\w.]*))")
GETATTR_PRIVATE_RE = re.compile(
    r"getattr\(\s*([\w.]+)\s*,\s*[\"'](_[\w]+)[\"']", re.MULTILINE
)
DIRECT_PRIVATE_RE = re.compile(r"\.\s*_([a-z][\w]*)\b")
SETATTR_RE = re.compile(
    r"setattr\(\s*([\w.]+)\s*,\s*[\"']([\w]+)[\"']|(\w+(?:\.\w+)+)\s*=\s*(?!==)"
)


def iter_py_files() -> list[Path]:
    files: list[Path] = []
    for entry in SCAN_PATHS:
        p = ROOT / entry
        if p.is_file():
            files.append(p)
        elif p.is_dir():
            for f in p.rglob("*.py"):
                if any(part in EXCLUDE for part in f.parts):
                    continue
                files.append(f)
    return sorted(files)


def classify(import_module: str, names: str) -> str:
    private_names = [n.strip().lstrip("*").strip() for n in names.split(",")] if names else []
    if any(n.startswith("_") for n in private_names if n):
        return "RED"
    internal_markers = (
        "agent.context_governance",
        "agent.subagent",
        "utils.prompt_templates",
        "agent.tools.context",
    )
    if any(m in import_module for m in internal_markers):
        return "ORANGE"
    if import_module == "nanobot" or import_module.startswith("nanobot.cli.commands"):
        return "YELLOW"
    return "GREEN"


def scan_file(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    rel = str(path.relative_to(ROOT)).replace("\\", "/")

    imports = []
    for i, line in enumerate(text.splitlines(), 1):
        m = IMPORT_RE.match(line)
        if not m:
            # продолжение многострочного from-import
            continue
        module = m.group(1) or m.group(3) or ""
        names = (m.group(2) or "").strip()
        imports.append(
            {
                "line": i,
                "statement": line.strip(),
                "module": module,
                "names": [n.strip() for n in names.split(",") if n.strip()] if names else [],
                "classification": classify(module, names),
            }
        )
    # многострочные from-import: догоняем имена до закрывающей скобки
    lines = text.splitlines()
    for imp in imports:
        if "(" in imp["statement"] and ")" not in imp["statement"]:
            j = imp["line"]  # 1-based -> индекс следующей
            while j < len(lines) and ")" not in lines[j]:
                for n in re.findall(r"[A-Za-z_][\w]*", lines[j]):
                    if n not in ("import",):
                        imp["names"].append(n)
                j += 1
            if j < len(lines):
                for n in re.findall(r"[A-Za-z_][\w]*", lines[j]):
                    if n != ")":
                        imp["names"].append(n)
            imp["names"] = list(dict.fromkeys(imp["names"]))
            if any(n.startswith("_") for n in imp["names"]):
                imp["classification"] = "RED"
            elif imp["classification"] == "YELLOW" and not any(
                n.startswith("_") for n in imp["names"]
            ):
                pass

    getattr_private = [
        {"line": (text[:m.start()].count("\n") + 1), "expr": m.group(0).strip(),
         "object": m.group(1), "attr": m.group(2)}
        for m in GETATTR_PRIVATE_RE.finditer(text)
    ]

    setattr_calls = []
    for m in re.finditer(r"setattr\(\s*([\w.]+)\s*,\s*[\"']([\w]+)[\"']", text):
        setattr_calls.append(
            {"line": text[: m.start()].count("\n") + 1, "expr": m.group(0).strip()}
        )

    return {
        "file": rel,
        "imports": imports,
        "getattr_private": getattr_private,
        "setattr_calls": setattr_calls,
    }


def main() -> None:
    out_path = ROOT / "docs/architecture/nanobot-inventory.json"
    args = sys.argv[1:]
    if "--out" in args:
        out_path = ROOT / args[args.index("--out") + 1]

    try:
        version = subprocess.run(
            [sys.executable, "-c", "import nanobot; print(nanobot.__version__)"],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip() or "0.3.0 (pinned)"
    except Exception:
        version = "0.3.0 (pinned)"

    files = [scan_file(p) for p in iter_py_files()]
    all_imports = [imp for f in files for imp in f["imports"]]
    counts: dict[str, int] = {}
    for imp in all_imports:
        counts[imp["classification"]] = counts.get(imp["classification"], 0) + 1

    inventory = {
        "_meta": {
            "description": "Инвентаризация зависимостей workspaces_nanobot от nanobot-ai",
            "nanobot_version_pinned": "0.3.0",
            "nanobot_version_installed": version,
            "scan_date": str(date.today()),
            "scanner": "tools/scan_nanobot_inventory.py",
            "legend": {
                "GREEN": "public/stable extension point",
                "YELLOW": "public API, но tightly coupled или внутренний класс CLI",
                "ORANGE": "internal implementation (нестабильный контракт)",
                "RED": "private API / monkey patch / private state",
            },
        },
        "summary": {
            "files_scanned": len(files),
            "total_imports": len(all_imports),
            "by_classification": counts,
            "getattr_private_total": sum(len(f["getattr_private"]) for f in files),
            "setattr_total": sum(len(f["setattr_calls"]) for f in files),
        },
        "files": files,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"OK: {out_path}")
    print(json.dumps(inventory["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
