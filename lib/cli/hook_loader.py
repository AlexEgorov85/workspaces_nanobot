"""HookLoader — авто-сканирование workspace/hooks/*.py для AgentHook-подклассов."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, List, Tuple


def scan_and_register(hooks_dir: Path, workspace_dir: Path) -> Tuple[List[Any], Any]:
    """Сканировать ``hooks_dir`` и вернуть (hooks, tool_audit_hook).

    Каждый ``*.py`` файл (исключая ``_*``) импортируется через
    ``importlib.util.spec_from_file_location`` под уникальным именем
    ``hooks.<basename>`` — это работает без зависимости от того, в каком
    порядке ``workspace/`` и ``workspace/hooks/`` добавлены в ``sys.path``
    (раньше код делал ``importlib.import_module(path.name[:-3])``, что
    требовало ``hooks/`` в ``sys.path`` как top-level — и в gateway это
    ломалось: warning No module named 'session_file_redirect_hook').

    Классы-наследники ``AgentHook`` (но не сам ``AgentHook`` и не ``_*``) —
    инстанцируются с ``workspace_dir``. Если конструктор падает — хук
    пропускается, повторной инстанциации без аргументов нет.

    Также ищется ``ToolAuditHook`` для метаданных.
    """
    from nanobot.agent import AgentHook

    hooks: List[Any] = []
    tool_audit_hook: Any = None

    if not hooks_dir.is_dir():
        return hooks, tool_audit_hook

    # Кэшируем индекс hooks-dir: importlib.util требует уникальное имя
    # модуля в sys.modules; используем индекс, чтобы повторный вызов
    # scan_and_register (например, в тестах) переиспользовал модули.
    for path in sorted(hooks_dir.iterdir()):
        if not path.is_file() or not path.name.endswith(".py") or path.name.startswith("_"):
            continue
        module_name = f"hooks.{path.stem}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                _print_warn(f"{path.name}: spec_from_file_location failed")
                continue
            mod = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = mod
            spec.loader.exec_module(mod)
        except Exception as exc:
            _print_warn(f"{path.name}: {exc}")
            continue
        for attr_name in dir(mod):
            attr = getattr(mod, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, AgentHook)
                and attr is not AgentHook
                and not attr_name.startswith("_")
            ):
                try:
                    hook = attr(workspace_dir=workspace_dir)
                except Exception as exc:
                    _print_warn(f"{attr_name}: {exc}")
                    continue
                hooks.append(hook)
                _print_ok(f"{attr_name} loaded")
                if tool_audit_hook is None and _is_tool_audit_hook(hook):
                    tool_audit_hook = hook
    return hooks, tool_audit_hook


def _is_tool_audit_hook(hook: Any) -> bool:
    try:
        from hooks.tool_audit_hook import ToolAuditHook
        return isinstance(hook, ToolAuditHook)
    except Exception:
        return False


def _print_warn(msg: str) -> None:
    try:
        from rich.console import Console
        Console().print(f"[yellow]⚠[/yellow] {msg}")
    except Exception:
        pass


def _print_ok(msg: str) -> None:
    try:
        from rich.console import Console
        Console().print(f"[green]✓[/green] {msg}")
    except Exception:
        pass
