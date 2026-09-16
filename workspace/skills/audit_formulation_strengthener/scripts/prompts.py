"""Загрузка и рендеринг system-промптов skill'а.

Промпты лежат в ``prompts/*.md`` рядом со ``scripts/`` (один уровень вверх).
Шаблон подставляется через ``str.replace`` (плейсхолдеры вида ``{{NAME}}``).

Не используем jinja2 — намеренно держим минимум зависимостей.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_SKILL_ROOT = Path(__file__).resolve().parent.parent
_PROMPTS_DIR = _SKILL_ROOT / "prompts"

if str(_SKILL_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT / "scripts"))


__all__ = ["load_prompt", "render_prompt"]


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


def render_prompt(template: str, variables: dict[str, Any]) -> str:
    """Подставить переменные в шаблон.

    Плейсхолдеры: ``{{NAME}}`` (регистрозависимые). Значения приводятся
    к ``str`` через ``str()``. ``None`` заменяется на пустую строку.

    Args:
        template: шаблон с плейсхолдерами.
        variables: dict с переменными.

    Returns:
        Шаблон с подставленными значениями.
    """
    out = template
    for key, value in variables.items():
        placeholder = "{{" + key + "}}"
        if value is None:
            replacement = ""
        else:
            replacement = str(value)
        out = out.replace(placeholder, replacement)
    return out
