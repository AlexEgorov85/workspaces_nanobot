"""Premature abstraction guard (PLAN §60).

PLAN §60:

> Не делать:
> - ``BaseStructureFactory``
> - ``AbstractHeadingStrategyFactory``
> - ``GenericNodeResolverFactory``
>
> без реального потребителя.
>
> Предпочитать:
> - малый модуль;
> - одна ответственность;
> - простые функции;
> - явные contracts.

Этот модуль предоставляет минимальные helpers для проверки
архитектурных anti-patterns:

* ``is_factory_pattern(name)`` — простая проверка имени класса.
* ``count_abstract_classes(module)`` — подсчёт абстрактных классов.
* ``has_oversized_class(module, max_lines)`` — слишком большой класс.

Используется как guard при code review (не в runtime).
"""

from __future__ import annotations

import inspect
from typing import Any


_FORBIDDEN_FACTORY_PATTERNS = (
    "Factory",
    "Builder",
    "StrategyFactory",
    "ResolverFactory",
    "ProcessorFactory",
)


def is_factory_pattern(name: str) -> bool:
    """True если имя класса выглядит как фабрика/builder."""
    if not name:
        return False
    return any(p in name for p in _FORBIDDEN_FACTORY_PATTERNS)


def count_abstract_classes(module: Any) -> int:
    """Подсчёт абстрактных классов в модуле."""
    count = 0
    for name, obj in inspect.getmembers(module, inspect.isclass):
        if obj.__module__ != module.__name__:
            continue
        if inspect.isabstract(obj):
            count += 1
    return count


def has_oversized_class(module: Any, max_lines: int = 100) -> bool:
    """True если в модуле есть класс > max_lines строк."""
    import re
    source = inspect.getsource(module)
    for name, obj in inspect.getmembers(module, inspect.isclass):
        if obj.__module__ != module.__name__:
            continue
        cls_source = inspect.getsource(obj)
        if cls_source.count("\n") > max_lines:
            return True
    return False


__all__ = [
    "is_factory_pattern",
    "count_abstract_classes",
    "has_oversized_class",
]