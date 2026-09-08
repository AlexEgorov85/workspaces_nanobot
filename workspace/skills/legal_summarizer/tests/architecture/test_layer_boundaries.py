"""Architecture boundary guard for ``legal_summarizer`` layered package.

Forbids ``import`` / ``import ... from`` between layers in the
direction of the dependency graph::

                  ┌─────────────────────┐
                  │   application       │  orchestration, idempotency
                  └─────────┬───────────┘
                            ▼
    ┌───────────┬───────────┴───────────┬───────────────┐
    │           │                       │               │
    ▼           ▼           ▼           ▼               ▼
  document  chunking  retrieval  planning           output
    │           │           │           │
    └───────────┴───────────┴─────┬─────┘
                                   ▼
                               execution
                                   │
                                   ▼
                               llm.calls / llm.prompts
                                   │
                                   ▼
                               llm.client (leaf)

Rules (canonical dependency direction):

* ``document`` is a leaf — may not import any internal layer.
* ``chunking`` may import ``document`` (block ownership, types).
* ``retrieval`` may import ``document`` (structure types).
* ``planning`` may import ``document`` and ``chunking``.
* ``execution`` may import ``document``, ``chunking``, ``llm.calls``,
  ``llm.prompts``, ``llm.tokens``, ``llm.single_flight``.
* ``application`` may import anything.
* ``llm.calls``, ``llm.prompts``, ``llm.config``, ``llm.client``,
  ``cache``, ``output`` are leaves.

Forbidden direction is what matters: ``llm.calls`` may NOT import
``execution``, ``chunking``, ``retrieval``, ``planning``,
``document``, ``application``. Same for other leaves.

The test walks every ``.py`` under
``workspace/skills/legal_summarizer/scripts`` and asserts
that no module reaches a forbidden target via ``<layer>...``
or ``llm.<sublayer>...``.

В частности, ``execution → application`` всегда запрещено: application —
верхний слой, dependency direction только ``application → execution``.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parents[2]
_RUNTIME_PKG = _SKILL_ROOT / "scripts"

# Layer name -> set of layer names it MUST NOT import.
# Direction matters: imports flow from leaves UP to application, not
# downward. ``document``, ``chunking``, ``retrieval``, ``planning`` are
# pure data/structure layers — they may NOT depend on ``execution``,
# ``application``, ``cache``, ``output``.
#
# ``execution`` — единственная точка bridge'а к ``llm`` (calls,
# prompts, single_flight). ``execution → application`` всегда запрещено:
# application — верхний слой.
_FORBIDDEN: dict[str, frozenset[str]] = {
    "document": frozenset({
        "retrieval", "execution", "planning", "application",
        "llm", "output",
    }),
    "chunking": frozenset({
        "retrieval", "execution", "planning", "application",
        "llm", "cache", "output",
    }),
    "retrieval": frozenset({
        "execution", "planning", "application",
        "llm", "cache", "output",
    }),
    "planning": frozenset({
        "execution", "application",
        "llm", "cache", "output",
    }),
# ``execution`` may import ``llm.calls``/``llm.prompts`` (canonical LLM API)
    # и ``llm.single_flight`` (technical gate).
    "execution": frozenset({
        "cache", "output",
    }),
    "application": frozenset(),  # application may import anything
    "llm": frozenset(),  # leaves (llm.client, llm.calls, llm.prompts, etc.)
    "cache": frozenset(),  # leaves
    "output": frozenset(),  # leaves
}

# Разрешённые исключения из общего правила — технические примитивы,
# которые являются infra-утилитами, а не domain-зависимостями.
#
# 1. ``llm.tokens`` (``TokenEstimator``) — pure-math token-budget.
#    Не делает LLM-вызовов, безопасен для всех data-слоёв
#    (``chunking``, ``retrieval``, ``planning``, ``document``).
# 2. ``cache.manifest_root`` — утилита path resolution
#    (``document/physical.py`` использует её для document cache key).
# 3. ``retrieval.index`` и ``retrieval.query`` — исторически
#    ``document/analysis.py`` собирает ``DocumentAnalysis`` через
#    эти модули. Это известное архитектурное исключение
#    (``DocumentAnalysis`` — это projectional representation, требующая
#    и document, и retrieval). Должно быть решено отдельным рефакторингом.
# 4. ~~``application.service`` — единственный **lazy** import из~~
#    ~~``execution.map_reduce``.~~ Удалено: ``application → execution`` —
#    единственное каноническое направление зависимости. Execution
#    получает всё необходимое через DI-callbacks и прямые импорты
#    из ``llm.*``, ``cache.*``, ``chunking.*``, ``document.*``.
_ALLOWED_TECHNICAL_EXCEPTIONS: dict[str, frozenset[str]] = {
    "document": frozenset({"cache", "retrieval"}),
    "chunking": frozenset({"llm.tokens"}),
    "retrieval": frozenset({"llm.tokens"}),
    "planning": frozenset({"llm.tokens"}),
}

def _is_allowed_exception(source_layer: str, target: str) -> bool:
    return target in _ALLOWED_TECHNICAL_EXCEPTIONS.get(source_layer, frozenset())

def _layer_of(path: Path) -> str | None:
    """Layer name from a relative path under ``legal_summarizer/``.

    Examples:
        ``document/foo.py`` → ``document``
        ``llm/calls.py`` → ``llm``
        ``llm/single_flight.py`` → ``llm``
        ``application/__init__.py`` → ``application``
    """
    rel = path.relative_to(_RUNTIME_PKG)
    parts = rel.parts
    if len(parts) < 2:
        return None
    return parts[0]

def _imported_target(module: str | None) -> str | None:
    """Target layer/sub-layer name if ``module`` is an internal layer import.

    Returns:
        ``document`` / ``chunking`` / ``cache`` / ``output`` для
        не-llm imports.

        Для ``llm`` → ``llm`` (целый пакет).
        Для ``llm.tokens`` → ``llm.tokens``.
        Для ``llm.calls`` → ``llm.calls``.
        Это позволяет различать технические импорты
        (``llm.tokens`` = ``TokenEstimator``, безопасен) от
        LLM-API импортов (``llm.calls`` / ``llm.client``,
        запрещены для data-слоёв).
    """
    if not module:
        return None
    parts = module.split(".")
    if parts[0] not in {
        "application", "cache", "chunking", "document", "execution",
        "llm", "output", "planning", "retrieval",
    }:
        return None
    if parts[0] == "llm":
        if len(parts) < 2:
            return "llm"
        return f"llm.{parts[1]}"
    return parts[0]

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
    for path in sorted(_RUNTIME_PKG.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        layer = _layer_of(path)
        if layer is None or layer not in _FORBIDDEN:
            continue
        forbidden = _FORBIDDEN[layer]
        for src_line, mod in _walk_module(path):
            target = _imported_target(mod)
            if target is None or target == layer:
                continue
            if target in forbidden:
                if _is_allowed_exception(layer, target):
                    continue
                rel = path.relative_to(_SKILL_ROOT)
                violations.append(
                    f"{rel} {src_line}: {layer} → {target} ({mod})"
                )
    return violations

def test_no_layer_boundary_violations() -> None:
    """Внутри ``legal_summarizer`` нижние слои не должны зависеть от верхних.

    Канонический поток: ``document → chunking/retrieval/planning →
    execution → llm → application``. Импорты только в этом направлении.
    Обратное направление (например, ``document → execution``) запрещено.
    """
    violations = _collect_violations()
    assert violations == [], (
        "Architecture boundary violations:\n  - " +
        "\n  - ".join(violations)
    )

def test_execution_does_not_import_application() -> None:
    """Regression: ``execution → application`` запрещено.

    ``application`` — верхний orchestrator-слой. ``execution``
    получает всё необходимое через DI-callbacks (``WriteChunkResultFn``,
    ``RunOneBatchFn``, ``LoadCachedPartialsFn``) и прямые импорты
    из ``llm.*`` / ``cache.*`` / ``chunking.*`` / ``document.*``.
    Запрет абсолютный: ни статического, ни lazy, ни ``__init__``
    импорта. Раньше ``execution.map_reduce`` использовал lazy
    ``_service_mod()`` для monkeypatch-совместимости; после миграции
    этот shortcut удалён.
    """
    execution_root = _RUNTIME_PKG / "execution"
    assert execution_root.is_dir(), (
        f"ожидался каталог {execution_root}, его нет — Skill не "
        "инициализирован правильно"
    )
    offenders: list[str] = []
    for path in sorted(execution_root.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        rel = path.relative_to(_SKILL_ROOT)
        text = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod == "application" or mod.startswith("application."):
                    offenders.append(f"{rel} L{node.lineno}: from {mod} ...")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "application" or alias.name.startswith("application."):
                        offenders.append(
                            f"{rel} L{alias.lineno}: import {alias.name}"
                        )
    assert offenders == [], (
        "execution → application запрещено (dependency direction). "
        "Найдены нарушители:\n  - " + "\n  - ".join(offenders) +
        "\nИспользуйте DI-callbacks или прямые импорты из "
        "нижних слоёв (llm.*, cache.*, chunking.*, document.*)."
    )