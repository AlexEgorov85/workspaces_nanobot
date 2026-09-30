"""HookLoader — авто-сканирование workspace/hooks/*.py для AgentHook-подклассов.

``workspace/hooks/`` — это директория ПЛАГИНОВ проекта: каждый ``*.py`` файл
должен содержать самодостаточный ``AgentHook``-подкласс, который можно
инстанцировать через ``cls(workspace_dir=workspace_dir)`` (единый контракт
для всех плагинов). Фреймворковые хуки (``lib/hooks/``: ``ToolAuditHook``,
``TerminalToolPrintHook``, ``DatabaseLoggingHook``) сюда НЕ входят — их
провязывает ``AgentFactory``/``ApplicationContext`` явно, поэтому здесь
не нужны ни ``inspect.signature``, ни маркеры-исключения, ни чёрные списки.

Сканер на успех молчит: полный список подключённых хуков (плагины +
фреймворковые) печатает ``ApplicationContext`` один раз после создания
агента — единая точка, без дублирующих сообщений.

Allowlist действительно ограничивающий: файл, чьё ``stem`` отсутствует
в ``_allowed_hook_names()``, **не импортируется и не выполняется**.
Прежнее поведение «warn + продолжить импорт» удалено (см. opencode
change ``runtime-patcher-composition-cleanup``, Decision 4).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

from loguru import logger


def scan_and_register(hooks_dir: Path, workspace_dir: Path) -> list[Any]:
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

    Имя файла должно быть в ``ALLOWED_HOOKS`` (allowlist) — это
    **действительно ограничивающий** механизм. Не-alwisted файлы
    **пропускаются целиком**: ни ``spec_from_file_location``, ни
    ``exec_module``, ни поиск ``AgentHook``-подклассов не вызываются.
    Пропуск логируется через ``logger.info`` с явным маркером
    «hook not in allowlist, skipped» (без ``rich.console``-warning,
    чтобы не давать false sense of security).
    """
    from nanobot.agent import AgentHook

    hooks: list[Any] = []

    if not hooks_dir.is_dir():
        return hooks

    allowed = _allowed_hook_names()

    for path in sorted(hooks_dir.iterdir()):
        if not path.is_file() or not path.name.endswith(".py") or path.name.startswith("_"):
            continue
        if path.stem not in allowed:
            # Hard-skip: файл вне allowlist не импортируется и не
            # выполняется. Если нужно зарегистрировать новый hook —
            # добавьте его имя в ``_allowed_hook_names()`` ниже.
            logger.info(
                "hook {} not in allowlist, skipped — добавьте в "
                "lib/cli/hook_loader.py::_allowed_hook_names()",
                path.name,
            )
            continue
        module_name = f"hooks.{path.stem}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                logger.warning(
                    "hook {} spec_from_file_location failed", path.name,
                )
                continue
            mod = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = mod
            try:
                spec.loader.exec_module(mod)
            except Exception as exc:
                # Откатить частично загруженный модуль из sys.modules
                # (см. поведение scan_and_register до этого patch).
                sys.modules.pop(module_name, None)
                logger.warning("hook {} failed to import: {}", path.name, exc)
                continue
        except Exception as exc:
            logger.warning("hook {} failed: {}", path.name, exc)
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
                    logger.warning(
                        "hook attr {} failed to instantiate: {}", attr_name, exc,
                    )
                    continue
                hooks.append(hook)
    return hooks


def _allowed_hook_names() -> frozenset[str]:
    """Allowlist имён плагинов в ``workspace/hooks/``.

    Защита от случайного добавления плагина, который не прошёл
    ревью. См. openspec/changes/post-0.3.5-patches-cleanup (группа 7.3)
    и opencode change ``runtime-patcher-composition-cleanup`` (Decision 4 —
    allowlist действительно ограничивающий).
    """
    return frozenset({
        "session_file_redirect_hook",
        "recent_files_hook",
        "debug_stream_diag",
    })
