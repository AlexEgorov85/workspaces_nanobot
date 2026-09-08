"""Predefined SQL-режим навыка ``audit_analyzer``.

Public API (см. ``references/predefined_scripts.md``):

* :func:`predefined.run` — выполнить скрипт через generic DuckDB-сервис.
* :func:`predefined.list_scripts` — метаданные всех 5 скриптов.
* :func:`predefined.list_available` — список имён через запятую.
* :class:`predefined.ScriptDefinition` — описание одного скрипта.
* :class:`predefined.ParamDefinition` — описание параметра.
* :class:`predefined.ParameterValidator` — валидация параметров.
* :class:`predefined.DynamicQueryBuilder` — сборка SQL из шаблона.

Реестр SQL хранится в ``predefined/scripts.py`` (Python-литералы),
а не в таблице ``public.agent_predefined_scripts`` (legacy migration оставлен
только для back-compat read).
"""

from __future__ import annotations

from workspace.skills.audit_analyzer.predefined.builder import (
    BuildError,
    DynamicQueryBuilder,
)
from workspace.skills.audit_analyzer.predefined.mode import (
    DuckDBServiceProtocol,
    list_available,
    list_scripts,
    run,
)
from workspace.skills.audit_analyzer.predefined.models import (
    ParamDefinition,
    ScriptDefinition,
)
from workspace.skills.audit_analyzer.predefined.scripts import (
    REGISTRY,
    get_script,
)
from workspace.skills.audit_analyzer.predefined.validator import (
    ParameterValidator,
    ValidationError,
)


__all__ = [
    "BuildError",
    "DynamicQueryBuilder",
    "DuckDBServiceProtocol",
    "ParamDefinition",
    "ParameterValidator",
    "REGISTRY",
    "ScriptDefinition",
    "ValidationError",
    "get_script",
    "list_available",
    "list_scripts",
    "run",
]
