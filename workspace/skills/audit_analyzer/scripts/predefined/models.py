"""Типизированные модели для predefined SQL-скриптов.

Перенесено из ``workspace/skills/audit_analyzer/scripts/scripts_registry.py``
(эталон a606fe0). Типизация и семантика сохранены — то же поведение
``ParamDefinition``/``ScriptDefinition`` и ``DynamicQueryBuilder``.

Skill-internal: используется только ``audit_analyzer/predefined/*``. Никаких
domain-знаний в core. Реестр SQL-скриптов хранится в этом skill'е
(``scripts.py``), а не в PostgreSQL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ParamType = Literal[
    "like", "exact", "limit", "number", "date", "enum", "boolean"
]


@dataclass(frozen=True)
class ParamDefinition:
    """Определение одного параметра скрипта.

    Attributes:
        type: Тип параметра. Управляет форматированием значения перед
              подстановкой в SQL:
              - ``like``    → оборачивает в %value% (ILIKE поиск)
              - ``exact``   → точное значение (без изменений)
              - ``limit``   → преобразуется в ``max_rows`` для LIMIT
              - ``number``  → ``int(value)``
              - ``date``    → строка даты (без изменений)
              - ``enum``    → строка из фиксированного набора
              - ``boolean`` → ``bool(value)``, используется для ``{% if %}``
        required: True если параметр обязателен.
        default: Значение по умолчанию (если не передан).
        description: Человекочитаемое описание.
        validation: Опциональные правила (для enum и vector-резолва).
    """

    type: ParamType = "exact"
    required: bool = False
    default: Any = None
    description: str = ""
    validation: dict[str, Any] | None = None


@dataclass(frozen=True)
class ScriptDefinition:
    """Полное описание предопределённого SQL-скрипта.

    Attributes:
        name: Уникальное имя скрипта.
        description: Краткое описание для меню.
        sql_template: SQL-шаблон с ``:param_name`` плейсхолдерами и
            ``{% if param %}...{% endif %}`` Jinja2-подобными блоками.
        parameters: Словарь ``{имя: ParamDefinition}``.
        max_rows_default: Лимит строк по умолчанию.
        returns: Что возвращает скрипт (для документации).
        long_description: Подробное описание для LLM/Agent.
    """

    name: str
    description: str
    sql_template: str
    parameters: dict[str, ParamDefinition] = field(default_factory=dict)
    max_rows_default: int = 1000
    returns: str = ""
    long_description: str = ""


__all__ = [
    "ParamDefinition",
    "ParamType",
    "ScriptDefinition",
]
