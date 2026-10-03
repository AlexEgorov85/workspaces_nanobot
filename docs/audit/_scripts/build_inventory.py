#!/usr/bin/env python
"""Build a static inventory of the product code for a full audit.

Produces, under docs/audit/_data/:
  inventory.json  - full symbol tree (classes, methods, functions) with LOC,
                    signatures, docstrings, and reference counters
  orphans.md      - product modules that nothing appears to import
  dead_symbols.md - top-level symbols with zero references anywhere
  duplicates.md   - identical / near-identical function bodies
  test_gaps.md    - product modules with no test reference
  _briefs/<name>.md - per-subsystem work briefs for the deep-audit agents

Scope: PRODUCT code only (lib, workspace/{tools,utils,hooks,skills scripts},
tools, benchmarks, root entry points). Tests are parsed for reference counting
but never audited as subjects.

Usage:  python docs/audit/_scripts/build_inventory.py
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import pathlib
import re
from collections import defaultdict

ROOT = pathlib.Path(__file__).resolve().parents[3]
OUT = ROOT / "docs" / "audit" / "_data"
BRIEFS = OUT / "_briefs"

SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "site-packages", "dist", "build",
    ".benchmarks", "data_store", "logs", "sessions", "history", "media",
    ".opencode", ".cline", "webui", "cli-apps", "profiles", "prompts",
    "cron", "memory", "docs", "openspec", ".github",
    # Копия форка соседнего воркера: её файлы — копии тех же модулей, и без
    # исключения они засчитывались бы как «импортёры», удваивая счётчики
    # ссылок и пряча настоящие (0 ссылок читается как 2).
    ".worktrees",
}

# --- product scope ---------------------------------------------------------
PRODUCT_PREFIXES = (
    "lib/", "tools/", "benchmarks/", "workspace/tools/", "workspace/utils/",
    "workspace/hooks/", "workspace/skills/",
)
PRODUCT_FILES = {
    "config.py", "gateway.py", "cli_agent.py", "streamlit_app.py",
    "project.json", "config.json",
}

# Subsystem batching for the deep-audit agents.
BRIEF_GROUPS: list[tuple[str, list[str]]] = [
    ("01-core-entrypoints", ["lib/core/", "config.py", "gateway.py",
                             "cli_agent.py", "streamlit_app.py"]),
    ("02-services-cache", ["lib/services/cache_provider.py",
                           "lib/services/cache_provider_impl.py",
                           "lib/services/duckdb_cache_store.py",
                           "lib/services/cache_load_service.py",
                           "lib/services/table_registry.py",
                           "lib/services/vector_index_service.py",
                           "lib/services/preload_service.py",
                           "lib/core/infra_registration.py",
                           "lib/core/skill_registration.py",
                           "lib/core/skill_config.py"]),
    ("03-services-runtime", ["lib/services/runtime_patcher.py",
                             "lib/services/runtime_inventory.py",
                             "lib/services/runtime_health.py",
                             "lib/services/runtime_events_subscriber.py",
                             "lib/services/compaction_event_subscriber.py",
                             "lib/services/project_tool_loader.py",
                             "lib/services/consolidator_locale.py",
                             "lib/services/schema_validation.py",
                             "lib/services/context_compaction.py",
                             "lib/services/channel_factory.py"]),
    ("04-services-data", ["lib/services/db_logging_service.py",
                          "lib/services/db_logging_bus.py",
                          "lib/services/llm_usage_store_factory.py",
                          "lib/services/llm_observer.py",
                          "lib/services/llm_client.py",
                          "lib/services/llm_config.py",
                          "lib/services/session_cold_sync_service.py",
                          "lib/services/session_storage.py",
                           "lib/services/subprocess_manager.py",
                           "lib/services/config_service.py",
                           "lib/services/transcription_service.py",
                           "lib/services/text_splitter.py",
                           "lib/services/duckdb_query.py"]),
    ("05-hooks-cli-lifecycle", ["lib/hooks/", "lib/cli/", "lib/lifecycle/",
                                "lib/session/", "lib/events/"]),
    ("06-channels", ["lib/channels/"]),
    ("07-utils", ["lib/utils/"]),
    ("08-workspace-plugins", ["workspace/tools/", "workspace/hooks/",
                              "workspace/utils/"]),
    ("09-skill-audit-analyzer", ["workspace/skills/audit_analyzer/scripts/"]),
    ("10-skill-legal-a", ["workspace/skills/legal_summarizer/scripts/application/",
                          "workspace/skills/legal_summarizer/scripts/cache/",
                          "workspace/skills/legal_summarizer/scripts/chunking/",
                          "workspace/skills/legal_summarizer/scripts/execution/"]),
    ("11-skill-legal-document", ["workspace/skills/legal_summarizer/scripts/document/"]),
    ("12-skill-legal-rest", ["workspace/skills/legal_summarizer/scripts/retrieval/",
                             "workspace/skills/legal_summarizer/scripts/llm/",
                             "workspace/skills/legal_summarizer/scripts/output/",
                             "workspace/skills/legal_summarizer/scripts/planning/",
                             "workspace/skills/legal_summarizer/scripts/cli.py",
                             "workspace/skills/legal_summarizer/scripts/cli_query.py",
                             "workspace/skills/legal_summarizer/scripts/__init__.py"]),
    ("13-tools-cli", ["tools/"]),
    ("14-benchmarks", ["benchmarks/"]),
]


def iter_py_files() -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            if f.endswith(".py"):
                out.append(pathlib.Path(dirpath) / f)
    return sorted(out)


def rel(p: pathlib.Path) -> str:
    return p.relative_to(ROOT).as_posix()


def is_test(r: str) -> bool:
    return (r.startswith("tests/")
            or "/tests/" in r
            or r.split("/")[-1].startswith("test_")
            or r.startswith("test_"))


def is_product(r: str) -> bool:
    """Product code = audited subjects. Test files are reference evidence only."""
    if is_test(r):
        return False
    return r.startswith(PRODUCT_PREFIXES) or r in PRODUCT_FILES


def mod_name(r: str) -> str:
    """repo path -> importable dotted module path (best effort)."""
    s = r[:-3] if r.endswith(".py") else r
    parts = s.split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1]
    return ".".join(parts)


def first_line(doc: str | None, limit: int = 220) -> str:
    if not doc:
        return ""
    line = " ".join(doc.strip().split())
    return line[:limit]


def sig_of(node: ast.AST) -> str:
    try:
        args = ast.unparse(node.args)  # type: ignore[attr-defined]
    except Exception:
        args = "..."
    ret = ""
    if getattr(node, "returns", None) is not None:
        try:
            ret = " -> " + ast.unparse(node.returns)  # type: ignore[attr-defined]
        except Exception:
            ret = ""
    return f"({args}){ret}"


def decorators_of(node: ast.AST) -> list[str]:
    out = []
    for d in getattr(node, "decorator_list", []) or []:
        try:
            out.append(ast.unparse(d))  # type: ignore[attr-defined]
        except Exception:
            pass
    return out


def complexity(node: ast.AST) -> int:
    """Rough branch count - proxy for how much logic a body carries."""
    n = 1
    for child in ast.walk(node):
        if isinstance(child, (ast.If, ast.For, ast.While, ast.ExceptHandler,
                              ast.With, ast.Assert, ast.IfExp, ast.comprehension)):
            n += 1
        elif isinstance(child, ast.BoolOp):
            n += max(0, len(child.values) - 1)
        elif isinstance(child, ast.Call):
            fn = child.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None) or ""
            if name in ("any", "all", "sum", "max", "min", "get", "join"):
                n += 1
    return n


def body_hash(node: ast.AST) -> str:
    """Hash of a function body with names/strings normalised - finds clones."""
    norm: list[str] = []

    def walk(n: ast.AST) -> None:
        if isinstance(n, ast.Name):
            norm.append("N")
        elif isinstance(n, ast.Constant):
            norm.append(repr(n.value)[:20] if isinstance(n.value, (int, float, bool)) else "S")
        elif isinstance(n, ast.arg):
            norm.append("A")
        elif isinstance(n, ast.Attribute):
            norm.append("A")
        elif isinstance(n, ast.Call):
            nm = getattr(n.func, "id", None) or getattr(n.func, "attr", "") or "c"
            norm.append(f"call:{nm}")
        else:
            norm.append(type(n).__name__)
        for c in ast.iter_child_nodes(n):
            walk(c)

    body = getattr(node, "body", None)
    if body is None:
        return ""
    for stmt in body:
        walk(stmt)
    return hashlib.sha1("|".join(norm).encode()).hexdigest()[:16]


class FileInfo:
    __slots__ = ("path", "rel", "module", "product", "test", "loc",
                 "code_loc", "doc", "imports", "classes", "functions",
                 "imported_names", "attr_uses", "name_uses", "strings",
                 "parse_error")

    def __init__(self, path: pathlib.Path):
        self.path = path
        self.rel = rel(path)
        self.module = mod_name(self.rel)
        self.product = is_product(self.rel)
        self.test = is_test(self.rel)
        self.loc = 0
        self.code_loc = 0
        self.doc = ""
        self.imports: list[dict] = []
        self.classes: list[dict] = []
        self.functions: list[dict] = []
        self.imported_names: list[str] = []   # bare identifiers imported
        self.attr_uses: dict[str, list[str]] = defaultdict(list)
        self.name_uses: dict[str, list[str]] = defaultdict(list)
        self.strings: set[str] = set()
        self.parse_error: str | None = None


def collect_fn(node: ast.AST, owner: str, parent: str | None) -> dict:
    name = node.name  # type: ignore[attr-defined]
    qual = f"{owner}.{name}" if owner else (f"{parent}.{name}" if parent else name)
    return {
        "name": name,
        "qualname": qual,
        "lineno": node.lineno,  # type: ignore[attr-defined]
        "endlineno": getattr(node, "end_lineno", node.lineno),  # type: ignore[attr-defined]
        "loc": (getattr(node, "end_lineno", node.lineno) or node.lineno) - node.lineno + 1,  # type: ignore[attr-defined]
        "sig": sig_of(node),
        "is_async": isinstance(node, ast.AsyncFunctionDef),
        "decorators": decorators_of(node),
        "doc": first_line(ast.get_docstring(node)),
        "nested": [collect_fn(n, "", qual) for n in node.body  # type: ignore[attr-defined]
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))],
        "branch": complexity(node),
        "hash": body_hash(node),
        "owner": owner,
    }


def collect_cls(node: ast.AST) -> dict:
    bases = []
    for b in node.bases:  # type: ignore[attr-defined]
        try:
            bases.append(ast.unparse(b))  # type: ignore[attr-defined]
        except Exception:
            pass
    methods, attrs = [], []
    for st in node.body:  # type: ignore[attr-defined]
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
            methods.append(collect_fn(st, node.name, None))  # type: ignore[attr-defined]
        elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
            attrs.append({"name": st.target.id, "lineno": st.lineno})
        elif isinstance(st, ast.Assign):
            for t in st.targets:
                if isinstance(t, ast.Name):
                    attrs.append({"name": t.id, "lineno": st.lineno})
    return {
        "name": node.name,  # type: ignore[attr-defined]
        "lineno": node.lineno,  # type: ignore[attr-defined]
        "endlineno": getattr(node, "end_lineno", node.lineno),  # type: ignore[attr-defined]
        "loc": (getattr(node, "end_lineno", node.lineno) or node.lineno) - node.lineno + 1,  # type: ignore[attr-defined]
        "bases": bases,
        "decorators": decorators_of(node),
        "doc": first_line(ast.get_docstring(node)),
        "methods": methods,
        "attrs": attrs,
    }


def parse(fi: FileInfo) -> None:
    try:
        src = fi.path.read_text(encoding="utf-8-sig", errors="ignore")
        tree = ast.parse(src, filename=fi.rel)
    except (SyntaxError, ValueError) as e:
        fi.parse_error = str(e)
        fi.loc = len(fi.path.read_text(encoding="utf-8-sig", errors="ignore").splitlines())
        return
    lines = src.splitlines()
    fi.loc = len(lines)
    fi.code_loc = sum(1 for ln in lines if ln.strip() and not ln.strip().startswith("#"))
    fi.doc = first_line(ast.get_docstring(tree))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                fi.imports.append({"module": a.name, "names": [a.asname or a.name],
                                   "lineno": node.lineno, "kind": "import"})
                fi.imported_names.append(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            mod = ("." * (node.level or 0)) + (node.module or "")
            for a in node.names:
                fi.imports.append({"module": mod, "names": [a.asname or a.name],
                                   "lineno": node.lineno, "kind": "from"})
                fi.imported_names.append(a.asname or a.name)
        elif isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name):
                fi.attr_uses[node.attr].append(f"{fi.rel}:{node.lineno}")
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            fi.name_uses[node.id].append(f"{fi.rel}:{node.lineno}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            for m in re.findall(r"[A-Za-z_][\w.]*(?:\.[\w]+)+", node.value):
                fi.strings.add(m)

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            fi.classes.append(collect_cls(node))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fi.functions.append(collect_fn(node, "", None))


SKILL_RE = re.compile(r"^workspace/skills/([^/]+)/scripts/(.+?)(?:/__init__)?\.py$")


def skill_root_of(r: str) -> str:
    """Skill whose scripts/ dir is the sys.path root for this file ('' if none)."""
    m = SKILL_RE.match(r)
    return m.group(1) if m else ""


def module_aliases(r: str) -> list[str]:
    """Every importable spelling of this module.

    Skill code is executed with the skill's ``scripts/`` dir as a sys.path root,
    so its modules are imported as ``application.service`` / ``output`` /
    ``cli``, while the same files are ALSO reachable from the repo root as
    ``workspace.skills.<skill>.scripts.application.service``. Counting only one
    spelling produces ~100 phantom "orphan" modules.
    """
    out = {mod_name(r)}
    m = SKILL_RE.match(r)
    if m:
        skill, rest = m.group(1), m.group(2).replace("/", ".")
        out.add(rest)
        out.add("scripts." + rest)
    return sorted(out)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    BRIEFS.mkdir(parents=True, exist_ok=True)

    files: dict[str, FileInfo] = {}
    for p in iter_py_files():
        fi = FileInfo(p)
        parse(fi)
        files[fi.rel] = fi

    product = {r: f for r, f in files.items() if f.product}
    tests = {r: f for r, f in files.items() if f.test}
    others = {r: f for r, f in files.items() if not f.product and not f.test}
    print(f"parsed: {len(files)} files | product={len(product)} test={len(tests)} other={len(others)}")
    for r, f in files.items():
        if f.parse_error:
            print(f"  PARSE ERROR {r}: {f.parse_error}")

    # ---- module reference graph -------------------------------------------
    # Keyed by (skill_root, spelling) so two skills' `cli` modules never match.
    by_alias: dict[tuple[str, str], FileInfo] = {}
    for r, f in files.items():
        sk = skill_root_of(r)
        for a in module_aliases(r):
            by_alias.setdefault((sk, a), f)

    importers: dict[str, set[str]] = defaultdict(set)
    dynamic: dict[str, set[str]] = defaultdict(set)
    for r, f in files.items():
        sk = skill_root_of(r)
        for imp in f.imports:
            mod = imp["module"]
            base = mod.lstrip(".")
            cands = {mod, base, base.rstrip("."), mod.lstrip(".").rstrip(".__init__")}
            for cand in cands:
                tgt = by_alias.get((sk, cand))
                if tgt is None and sk:
                    tgt = by_alias.get(("", cand))
                if tgt is not None and tgt.rel != r:
                    importers[tgt.rel].add(r)
        for s in f.strings:
            tgt = by_alias.get((sk, s)) or by_alias.get(("", s))
            if tgt is not None and tgt.rel != r:
                dynamic[tgt.rel].add(r)

    # ---- name reference index (all files) ---------------------------------
    name_index: dict[str, set[str]] = defaultdict(set)     # identifier -> files
    attr_index: dict[str, set[str]] = defaultdict(set)      # method name -> files
    str_index: dict[str, set[str]] = defaultdict(set)      # symbol string -> files
    for r, f in files.items():
        for n, sites in f.name_uses.items():
            name_index[n].add(r)
            str_index[n].add(r)
        for n, sites in f.attr_uses.items():
            attr_index[n].add(r)
            str_index[n].add(r)
        for n in f.imported_names:
            name_index[n].add(r)
            str_index[n].add(r)

    # ---- annotate product symbols ----------------------------------------
    def refs_for(kind: str, symname: str, owner_cls: str | None) -> dict:
        if kind == "method" and owner_cls:
            ext = {s.split(":")[0] for s in attr_index.get(symname, set())}
        else:
            ext = (name_index.get(symname, set()) | str_index.get(symname, set()))
        return {
            "ref_files": sorted(ext),
            "ref_count": len(ext),
            "in_tests": sorted({x for x in ext if files[x].test}),
        }

    report_files: dict[str, dict] = {}
    for r, f in product.items():
        entry = {
            "rel": r, "module": f.module, "loc": f.loc, "code_loc": f.code_loc,
            "doc": f.doc, "parse_error": f.parse_error,
            "imported_by": sorted(importers.get(r, set())),
            "imported_by_count": len(importers.get(r, set())),
            "dynamic_import": sorted(dynamic.get(r, set())),
            "test_refs": sorted({x for a in module_aliases(r)
                                 for x in str_index.get(a, set())
                                 if files[x].test}),
            "classes": [], "functions": [],
        }
        for c in f.classes:
            cdir = refs_for("class", c["name"], None)
            c = dict(c)
            c["ref_count"] = cdir["ref_count"]
            c["ref_files"] = cdir["ref_files"][:25]
            c["in_tests"] = cdir["in_tests"][:25]
            c["instantiated_in"] = sorted({
                s.split(":")[0] for s in f.name_uses.get(c["name"], [])
            } | {x for x in cdir["ref_files"] if x in f.name_uses.get(c["name"], [])})
            methods = []
            for m in c["methods"]:
                # internal self.<m>( ) callers inside the same file
                internal = len(re.findall(
                    r"(?:self|cls)\s*\.\s*" + re.escape(m["name"]) + r"\s*\(",
                    f.path.read_text(encoding="utf-8-sig", errors="ignore")))
                mdir = refs_for("method", m["name"], c["name"])
                m = dict(m)
                m["internal_call_sites"] = internal
                m["ref_count"] = mdir["ref_count"]
                m["ref_files"] = mdir["ref_files"][:25]
                m["in_tests"] = mdir["in_tests"][:25]
                methods.append(m)
            c["methods"] = methods
            entry["classes"].append(c)
        for fn in f.functions:
            fdir = refs_for("function", fn["name"], None)
            fn = dict(fn)
            fn["ref_count"] = fdir["ref_count"]
            fn["ref_files"] = fdir["ref_files"][:25]
            fn["in_tests"] = fdir["in_tests"][:25]
            entry["functions"].append(fn)
        report_files[r] = entry

    totals = {
        "product_files": len(product),
        "product_loc": sum(f.loc for f in product.values()),
        "test_files": len(tests),
        "product_classes": sum(len(e["classes"]) for e in report_files.values()),
        "product_methods": sum(len(c["methods"]) for e in report_files.values()
                               for c in e["classes"]),
        "product_functions": sum(len(e["functions"]) for e in report_files.values()),
    }
    print("TOTALS:", json.dumps(totals))

    (OUT / "inventory.json").write_text(
        json.dumps({"totals": totals, "files": report_files}, ensure_ascii=False, indent=1),
        encoding="utf-8")

    # ---- orphan modules ---------------------------------------------------
    lines = ["# Product modules with no static importer", "",
             "`dynamic` = referenced only as a string (importlib / plugin scanning).",
             "A module can legitimately have zero importers (entry points, test-profile",
             "fixtures, module registries, stdlib-style plugin roots). Judge before deleting.", ""]
    orph = []
    for r, e in sorted(report_files.items()):
        if e["imported_by_count"] == 0:
            orph.append((r, e["loc"], e["dynamic_import"], e["doc"]))
    lines.append(f"**{len(orph)} of {len(product)} product modules have no static importer.**")
    lines += ["", "| Module | LOC | Dynamic-only refs | Docstring |", "|---|---:|---|---|"]
    for r, loc, dyn, doc in sorted(orph, key=lambda x: -x[1]):
        lines.append(f"| `{r}` | {loc} | {len(dyn)} | {doc[:90]} |")
    (OUT / "orphans.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ---- dead top-level symbols ------------------------------------------
    lines = ["# Top-level symbols with zero references", "",
             "Zero references means: no import of the name, no attribute use, no string",
             "mention, in ANY file including tests. Strong dead-code candidates.", ""]
    dead = []
    for r, e in report_files.items():
        for c in e["classes"]:
            if c["ref_count"] == 0:
                dead.append((f"{r}::{c['name']}", "class", c["loc"], c["doc"]))
        for fn in e["functions"]:
            if fn["ref_count"] == 0 and not fn["name"].startswith("_"):
                dead.append((f"{r}::{fn['name']}", "function", fn["loc"], fn["doc"]))
    lines.append(f"**{len(dead)} symbols.**")
    lines += ["", "| Symbol | Kind | LOC | Docstring |", "|---|---|---:|---|"]
    for s, k, loc, doc in sorted(dead, key=lambda x: -x[2]):
        lines.append(f"| `{s}` | {k} | {loc} | {doc[:90]} |")
    (OUT / "dead_symbols.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ---- duplicate bodies -------------------------------------------------
    buckets: dict[str, list[str]] = defaultdict(list)
    for r, e in report_files.items():
        for c in e["classes"]:
            for m in c["methods"]:
                if m["hash"] and m["loc"] >= 4:
                    buckets[m["hash"]].append(f"{r}:{m['qualname']} ({m['loc']}L)")
        for fn in e["functions"]:
            if fn["hash"] and fn["loc"] >= 4:
                buckets[fn["hash"]].append(f"{r}::{fn['qualname']} ({fn['loc']}L)")
    lines = ["# Structurally identical function bodies", "",
             "Bodies normalised (names/strings replaced) then hashed. Strong signal of",
             "copy-paste, generated scaffolding, or intentional parallel implementations.", ""]
    dupn = 0
    for h, members in sorted(buckets.items()):
        if len(members) > 1:
            dupn += len(members)
            lines += [f"### `{h}` — {len(members)} copies", ""]
            lines += [f"- {m}" for m in members]
            lines.append("")
    lines.insert(4, f"**{dupn} function bodies form clone groups.**\n")
    (OUT / "duplicates.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ---- test gaps --------------------------------------------------------
    lines = ["# Product modules with no trace in any test file", ""]
    gaps = [(r, e) for r, e in report_files.items()
            if not any(files[t].test for t in e["imported_by"] + e["test_refs"])]
    lines.append(f"**{len(gaps)} of {len(product)} product modules.**")
    lines += ["", "| Module | LOC | Dynamically loaded | Docstring |", "|---|---:|---|---|"]
    for r, e in sorted(gaps, key=lambda x: -x[1]["loc"]):
        dyn = "yes" if e["dynamic_import"] else ""
        lines.append(f"| `{r}` | {e['loc']} | {dyn} | {e['doc'][:90]} |")
    (OUT / "test_gaps.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ---- per-group briefs -------------------------------------------------
    # Wipe stale briefs first: group names change between runs, and a leftover
    # brief from a previous split would silently audit the wrong file set.
    if BRIEFS.exists():
        for stale in BRIEFS.glob("*.md"):
            stale.unlink()
    assigned: set[str] = set()
    for name, prefixes in BRIEF_GROUPS:
        sel = [r for r in product
               if any(r == p.rstrip("/") or r.startswith(p) for p in prefixes)]
        assigned |= set(sel)
        write_brief(name, sel, product, report_files, files)

    rest = sorted(set(product) - assigned)
    if rest:
        write_brief("99-unassigned", rest, product, report_files, files)

    print(f"briefs written: {len(list(BRIEFS.glob('*.md')))}")
    print("unassigned product files:", len(rest))
    for r in rest:
        print("   ", r)


def write_brief(name: str, sel: list[str], product: dict, rep: dict,
                files: dict) -> None:
    if not sel:
        return
    out = [f"# Work brief `{name}`", "",
           f"Product files: **{len(sel)}**, LOC: **{sum(product[r].loc for r in sel)}**", ""]
    for r in sorted(sel, key=lambda x: -product[x].loc):
        e = rep[r]
        out += [
            f"## `{r}` — {e['loc']} LOC (code {e['code_loc']})",
            f"- module: `{e['module']}`",
            f"- docstring: {e['doc'] or '— NONE —'}",
            f"- static importers ({e['imported_by_count']}): "
            + (", ".join(f"`{x}`" for x in e["imported_by"][:14]) or "— NONE —"),
            f"- string/dynamic refs: {len(e['dynamic_import'])}",
            f"- test files touching it: "
            + (", ".join(f"`{x}`" for x in e["test_refs"][:10]) or "— none —"),
            f"- classes: {len(e['classes'])}, module functions: {len(e['functions'])}",
            "",
        ]
        for c in e["classes"]:
            out.append(f"### class `{c['name']}` — lines {c['lineno']}-{c['endlineno']} "
                       f"({c['loc']} LOC), {len(c['methods'])} methods")
            out.append(f"- bases: {', '.join(c['bases']) or 'object'}")
            out.append(f"- decorators: {', '.join(c['decorators']) or '—'}")
            out.append(f"- docstring: {c['doc'] or '— NONE —'}")
            out.append(f"- name referenced in {c['ref_count']} file(s); "
                       f"tests: {len(c['in_tests'])}")
            out.append("")
            out.append("| Method | Lines | Sig | Br | self-call sites | Ref files | Doc |")
            out.append("|---|---|---|---:|---:|---:|---|")
            for m in c["methods"]:
                doc = m["doc"][:110].replace("|", "\\|") or "—"
                out.append(
                    f"| `{m['name']}` | {m['lineno']}-{m['endlineno']} | "
                    f"`{m['sig'][:90]}` | {m['branch']} | {m['internal_call_sites']} | "
                    f"{m['ref_count']} | {doc} |")
            out.append("")
        if e["functions"]:
            out.append("### module-level functions")
            out.append("")
            out.append("| Function | Lines | Sig | Br | Ref files | Doc |")
            out.append("|---|---|---|---:|---:|---|")
            for m in e["functions"]:
                doc = m["doc"][:110].replace("|", "\\|") or "—"
                out.append(
                    f"| `{m['name']}` | {m['lineno']}-{m['endlineno']} | "
                    f"`{m['sig'][:90]}` | {m['branch']} | {m['ref_count']} | {doc} |")
            out.append("")
    (BRIEFS / f"{name}.md").write_text("\n".join(out) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
