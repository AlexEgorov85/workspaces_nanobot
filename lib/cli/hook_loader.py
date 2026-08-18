"""HookLoader — авто-сканирование workspace/hooks/*.py для AgentHook-подклассов.

``workspace/hooks/`` — это директория ПЛАГИНОВ проекта: каждый ``*.py`` файл
должен содержать самодостаточный ``AgentHook``-подкласс, который можно
инстанцировать через ``cls(workspace_dir=workspace_dir)`` (единый контракт
для всех плагинов). Фреймворковые хуки (``lib/hooks/``: ``ToolAuditHook``,
``DatabaseLoggingHook``, ``BaseToolTrackingHook``) сюда НЕ входят — их
провязывает ``AgentFactory``/``ApplicationContext`` явно, поэтому здесь
не нужны ни ``inspect.signature``, ни маркеры-исключения, ни чёрные списки.

Сканер на успех молчит: полный список подключённых хуков (плагины +
фреймворковые) печатает ``ApplicationContext`` один раз после создания
агента — единая точка, без дублирующих сообщений.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, List


def scan_and_register(hooks_dir: Path, workspace_dir: Path) -> List[Any]:
    """Сканировать ``hooks_dir`` и вернуть список инстанцированных плагинов.

    Каждый ``*.py`` файл (исключая ``_*``) импортируется через
    ``importlib.util.spec_from_file_location`` под уникальным именем
    ``hooks.<basename>`` — это работает без зависимости от того, в каком
    порядке ``workspace/`` и ``workspace/hooks/`` добавлены в ``sys.path``
    (раньше код делал ``importlib.import_module(path.name[:-3])``, что
    требовало ``hooks/`` в ``sys.path`` как top-level — и в gateway это
    ломалось: warning No module named 'session_file_redirect_hook').

    Каждый найденный ``AgentHook``-подкласс инстанцируется единообразно
    через ``cls(workspace_dir=workspace_dir)``. Классы, которые не удалось
    импортировать или инстанцировать, пропускаются с warning'ом — сканер
    не ломает старт из-за одного битого плагина. На успехе не печатает
    ничего (см. docstring модуля).
    """
    from nanobot.agent import AgentHook

    hooks: List[Any] = []

    if not hooks_dir.is_dir():
        return hooks

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
    return hooks


def _print_warn(msg: str) -> None:
    try:
        from rich.console import Console
        Console().print(f"[yellow]⚠[/yellow] {msg}")
    except Exception:
        pass
