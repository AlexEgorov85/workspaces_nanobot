"""Architecture guard: ``lib/services/runtime_patcher.py`` не содержит
project-tool-registration boundary.

Контракт (opencode change ``runtime-patcher-composition-cleanup``,
Decision 3): ``RuntimePatcher`` отвечает ТОЛЬКО за monkey-patch'и upstream
``nanobot.agent.loop.AgentLoop``. Регистрация кастомных tool'ов из
``workspace/tools/*.py`` — ответственность
``lib.services.project_tool_loader`` (независимый stateless helper).

Этот тест через AST-анализ проверяет, что ``runtime_patcher.py`` не
содержит граничных пересечений: импортов ``workspace.tools.*``,
построения ``ToolContext`` или вызова ``agent.tools.register``.

Защищает от регрессии: если кто-то вернёт ``patch_project_tools`` или
добавит прямой импорт ``workspace.tools.*`` в ``runtime_patcher.py`` —
тест упадёт, сигнализируя нарушение архитектурной границы.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TARGET_FILE = REPO_ROOT / "lib" / "services" / "runtime_patcher.py"


def _parse_target() -> ast.Module:
    source = TARGET_FILE.read_text(encoding="utf-8")
    return ast.parse(source, filename=str(TARGET_FILE))


def _walk_imports(tree: ast.Module) -> list[str]:
    """Список dotted-имён всех импортов (``import X.Y.Z`` / ``from X.Y import Z``)."""
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.append(node.module)
    return out


def _walk_attribute_calls(tree: ast.Module) -> list[tuple[str, int]]:
    """Список ``(attr_name, lineno)`` всех вызовов вида ``<obj>.<attr>(...)``."""
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            out.append((func.attr, node.lineno))
    return out


def test_runtime_patcher_does_not_import_workspace_tools() -> None:
    """``runtime_patcher.py`` не должен импортировать ``workspace.tools.*``."""
    tree = _parse_target()
    imports = _walk_imports(tree)
    bad = [m for m in imports if m == "workspace.tools" or m.startswith("workspace.tools.")]
    assert not bad, (
        "RuntimePatcher не должен импортировать workspace.tools.* — "
        f"найдены импорты: {bad}. Регистрация project tools переехала в "
        "lib.services.project_tool_loader."
    )


def test_runtime_patcher_does_not_construct_tool_context() -> None:
    """``runtime_patcher.py`` не должен импортировать ``ToolContext``
    из ``nanobot.agent.tools.context`` (отвечает за регистрацию project tools).

    Исключение: ``current_request_context`` (proxy для per-request
    контекста, используется в ``patch_subagent_logging``) — это
    **не** tool-registration boundary.
    """
    import ast

    tree = _parse_target()
    bad_imports: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if not (
            node.module == "nanobot.agent.tools.context"
            or node.module.endswith(".agent.tools.context")
        ):
            continue
        # Разрешаем импорт только current_request_context / подобных
        # proxy-функций, но не ToolContext (класс для tool registration).
        for alias in node.names:
            if alias.name == "ToolContext":
                bad_imports.append(f"{node.module}:{alias.name}")
    assert not bad_imports, (
        "RuntimePatcher не должен импортировать ToolContext из "
        "nanobot.agent.tools.context — это часть registration boundary "
        f"project tools. Найдено: {bad_imports}. Регистрация переехала "
        "в lib.services.project_tool_loader."
    )


def test_runtime_patcher_does_not_call_agent_tools_register() -> None:
    """``runtime_patcher.py`` не должен вызывать ``agent.tools.register(...)``."""
    tree = _parse_target()
    calls = _walk_attribute_calls(tree)
    bad = [line for attr, line in calls if attr == "register"]
    # ``register`` — допустим для ``self._record``, ``logger.add`` и т.п.,
    # но не для ``<obj>.register`` в режиме tool-registration. Ищем
    # явное ``agent.tools.register`` через анализ вызовов c data-flow.
    # Упрощённая проверка: запрещаем любой ``attr==\"register\"`` вызов.
    # Более точная — через Constant-аргументы или имя-цели.
    assert not bad, (
        "RuntimePatcher не должен вызывать .register(...) — "
        "это ответственность project_tool_loader.register_project_tools. "
        f"Найдены .register(...) вызовы на строках: {bad}"
    )


def test_runtime_patcher_does_not_reference_nanobot_tools_base() -> None:
    """``runtime_patcher.py`` не должен импортировать
    ``nanobot.agent.tools.base`` (Tool-классы — domain project tools)."""
    tree = _parse_target()
    imports = _walk_imports(tree)
    bad = [m for m in imports if m.endswith(".agent.tools.base")]
    assert not bad, (
        "RuntimePatcher не должен импортировать nanobot.agent.tools.base — "
        f"найдены: {bad}."
    )


@pytest.mark.parametrize(
    "forbidden",
    ["project_tools", "workspace.tools"],
)
def test_runtime_patcher_does_not_mention_project_tools(forbidden: str) -> None:
    """``runtime_patcher.py`` не должен содержать упоминаний project tools
    в production-коде (вызовы, имена, импорты).

    Исключение: docstring'и — упоминания ``patch_project_tools`` как
    исторический audit-trail допустимы (см. module docstring).
    """
    tree = _parse_target()

    # Собираем номера строк, которые являются docstring'ами
    # (первый Expr → Constant в теле Module / FunctionDef / AsyncFunctionDef /
    # ClassDef).
    docstring_lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (
            ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
        )):
            if (node.body
                    and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                # Весь docstring обычно занимает несколько строк; используем
                # маркер на первой строке.
                docstring_lines.add(node.body[0].lineno)

    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if forbidden in node.value and node.lineno not in docstring_lines:
                found.append((node.value[:60], node.lineno))
        elif isinstance(node, ast.Name) and node.id == forbidden:
            if node.lineno not in docstring_lines:
                found.append((forbidden, node.lineno))

    assert not found, (
        f"RuntimePatcher не должен содержать упоминаний {forbidden!r} "
        f"в production-коде (вызовы/имена/импорты). Найдено: {found}"
    )
