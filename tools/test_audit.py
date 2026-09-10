"""Phase 1+2 deterministic AST audit of the test suite.

Produces test-audit.json (machine-readable) and prints a one-line
summary per file to stdout. Designed to be re-run.

Categories tracked:
- test_id, file, class, line, name, async, markers, decorators
- fixtures used, production imports, mock targets, patched symbols
- assertion count + assertion smells
- skip / xfail / silent except
- production target heuristic
- initial KEEP/STRENGTHEN/REWRITE/MERGE/MOVE/DELETE/XPASS/INVESTIGATE verdict
"""
from __future__ import annotations

import ast
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(r"C:\Users\Алексей\.nanobot")
OUT_DIR = REPO / "workspace" / "data_store" / "cache" / "sessions" / "test-suite-audit-2026-09-10" / "audit"
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_JSON = OUT_DIR / "test-audit.json"
OUT_SUMMARY = OUT_DIR / "test-audit-summary.txt"

TEST_ROOTS = [
    REPO / "tests",
    REPO / "workspace" / "skills" / "legal_summarizer" / "tests",
]
SKIP_FILES = {"__init__.py", "conftest.py", "helpers.py", "cyrillic_literals.py"}

BUILTIN_TEST_DIRS = {"tests", "test", "integration", "contract", "benchmark", "benchmarks", "architecture", "unit", "smoke"}

# ---------------------------------------------------------------------------
# helpers


def _is_test_call(node: ast.Call, names: tuple[str, ...]) -> bool:
    f = node.func
    if isinstance(f, ast.Name) and f.id in names:
        return True
    if isinstance(f, ast.Attribute) and f.attr in names:
        return True
    return False


