"""Архитектурный страж границ платформы.

Проверяет правила из ``README.md`` автоматически. Это единственная защита
от тихого возврата зависимости от агента: человек забудет, CI — нет.

Правила:
1. В ``mcp-platform/**`` запрещены импорты агента и его внутренних пакетов.
2. Никаких динамических импортов запрещённых модулей.
3. Никаких ссылок на внутренности AgentLoop (AgentLoop/ToolContext/MessageBus).
4. Каждый ``servers/*/server.py`` обязан импортироваться при ЗАПРЕЩЁННОМ
   ``nanobot`` — то есть реально стартовать без агента.
5. Серверы не импортируют друг друга.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
SELF = Path(__file__).resolve()

#: Корни, которые нельзя импортировать: агент и его внутренние пакеты.
FORBIDDEN_ROOTS = frozenset(
    {
        "nanobot",
        "lib",
        "workspace",
        "tools",
        "benchmarks",
        "config",
        "cli_agent",
        "gateway",
        "streamlit_app",
    }
)

#: Внутренности agent runtime, которых не должно быть даже косвенно.
AGENT_INTERNALS = ("AgentLoop", "ToolContext", "MessageBus", "CommandRouter")

#: Динамический импорт в обход правил.
DYNAMIC_IMPORT = re.compile(r"import_module\(\s*[\"']([A-Za-z_][\w.]*)[\"']")


def _python_files() -> list[Path]:
    return sorted(
        p
        for p in PLATFORM_ROOT.rglob("*.py")
        if "__pycache__" not in p.parts and ".venv" not in p.parts
    )


def _server_modules() -> list[Path]:
    return sorted(PLATFORM_ROOT.glob("servers/*/server.py"))


def _rel(path: Path) -> str:
    return path.relative_to(PLATFORM_ROOT).as_posix()


def _imported_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and not node.level:
            if node.module:
                roots.add(node.module.split(".")[0])
    return roots


def test_platform_is_not_empty() -> None:
    """Страховка от «тест зелёный потому что сканировать нечего»."""
    files = _python_files()
    assert len(files) >= 6, f"ожидались реальные файлы платформы, найдено {len(files)}"
    assert _server_modules(), "нет ни одного MCP-сервера — страж нечего проверять"


@pytest.mark.parametrize("path", _python_files(), ids=_rel)
def test_no_forbidden_imports(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    bad = _imported_roots(tree) & FORBIDDEN_ROOTS
    assert not bad, f"{_rel(path)} импортирует запрещённое: {sorted(bad)}"


@pytest.mark.parametrize("path", _python_files(), ids=_rel)
def test_no_dynamic_forbidden_imports(path: Path) -> None:
    if path == SELF:
        return
    found = {m.group(1) for m in DYNAMIC_IMPORT.finditer(path.read_text(encoding="utf-8"))}
    bad = {name for name in found if name.split(".")[0] in FORBIDDEN_ROOTS}
    assert not bad, f"{_rel(path)} динамически импортирует запрещённое: {sorted(bad)}"


@pytest.mark.parametrize("path", _python_files(), ids=_rel)
def test_no_agent_internals(path: Path) -> None:
    if path == SELF:
        return
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
    bad = used & set(AGENT_INTERNALS)
    assert not bad, f"{_rel(path)} использует внутренности агента: {sorted(bad)}"


@pytest.mark.parametrize("path", _server_modules(), ids=_rel)
def test_server_imports_without_nanobot(path: Path) -> None:
    """Главный gate: сервер поднимается в процессе, где импорт агента запрещён.

    Блокировка ставится на уровне ``sys.meta_path`` — это ловит и прямые
    ``import nanobot``, и транзитивные подтягивания из SDK.
    """
    module = "servers." + path.parent.name + ".server"
    code = f"""
import sys
from importlib.abc import MetaPathFinder

class BanAgent(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "nanobot" or fullname.startswith("nanobot."):
            raise ImportError("nanobot is not allowed in mcp-platform")
        return None

sys.meta_path.insert(0, BanAgent())
sys.path.insert(0, {str(PLATFORM_ROOT)!r})

import importlib
mod = importlib.import_module({module!r})
assert hasattr(mod, "mcp"), "server module must expose `mcp`"
print("OK")
"""
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(PLATFORM_ROOT),
        timeout=120,
    )
    assert proc.returncode == 0, f"{module} не поднялся без агента:\n{proc.stderr}"
    assert "OK" in proc.stdout


def test_servers_do_not_import_each_other() -> None:
    """Серверы — независимые процессы, не слои одной программы."""
    offenders: list[str] = []
    for path in _server_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        own = path.parent.name
        for node in ast.walk(tree):
            target = None
            if isinstance(node, ast.ImportFrom) and node.module:
                target = node.module
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("servers."):
                        offenders.append(f"{_rel(path)} -> {alias.name}")
                continue
            if target and target.startswith("servers."):
                if not target.startswith(f"servers.{own}"):
                    offenders.append(f"{_rel(path)} -> {target}")
    assert not offenders, f"межсерверные импорты запрещены: {offenders}"
