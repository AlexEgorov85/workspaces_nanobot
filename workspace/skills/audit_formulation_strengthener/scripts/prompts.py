"""Загрузка и рендеринг system-промптов skill'а.

Промпты лежат в ``prompts/*.md`` рядом со ``scripts/`` (один уровень вверх).
Шаблон подставляется через ``render_prompt(template, **vars)`` —
плейсхолдеры вида ``{{NAME}}``.

Жёсткая проверка: после подстановки не должно остаться неразрешённых
``{{...}}`` — иначе ``PromptUnresolvedVarError`` (защита от тихих остатков).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


_SKILL_ROOT = Path(__file__).resolve().parent.parent
_PROMPTS_DIR = _SKILL_ROOT / "prompts"


__all__ = ["load_prompt", "render_prompt", "PromptUnresolvedVarError"]


class PromptUnresolvedVarError(Exception):
    """В шаблоне остались неразрешённые плейсхолдеры ``{{...}}`` после подстановки.

    Attributes:
        unresolved: список имён неразрешённых плейсхолдеров (без ``{{``/``}}``).
        template: исходный шаблон (для диагностики).
    """

    def __init__(self, message: str, *, unresolved: list[str], template: str) -> None:
        super().__init__(message)
        self.unresolved = unresolved
        self.template = template


_PLACEHOLDER_RE = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")
_UNRESOLVED_ANY_RE = re.compile(r"\{\{[^{}]*\}\}")


def load_prompt(name: str) -> str:
    """Загрузить system-промпт из ``prompts/<name>.md``.

    Args:
        name: имя файла без расширения (например, ``"analyze_system"``).

    Returns:
        Содержимое файла.

    Raises:
        FileNotFoundError: если файл не существует.
    """
    path = _PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Промпт не найден: {path}")
    return path.read_text(encoding="utf-8")


def render_prompt(template: str, **vars: Any) -> str:
    """Подставить переменные в шаблон.

    Плейсхолдеры: ``{{NAME}}`` (uppercase/underscore, цифры после первой буквы).
    Значения приводятся к ``str`` через ``str()``. ``None`` заменяется на
    пустую строку.

    Args:
        template: шаблон с плейсхолдерами.
        **vars: переменные для подстановки.

    Returns:
        Шаблон с подставленными значениями.

    Raises:
        PromptUnresolvedVarError: если в шаблоне остались неразрешённые
            ``{{...}}`` после подстановки (включая имена, не переданные
            в ``**vars``).
    """
    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in vars:
            return match.group(0)
        value = vars[key]
        return "" if value is None else str(value)

    rendered = _PLACEHOLDER_RE.sub(repl, template)

    remaining = _UNRESOLVED_ANY_RE.findall(rendered)
    if remaining:
        names = sorted({p.strip("{}").strip() for p in remaining})
        raise PromptUnresolvedVarError(
            f"В шаблоне остались неразрешённые плейсхолдеры: {names}",
            unresolved=names,
            template=template,
        )
    return rendered
