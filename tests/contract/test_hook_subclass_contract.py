"""Контрактный тест: кастомные хуки lib/hooks/* обязаны наследовать AgentHook.

nanobot 0.3.5 добавил ``before_run`` и ``finalize_content`` в ``AgentHook``,
которые ``CompositeHook`` раскручивает через ``getattr(h, method_name)``.
Если кастомный хук — bare-класс, ``CompositeHook._for_each_hook_safe``
бросит ``AttributeError`` (а для ``finalize_content`` ``CompositeHook``
не изолирует ошибку — она ВСПЛЫВАЕТ и роняет оборот).

Этот тест фиксирует инвариант:

  Каждый хук в ``lib/hooks/`` обязан наследовать ``AgentHook``, иначе
  при апгрейдах nanobot раскроются именно такие регрессии.

Запускается через ``pytest -m contract``.
"""
from __future__ import annotations

import importlib
from typing import Callable

import pytest

pytestmark = pytest.mark.contract

CUSTOM_HOOKS: list[tuple[str, str]] = [
    ("lib.hooks.database_logging_hook", "DatabaseLoggingHook"),
    ("lib.hooks.tool_audit_hook", "ToolAuditHook"),
    ("lib.hooks.terminal_tool_print_hook", "TerminalToolPrintHook"),
    ("lib.hooks.repeat_guard_hook", "RepeatGuardHook"),
]

LIFECYCLE_METHODS: tuple[str, ...] = (
    "before_run",
    "after_run",
    "on_error",
    "on_finally",
    "before_iteration",
    "after_iteration",
    "on_stream",
    "on_stream_end",
    "on_provider_tool_event",
    "before_execute_tools",
    "before_execute_tool",
    "after_execute_tool",
    "on_execute_tool_error",
    "emit_reasoning",
    "emit_reasoning_end",
    "finalize_content",
    "wants_streaming",
)


def _import(cls_path: tuple[str, str]):
    module_name, class_name = cls_path
    return getattr(importlib.import_module(module_name), class_name)


#: Явные конструкторы: у хуков разные сигнатуры, и угадывать их по
#: ``inspect.signature`` — значит завести тест, который ломается от чужого
#: косметического рефакторинга, а не от реальной регрессии.
_BUILDERS: dict[tuple[str, str], Callable[[], object]] = {
    ("lib.hooks.database_logging_hook", "DatabaseLoggingHook"): lambda: (
        _import(("lib.hooks.database_logging_hook", "DatabaseLoggingHook"))(
            None, session_key="s1", request_id="r1"
        )
    ),
    ("lib.hooks.tool_audit_hook", "ToolAuditHook"): lambda: _import(
        ("lib.hooks.tool_audit_hook", "ToolAuditHook")
    )(),
    ("lib.hooks.terminal_tool_print_hook", "TerminalToolPrintHook"): lambda: _import(
        ("lib.hooks.terminal_tool_print_hook", "TerminalToolPrintHook")
    )(),
    ("lib.hooks.repeat_guard_hook", "RepeatGuardHook"): lambda: _import(
        ("lib.hooks.repeat_guard_hook", "RepeatGuardHook")
    )(None),
}


@pytest.mark.parametrize("cls_path", CUSTOM_HOOKS, ids=lambda p: p[1])
def test_custom_hook_inherits_agent_hook(cls_path):
    """Регрессия для nanobot 0.3.5: все три хука обязаны быть AgentHook."""
    from nanobot.agent import AgentHook

    cls = _import(cls_path)
    assert issubclass(cls, AgentHook), (
        f"{cls.__name__} must inherit AgentHook, иначе CompositeHook "
        f"упадёт на before_run/finalize_content при апгрейде nanobot."
    )


@pytest.mark.parametrize("cls_path", CUSTOM_HOOKS, ids=lambda p: p[1])
def test_custom_hook_defines_full_lifecycle_surface(cls_path):
    """Все 17 lifecycle-методов AgentHook должны быть определены на классе
    (либо как override, либо как inherited из AgentHook).

    Это контракт, который nanobot раскрывает через ``CompositeHook`` —
    любой добавленный метод без override должен прийти из базы. Если база
    не подключена — тест это поймает.
    """
    cls = _import(cls_path)
    for name in LIFECYCLE_METHODS:
        assert callable(getattr(cls, name, None)), (
            f"{cls.__name__}.{name} missing — "
            f"CompositeHook._for_each_hook_safe упадёт с AttributeError."
        )


@pytest.mark.parametrize("cls_path", CUSTOM_HOOKS, ids=lambda p: p[1])
def test_custom_hook_init_calls_super(cls_path):
    """__init__ хука должен вызвать ``super().__init__()`` — иначе не
    инициализируется ``AgentHook._reraise``, который проверяет
    ``CompositeHook._for_each_hook_safe`` (флаг re-raise vs safe-log).

    Аргументы допускаются: ``RepeatGuardHook`` обязан передать
    ``reraise=True``, иначе ``_for_each_hook_safe`` проглотит его
    ``RepeatGuardBlocked`` и режим ``block`` станет молчаливым no-op.
    """
    import inspect
    import re

    cls = _import(cls_path)
    src = inspect.getsource(cls.__init__)
    assert re.search(r"super\([^)]*\)\.__init__\(", src), (
        f"{cls.__name__}.__init__ must call super().__init__() "
        f"для инициализации AgentHook._reraise."
    )


@pytest.mark.parametrize("cls_path", CUSTOM_HOOKS, ids=lambda p: p[1])
def test_reraise_flag_is_actually_set(cls_path):
    """Флаг ``_reraise`` обязан быть выставлен конструктором.

    Проверка на исходнике кода (``super().__init__()`` в теле ``__init__``)
    пропускала бы вызов в ветке, которая не выполняется. Здесь инстанс
    строится по-настоящему — единственный способ убедиться, что флаг
    дошёл до объекта.
    """
    cls = _import(cls_path)
    hook = _BUILDERS[cls_path]()
    assert isinstance(hook._reraise, bool), f"{cls.__name__}: _reraise не выставлен"
