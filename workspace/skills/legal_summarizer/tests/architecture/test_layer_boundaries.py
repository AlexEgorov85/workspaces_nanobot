"""Architecture boundary guard for ``legal_summarizer`` layered package.

Forbids ``import`` / ``import ... from`` between layers in the
direction of the dependency graph:

    domain       ← document  ← retrieval ← application
                         ↑          ↓
                      chunking  execution
                         ↓          ↓
                       llm  ← planning → output, infrastructure

Concrete rules (from AGENTS.md §65 / `docs/TARGET_ARCHITECTURE.md`):

* ``domain`` may not import any layer.
* ``document`` may not import ``retrieval``, ``execution``, ``llm``, ``planning``.
* ``retrieval`` may not import ``execution``, ``llm``.
* ``planning`` may not import ``llm``, ``execution``.
* ``chunking`` may not import ``execution``, ``llm``.
* ``execution`` may not import ``document``, ``retrieval``, ``llm``.
* ``application`` may import anything.
* ``llm``, ``output``, ``cache``, ``infrastructure`` are leaves
  (no imports from internal layers required — allowed: any).

The test walks every ``.py`` under
``workspace/skills/legal_summarizer/legal_summarizer`` and asserts
that no module reaches a forbidden target via ``legal_summarizer.<layer>...``.
"""

from __future__ import annotations

import ast
from pathlib import Path


_SRC_ROOT = (
    Path(__file__).resolve().parents[3]
    / "legal_summarizer"
)

# layer name -> set of layer names it MUST NOT import
_FORBIDDEN: dict[str, frozenset[str]] = {
    "domain": frozenset({
        "document", "chunking", "retrieval",
        "planning", "execution", "llm",
        "application", "cache", "output", "infrastructure",
    }),
    "document": frozenset({
        "retrieval", "execution", "llm", "planning",
    }),
    "chunking": frozenset({
        "execution", "llm",
    }),
    "retrieval": frozenset({
        "execution", "llm",
    }),
    "planning": frozenset({
        "llm",
    }),
    "execution": frozenset({
        "document", "retrieval", "llm",
    }),
}


def _layer_of(path: Path) -> str | None:
    rel = path.relative_to(_SRC_ROOT)
    parts = rel.parts
    if len(parts) < 2:
        return None
    return parts[0]


def _imported_layer(module: str | None) -> str | None:
    """Return the layer name if ``module`` is an internal ``legal_summarizer.*`` import."""
    if not module or not module.startswith("legal_summarizer."):
        return None
    parts = module.split(".")
    if len(parts) < 2:
        return None
    return parts[1]


def _walk_module(path: Path) -> list[tuple[str, str]]:
    """Return list of (source_line, fully-qualified-module) imports in ``path``."""
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return []
    hits: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            hits.append((f"L{node.lineno}", mod))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                hits.append((f"L{alias.lineno}", alias.name))
    return hits


def _collect_violations() -> list[str]:
    violations: list[str] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        layer = _layer_of(path)
        if layer is None or layer not in _FORBIDDEN:
            continue
        forbidden = _FORBIDDEN[layer]
        for src_line, mod in _walk_module(path):
            target_layer = _imported_layer(mod)
            if target_layer is None or target_layer == layer:
                continue
            if target_layer in forbidden:
                rel = path.relative_to(_SRC_ROOT.parent.parent.parent)
                violations.append(
                    f"{rel} {src_line}: {layer} → {target_layer} "
                    f"({mod})"
                )
    return violations


def test_no_layer_boundary_violations():
    """Внутри ``legal_summarizer`` слои не должны ссылаться на запрещённые зависимости."""
    violations = _collect_violations()
    assert violations == [], (
        "Architecture boundary violations:\n  - " +
        "\n  - ".join(violations)
    )