def _short(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return "<unparse>"


def _collect_names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


# ---------------------------------------------------------------------------
# per-test extraction


def _function_body_range(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[int, int]:
    return fn.lineno, (fn.end_lineno or fn.lineno)


def _walk_collect_calls(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.Call]:
    return [n for n in ast.walk(fn) if isinstance(n, ast.Call)]


def _walk_collect_asserts(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.Assert]:
    return [n for n in ast.walk(fn) if isinstance(n, ast.Assert)]


def _is_pytest_skip(call: ast.Call) -> bool:
    if _is_test_call(call, ("skip", "skipif", "xfail")) and isinstance(call.func, ast.Attribute):
        owner = call.func.value
        if isinstance(owner, ast.Name) and owner.id == "pytest":
            return True
    return False


def _has_pytest_marker(decorators: list[ast.expr]) -> set[str]:
    found: set[str] = set()
    for d in decorators:
        if isinstance(d, ast.Attribute) and d.attr in {"skip", "skipif", "xfail", "parametrize", "asyncio"}:
            found.add(d.attr)
        if isinstance(d, ast.Call):
            f = d.func
            if isinstance(f, ast.Attribute) and f.attr in {"skip", "skipif", "xfail", "parametrize", "asyncio"}:
                owner = f.value
                if isinstance(owner, ast.Name) and owner.id == "pytest":
                    found.add(f.attr)
            if isinstance(f, ast.Name) and f.id == "pytest":
                if d.args and isinstance(d.args[0], ast.Constant):
                    found.add(f"mark:{d.args[0].value}")
    return found


def _collect_assertion_smells(asserts: list[ast.Assert], all_calls: list[ast.Call]) -> list[dict]:
    smells: list[dict] = []
    for a in asserts:
        t = a.test
        line = a.lineno
        # assert True / assert False / bare bool constant
        if isinstance(t, ast.Constant) and isinstance(t.value, bool):
            smells.append({"line": line, "type": "assert_bool_literal", "snippet": _short(t)[:120]})
            continue
        if isinstance(t, ast.Compare):
            left_expr = _compare_lefts(t)
            for left, op, right in zip(left_expr, t.ops, t.comparators):
                smells.extend(_check_compare_smell(left, op, right, line))
        if isinstance(t, ast.Call):
            smells.extend(_check_call_smell(t, line))
        if isinstance(t, ast.Name) and t.id in {"True", "False"}:
            smells.append({"line": line, "type": "assert_name_bool", "snippet": t.id})
        # Tautology: foo(x) == foo(x) — same call duplicated
        if isinstance(t, ast.Compare):
            left_expr = _compare_lefts(t)
            for left, op, right in zip(left_expr, t.ops, t.comparators):
                if isinstance(op, (ast.Eq, ast.Is)):
                    if _short(left) == _short(right):
                        smells.append({"line": line, "type": "tautology_identical_operands", "snippet": _short(t)[:160]})
                # assert x >= 0 / assert x <= MAX  (pure bound when x is always non-negative by type)
                if isinstance(op, (ast.GtE, ast.LtE, ast.Gt, ast.Lt)) and isinstance(right, ast.Constant) and isinstance(right.value, (int, float)):
                    if right.value in (0, 0.0) and isinstance(op, ast.GtE):
                        smells.append({"line": line, "type": "weak_lower_bound_zero", "snippet": _short(t)[:160]})
        if isinstance(t, ast.Compare):
            left_expr = _compare_lefts(t)
            for left, op, right in zip(left_expr, t.ops, t.comparators):
                smells.extend(_check_compare_smell(left, op, right, line))
        if isinstance(t, ast.Call):
            smells.extend(_check_call_smell(t, line))
        # self-fake-verification pattern: result == fake_expected, where fake_expected built in test
        # — heuristic: literal RHS that is a Name defined in same function (gathered later).
    # bare `pass` or only skip — counted separately
    return smells


def _check_compare_smell(left, op, right, line: int) -> list[dict]:
    smells: list[dict] = []
    if isinstance(op, ast.Is):
        if isinstance(right, ast.Constant) and right.value is None:
            smells.append({"line": line, "type": "assert_is_not_none", "snippet": f"{_short(left)} is None / is not None"})
        if isinstance(left, ast.Constant) and left.value is None and isinstance(op, ast.IsNot):
            smells.append({"line": line, "type": "assert_x_is_not_none_inverse", "snippet": f"{_short(left)} is not None"})
    return smells


def _check_call_smell(call: ast.Call, line: int) -> list[dict]:
    smells: list[dict] = []
    name = None
    if isinstance(call.func, ast.Name):
        name = call.func.id
    elif isinstance(call.func, ast.Attribute):
        name = call.func.attr
    if name == "callable":
        smells.append({"line": line, "type": "callable_only", "snippet": _short(call)[:120]})
    if name == "hasattr":
        smells.append({"line": line, "type": "hasattr_only", "snippet": _short(call)[:120]})
    if name == "endswith":
        smells.append({"line": line, "type": "endswith_only", "snippet": _short(call)[:120]})
    return smells


def _compare_lefts(node: ast.Compare) -> list[ast.expr]:
    """Normalise Compare.left to a list across Python versions.

    Python 3.12+ changed Compare.left to a single expr instead of a list[expr].
    This helper hides the difference: the resulting list has len == len(node.ops),
    and the i-th item is what was compared against node.comparators[i].
    """
    left = getattr(node, "left", None)
    if isinstance(left, list):
        return left
    if left is None:
        return []
    # single expr + list of comparators: walk forward
    return [left, *node.comparators[:-1]]


def _collect_skips(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[dict]:
    skips = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and _is_pytest_skip(node):
            skips.append({"line": node.lineno, "type": "pytest_call", "snippet": _short(node)[:120]})
    return skips


def _collect_silent_excepts(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[dict]:
    out = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Try):
            for handler in node.handlers:
                body = handler.body
                # swallow if body is `pass` or no assert / no raise / no logging
                is_pass_only = len(body) == 1 and isinstance(body[0], ast.Pass)
                if is_pass_only:
                    out.append({"line": handler.lineno, "type": "silent_pass", "snippet": "except ...: pass"})
                    continue
                # broad Exception with no re-raise and no assertion
                if isinstance(handler.type, ast.Name) and handler.type.id == "Exception":
                    has_assert = any(isinstance(b, ast.Assert) for b in ast.walk(handler))
                    has_raise = any(isinstance(b, ast.Raise) for b in ast.walk(handler))
                    if not has_assert and not has_raise:
                        out.append({"line": handler.lineno, "type": "broad_silent_except", "snippet": "except Exception: (no assert, no raise)"})
    return out


def _collect_import_fallbacks(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[dict]:
    """Flag `try: import X except ImportError: ... pass|continue|...` style code
    used as soft import guards. Often turns architectural tests into NOPs.

    Also flags `try: importlib.import_module(...) except ImportError: ...` patterns
    where the except handler does not assert/raise — these silently mask forbidden
    imports in architectural guard tests.
    """
    out = []
    # Walk the whole function looking for any Assert/Raise — used to verify that
    # the success path of the try-block (which sits outside the Try node body
    # when written as `try: import; except ImportError: return; raise AssertionError(...)`)
    # actually contains an assertion.
    fn_asserts_raises = any(isinstance(b, (ast.Assert, ast.Raise)) for b in ast.walk(fn))
    for node in ast.walk(fn):
        if not isinstance(node, ast.Try):
            continue
        body_stmts = node.body
        if not body_stmts:
            continue
        first = body_stmts[0]
        is_static_import = isinstance(first, (ast.Import, ast.ImportFrom))
        is_dynamic_import = False
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Call):
            call = first.value
            f = call.func
            importlib_call = (isinstance(f, ast.Attribute) and f.attr in {"import_module"} and
                              isinstance(f.value, ast.Name) and f.value.id == "importlib")
            if importlib_call:
                is_dynamic_import = True
                imp_str = ast.unparse(call)[:120]
        if not (is_static_import or is_dynamic_import):
            continue
        label = ast.unparse(first)[:120] if is_static_import else imp_str
        for handler in node.handlers:
            catches_import_error = False
            if handler.type is None:
                catches_import_error = True
            elif isinstance(handler.type, ast.Name) and handler.type.id == "ImportError":
                catches_import_error = True
            elif isinstance(handler.type, ast.Tuple):
                for elt in handler.type.elts:
                    if isinstance(elt, ast.Name) and elt.id == "ImportError":
                        catches_import_error = True
            if not catches_import_error:
                continue
            has_assert = any(isinstance(b, ast.Assert) for b in ast.walk(handler))
            has_raise = any(isinstance(b, ast.Raise) for b in ast.walk(handler))
            if not has_assert and not has_raise:
                # Real guard requires an Assert/Raise either inside the handler
                # (success path stays here) OR somewhere in the function
                # (success path continues after the Try and asserts/raises).
                if not fn_asserts_raises:
                    out.append({
                        "line": handler.lineno,
                        "type": "soft_import_guard",
                        "snippet": f"try: {label}; except ImportError: (no assert/raise anywhere)",
                    })
    return out


def _detect_self_fake(fn: ast.FunctionDef | ast.AsyncFunctionDef, monkey_targets: list[str]) -> dict | None:
    """Detect self-fake-verification pattern:
    1) test creates a literal/dict object (e.g. expected = {...}, fake_manifest = {...})
    2) test calls monkeypatch.setattr(<prod_module>, "<func_under_test>", <stub>)
    3) test asserts that the (potentially stubbed) call returned the literal built in step 1

    Heuristics (any of):
    a) assert <call_to_monkeypatched_attr>(...) == <local_name_in_test>
    b) assert <local_suspicious_name>[...] or .attr is compared against anything
       where the local name was built in test AND a monkeypatch happened
    """
    if not monkey_targets:
        return None

    body_assign_names: dict[str, ast.AST] = {}
    for stmt in fn.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            body_assign_names[stmt.targets[0].id] = stmt.value

    suspicious = set()
    spy_names = {
        "call_count", "calls", "captured", "recorded", "seen", "trace",
        "spy_calls", "counter", "hits", "misses", "active", "attempts",
        "retries", "invocations", "request_log", "tried",
    }
    for name, value in body_assign_names.items():
        lname = name.lower()
        # Skip spy/counter names — they are legitimate verification of call counts.
        if any(s in lname for s in spy_names):
            continue
        if name.startswith(("fake_", "_fake", "expected_", "mock_")):
            suspicious.add(name)
            continue
        # dicts whose keys look like counter / flags are spies, not fixtures
        if isinstance(value, ast.Dict):
            keys = []
            for k in value.keys:
                if isinstance(k, ast.Constant):
                    keys.append(str(k.value))
                elif isinstance(k, ast.Name):
                    keys.append(k.id)
            if keys and any(k in {"n", "count", "ok", "err", "calls", "active", "fail"} for k in keys):
                continue
            # otherwise a non-trivial dict literal — likely an expected fixture
            if len(keys) >= 2:
                suspicious.add(name)
        elif isinstance(value, (ast.List, ast.Constant)):
            # Constants (None, 0, "") and empty literals are NOT suspicious
            if isinstance(value, ast.Constant) and value.value in (None, 0, 0.0, "", b"", False):
                continue
            suspicious.add(name)

    target_attrs = {mt.split(".")[-1] for mt in monkey_targets}

    # Pattern (a)
    for a in ast.walk(fn):
        if not isinstance(a, ast.Assert):
            continue
        t = a.test
        if not isinstance(t, ast.Compare):
            continue
        lefts = _compare_lefts(t)
        for left, right in zip(lefts, t.comparators):
            if isinstance(left, ast.Call) and isinstance(left.func, ast.Attribute):
                if left.func.attr in target_attrs and isinstance(right, ast.Name) and right.id in body_assign_names:
                    return {
                        "line": a.lineno,
                        "type": "self_fake_verification",
                        "snippet": f"assert {ast.unparse(left)} == {right.id} (where {right.id} is built in test, and {left.func.attr} was monkeypatched)",
                    }

    # Pattern (b): assert <fake_xxx>[...] compared to anything
    for a in ast.walk(fn):
        if not isinstance(a, ast.Assert):
            continue
        for n in ast.walk(a.test):
            base = None
            if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name):
                base = n.value.id
            elif isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name):
                base = n.value.id
            if base and base in suspicious:
                return {
                    "line": a.lineno,
                    "type": "self_fake_verification",
                    "snippet": f"assert references local '{base}' (built in test) AND monkeypatch.setattr applied",
                }

    return None


def _detect_mock_of_target(fn: ast.FunctionDef | ast.AsyncFunctionDef, target: dict) -> str | None:
    """If the test monkeypatches the production function it claims to test, flag."""
    if target.get("binding") != "BOUND":
        return None
    target_attr = target.get("method")
    if not target_attr or target_attr == "(module-level call)":
        return None
    for d in fn.decorator_list:
        d_str = ast.unparse(d)
        if target_attr in d_str and "patch" in d_str:
            return f"@patch decorator on production target: {d_str[:200]}"
    return None


def _collect_mocks_patches(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> dict:
    """Find:
    - monkeypatch.setattr / .setitem / .delattr targets
    - @patch / mock.patch decorators (resolved at function level)
    - Mock(...) / MagicMock(...) instantiations
    """
    mock_targets: list[str] = []
    mock_calls: list[str] = []
    monkey_targets: list[str] = []
    monkey_calls: list[str] = []
    patch_decorators: list[str] = []
    mock_specs: list[str] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else (
                node.func.attr if isinstance(node.func, ast.Attribute) else None
            )
            if name in {"Mock", "MagicMock", "AsyncMock", "PropertyMock"}:
                mock_calls.append(f"{name}@{node.lineno}")
            if isinstance(node.func, ast.Attribute) and node.func.attr in {"setattr", "setitem", "delattr"}:
                owner = node.func.value
                owner_name = _short(owner) if owner else ""
                if "monkey" in owner_name.lower():
                    monkey_calls.append(f"{node.func.attr}@{node.lineno}:{_short(node)[:120]}")
                    if node.args:
                        monkey_targets.append(_short(node.args[0]) + "." + (_short(node.args[1]) if len(node.args) > 1 else "?"))
    for d in fn.decorator_list:
        d_str = _short(d)
        if "patch" in d_str and ("mock" in d_str or "patch." in d_str or d_str.startswith("patch")):
            patch_decorators.append(d_str[:200])
    return {
        "mock_calls": mock_calls,
        "monkeypatch_targets": monkey_targets,
        "monkeypatch_calls": monkey_calls,
        "patch_decorators": patch_decorators,
    }


def _collect_fixtures_used(fn: ast.FunctionDef | ast.AsyncFunctionDef, class_node: ast.ClassDef | None) -> list[str]:
    used = set()
    for arg in fn.args.args + fn.args.kwonlyargs:
        used.add(arg.arg)
    if class_node is not None:
        # collect fixtures from class decorators or class-level fixture refs
        pass
    # also detect `request.getfixturevalue(...)` and `tmp_path`/`monkeypatch` references
    referenced_names = _collect_names(fn)
    return sorted(used)


def _collect_production_calls(fn: ast.FunctionDef | ast.AsyncFunctionDef, test_module: str, all_calls: list[ast.Call]) -> tuple[list[str], list[str]]:
    """Heuristic: production calls = calls into non-test, non-mock, non-pytest modules.

    We classify each call by whether the owner of the attribute / first import alias
    points to a name imported from a module that lives outside the test tree.
    """
    prod_calls: list[str] = []
    mock_like: list[str] = []
    bad_mock_targets: list[str] = []

    # collect imported aliases (name -> module)
    imported: dict[str, str] = {}
    # done at module level — caller passes via closure; we'll inline here per fn

    # Track potential mock targets: calls into `module.func` where the alias `module` is imported from production.
    # We do not resolve cross-module here; instead we flag attribute calls of the form `obj.method(...)`
    # where the snippet contains a clearly production-looking name.
    for c in all_calls:
        f = c.func
        snippet = _short(c).split("\n")[0][:160]
        if isinstance(f, ast.Attribute):
            if f.attr.startswith("assert") or f.attr in {"get", "set_default", "update"}:
                continue
            # Likely production: snake_case name, not _pytest, not Mock, not monkeypatch
            if f.attr in {"return_value", "side_effect", "assert_called"}:
                mock_like.append(snippet)
                continue
            prod_calls.append(snippet)
        elif isinstance(f, ast.Name):
            if f.id in {"pytest", "Mock", "MagicMock", "patch", "monkeypatch", "fail", "skip", "xfail"}:
                continue
            prod_calls.append(f.id + "()")

    return prod_calls, mock_like


def _resolve_imports(tree: ast.Module) -> dict[str, str]:
    table: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                table[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for alias in node.names:
                table[alias.asname or alias.name] = f"{mod}.{alias.name}" if mod else alias.name
    return table


def _production_target_heuristic(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    imports: dict[str, str],
    prod_calls: list[str],
) -> dict:
    """Best-effort production target binding.

    We pick the first call that:
      - comes from a name imported from a non-test module,
      - is not pytest / Mock / patch.
    If none: UNBOUND.
    """
    for c in ast.walk(fn):
        if not isinstance(c, ast.Call):
            continue
        f = c.func
        snippet = _short(c).split("\n")[0][:160]
        if isinstance(f, ast.Attribute):
            owner = f.value
            if isinstance(owner, ast.Name):
                mod = imports.get(owner.id)
                if mod and not mod.startswith("tests.") and not mod.startswith("_pytest") and not mod.startswith("unittest"):
                    return {
                        "owner_alias": owner.id,
                        "owner_module": mod,
                        "method": f.attr,
                        "snippet": snippet,
                        "binding": "BOUND",
                    }
            elif isinstance(owner, ast.Attribute):
                # chain like foo.bar.baz() — try to resolve outermost
                chain = []
                cur: ast.AST = owner
                while isinstance(cur, ast.Attribute):
                    chain.append(cur.attr)
                    cur = cur.value
                if isinstance(cur, ast.Name):
                    mod = imports.get(cur.id)
                    if mod and not mod.startswith("tests.") and not mod.startswith("_pytest"):
                        return {
                            "owner_alias": cur.id,
                            "owner_module": mod,
                            "method": chain[-1] if chain else "?",
                            "snippet": snippet,
                            "binding": "BOUND",
                        }
        elif isinstance(f, ast.Name):
            mod = imports.get(f.id)
            if mod and not mod.startswith("tests.") and not mod.startswith("_pytest") and not mod.startswith("unittest") and not mod.startswith("pytest"):
                return {
                    "owner_alias": f.id,
                    "owner_module": mod,
                    "method": "(module-level call)",
                    "snippet": snippet,
                    "binding": "BOUND",
                }
    return {"owner_alias": None, "owner_module": None, "method": None, "snippet": None, "binding": "UNBOUND"}


def _classify(test: dict) -> str:
    smells = {s["type"] for s in test["smells"]}
    if test["skip_count"] > 0 and test["assert_count"] == 0:
        return "XPASS"
    if test.get("self_fake_likely"):
        return "REWRITE"
    if test.get("mock_target_warning"):
        return "REWRITE"
    if "self_fake_verification" in smells:
        return "REWRITE"
    if not smells and test["assert_count"] >= 1 and test["production_target"]["binding"] == "BOUND":
        return "KEEP"
    if smells and test["production_target"]["binding"] == "UNBOUND":
        return "DELETE"
    if "tautology_identical_operands" in smells:
        return "REWRITE"
    if "silent_pass" in smells or "broad_silent_except" in smells or "soft_import_guard" in smells:
        return "REWRITE"
    if {"callable_only", "hasattr_only", "assert_is_not_none"} & smells and test["production_target"]["binding"] == "UNBOUND":
        return "DELETE"
    if {"callable_only", "hasattr_only", "assert_is_not_none", "endswith_only", "weak_lower_bound_zero"} & smells:
        return "STRENGTHEN"
    if "endswith_only" in smells and test["production_target"]["binding"] == "UNBOUND":
        return "DELETE"
    if test["assert_count"] == 0:
        return "INVESTIGATE"
    return "REWRITE"


# ---------------------------------------------------------------------------
# main per-file

TEST_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)


def extract_tests_in_file(path: Path) -> list[dict]:
    src = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return [{
            "file": str(path.relative_to(REPO)),
            "parse_error": f"{e.msg} @ {e.lineno}:{e.offset}",
            "tests": [],
        }]
    imports = _resolve_imports(tree)
    rel = str(path.relative_to(REPO))
    out: list[dict] = []
    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        for fn in [n for n in cls.body if isinstance(n, TEST_FUNCS)]:
            if not fn.name.startswith("test"):
                continue
            out.append(_summarize(fn, cls, rel, imports))
    for fn in [n for n in tree.body if isinstance(n, TEST_FUNCS)]:
        if not fn.name.startswith("test"):
            continue
        out.append(_summarize(fn, None, rel, imports))
    return out


def _summarize(fn, cls, rel, imports) -> dict:
    fn_lo, fn_hi = _function_body_range(fn)
    calls = _walk_collect_calls(fn)
    asserts = _walk_collect_asserts(fn)
    smells = _collect_assertion_smells(asserts, calls)
    skips = _collect_skips(fn)
    silent = _collect_silent_excepts(fn)
    soft_imports = _collect_import_fallbacks(fn)
    mocks = _collect_mocks_patches(fn)
    fixtures = _collect_fixtures_used(fn, cls)
    prod_calls, mock_like = _collect_production_calls(fn, rel, calls)
    target = _production_target_heuristic(fn, imports, calls)
    self_fake = _detect_self_fake(fn, mocks.get("monkeypatch_targets", []))
    mock_target_warning = _detect_mock_of_target(fn, target)
    if self_fake:
        smells.append(self_fake)

    # self-fake heuristic: at least one Compare with literal RHS where literal is a Name defined in the same fn
    local_names = _collect_names(fn)
    body_assign_names = set()
    for stmt in fn.body:
        if isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                if isinstance(t, ast.Name):
                    body_assign_names.add(t.id)

    result = {
        "test_id": f"{rel}::{fn.name}",
        "file": rel,
        "class": cls.name if cls else None,
        "line_start": fn_lo,
        "line_end": fn_hi,
        "function": fn.name,
        "async": isinstance(fn, ast.AsyncFunctionDef),
        "decorators": [ast.unparse(d)[:200] for d in fn.decorator_list],
        "markers": sorted(_has_pytest_marker(fn.decorator_list + sum((c.decorator_list for c in ast.walk(fn) if isinstance(c, ast.ClassDef)), []))),
        "fixtures": fixtures,
        "assert_count": len(asserts),
        "smells": smells,
        "smell_count": len(smells),
        "skip_count": len(skips),
        "silent_except_count": len(silent),
        "silent_excepts": silent,
        "skip_lines": skips,
        "mocks": mocks,
        "production_target": target,
        "prod_call_count": len(prod_calls),
        "first_prod_calls_sample": prod_calls[:8],
        "self_fake_likely": self_fake is not None,
        "self_fake_detail": self_fake,
        "mock_target_warning": mock_target_warning,
        "soft_import_guards": soft_imports,
    }
    result["verdict"] = _classify(result)
    return result


# ---------------------------------------------------------------------------
# main


def main(strict: bool = False, summary: bool = False) -> int:
    all_files = []
    for root in TEST_ROOTS:
        if not root.exists():
            continue
        for p in root.rglob("*.py"):
            if p.name in SKIP_FILES:
                continue
            if "__pycache__" in p.parts:
                continue
            all_files.append(p)

    records: list[dict] = []
    summary_rows = []
    for p in sorted(all_files):
        per_file = extract_tests_in_file(p)
        if per_file and "parse_error" in per_file[0] and not per_file[0].get("tests"):
            records.append({"file": per_file[0]["file"], "parse_error": per_file[0]["parse_error"], "tests": []})
            continue
        # extract_tests_in_file returns flat list; rewrite as one record per file
        rel = str(p.relative_to(REPO))
        tests = per_file
        records.append({"file": rel, "tests": tests})

    # Write JSON
    payload = {
        "repo_root": str(REPO),
        "total_files": len(records),
        "total_tests": sum(len(r["tests"]) for r in records),
        "verdict_counter": dict(Counter(t["verdict"] for r in records for t in r["tests"])),
        "smell_counter": dict(Counter(s["type"] for r in records for t in r["tests"] for s in t["smells"])),
        "records": records,
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    # Summary
    lines = []
    lines.append(f"Total test files: {payload['total_files']}")
    lines.append(f"Total test_* functions: {payload['total_tests']}")
    lines.append("Verdicts:")
    for k, v in sorted(payload["verdict_counter"].items(), key=lambda kv: -kv[1]):
        lines.append(f"  {k}: {v}")
    lines.append("Top smells:")
    for k, v in sorted(payload["smell_counter"].items(), key=lambda kv: -kv[1])[:25]:
        lines.append(f"  {k}: {v}")
    lines.append("")
    lines.append("Worst files by smell count:")
    file_smell = [(r["file"], sum(t["smell_count"] for t in r["tests"]), len(r["tests"])) for r in records if r["tests"]]
    for f, sm, total in sorted(file_smell, key=lambda x: -x[1])[:20]:
        lines.append(f"  {sm:4d} smells / {total:3d} tests  {f}")
    OUT_SUMMARY.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))

    if strict:
        verdict_counter = payload["verdict_counter"]
        smell_counter = payload["smell_counter"]
        failures = []
        for smell in ("self_fake_verification", "tautology_identical_operands",
                       "broad_silent_except"):
            if smell_counter.get(smell, 0) > 0:
                failures.append(f"{smell}: {smell_counter[smell]}")
        if verdict_counter.get("DELETE", 0) > 0:
            failures.append(f"DELETE: {verdict_counter['DELETE']}")
        if verdict_counter.get("XPASS", 0) > 0:
            failures.append(f"XPASS: {verdict_counter['XPASS']}")
        if failures:
            print("\n[STRICT MODE FAIL] " + "; ".join(failures), file=sys.stderr)
            return 1
        print("\n[STRICT MODE OK]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="AST-based test suite audit.")
    parser.add_argument("--strict", action="store_true",
                        help="Exit non-zero on any new self_fake_verification/tautology/broad_silent_except/DELETE/XPASS.")
    args = parser.parse_args()
    sys.exit(main(strict=args.strict))
