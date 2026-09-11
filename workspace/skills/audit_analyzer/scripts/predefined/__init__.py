"""Predefined SQL-режим навыка ``audit_analyzer``.

Public API:

* :func:`predefined.run` — выполнить скрипт через generic DuckDB-сервис.
* :func:`predefined.list_scripts` — метаданные всех скриптов из DB.
* :func:`predefined.list_available` — список имён через запятую.
* :func:`predefined.load_script` / :func:`predefined.load_all` — DB-only
  чтение из ``public.agent_predefined_scripts`` через DuckDB-PG-снимок.
* :class:`predefined.ScriptDefinition` — описание одного скрипта.
* :class:`predefined.ParamDefinition` — описание параметра.
* :class:`predefined.ParameterValidator` — валидация параметров.
* :class:`predefined.DynamicQueryBuilder` — сборка SQL из шаблона.

Канонический источник SQL — ``public.agent_predefined_scripts`` (PostgreSQL).
Python ``REGISTRY`` удалён в Phase 7.
"""

from __future__ import annotations

from workspace.skills.audit_analyzer.scripts.predefined.builder import (
    BuildError,
    DynamicQueryBuilder,
)
from workspace.skills.audit_analyzer.scripts.predefined.db_loader import (
    DBScriptProvider,
    load_all,
    load_script,
)
from workspace.skills.audit_analyzer.scripts.predefined.mode import (
    DuckDBServiceProtocol,
    list_available,
    list_scripts,
    run,
)
from workspace.skills.audit_analyzer.scripts.predefined.models import (
    ParamDefinition,
    ScriptDefinition,
)
from workspace.skills.audit_analyzer.scripts.predefined.validator import (
    ParameterValidator,
    ValidationError,
)


__all__ = [
    "BuildError",
    "DBScriptProvider",
    "DynamicQueryBuilder",
    "DuckDBServiceProtocol",
    "ParamDefinition",
    "ParameterValidator",
    "ScriptDefinition",
    "ValidationError",
    "list_available",
    "list_scripts",
    "load_all",
    "load_script",
    "run",
]