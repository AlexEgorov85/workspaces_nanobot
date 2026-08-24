"""Хелперы для contract-тестов."""

from __future__ import annotations

import inspect
from typing import Any

import pytest


def assert_params(func: Any, positional: list[str], kwonly: list[str] | None = None) -> None:
    """Проверить позиционные и keyword-only параметры функции/метода."""
    if not callable(func):
        pytest.fail(f"{func!r} is not callable")
    sig = inspect.signature(func)
    params = list(sig.parameters.values())
    names = [p.name for p in params if p.kind in (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )]
    kw_names = [p.name for p in params if p.kind == inspect.Parameter.KEYWORD_ONLY]
    assert names[0] in ("self", "cls"), f"first param of {func} is {names[0]}"
    assert names[1 : 1 + len(positional)] == positional, (
        f"{func}: positional {names[1:]} != prefix {positional}"
    )
    if kwonly is not None:
        assert kw_names == kwonly, f"{func}: kwonly {kw_names} != {kwonly}"
