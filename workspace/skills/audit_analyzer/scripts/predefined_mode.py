"""Режим: predefined — выполнение готовых SQL-шаблонов по имени скрипта.

Использует skill-side модуль ``workspace.skills.audit_analyzer.predefined``
(модели, валидацию, builder). Источник SQL — ``predefined/scripts.py``
(внутри skill'а), а не таблица ``public.agent_predefined_scripts``.

Совместим с любым backend, удовлетворяющим ``DuckDBServiceProtocol``
(например, ``DuckDbCacheStore``).
"""

from __future__ import annotations

from typing import Any

from workspace.skills.audit_analyzer.predefined import run as predefined_run


__all__ = ["run"]


def run(
    script_name: str,
    db: Any,
    params: dict[str, Any] | None = None,
    index_dir: str = "",
) -> dict:
    """Выполнить предопределённый SQL-скрипт.

    Pipeline:
      1. lookup ScriptDefinition по имени (``predefined/scripts.py``);
      2. resolve/merge параметров (``ParameterValidator``);
      3. сборка SQL (``DynamicQueryBuilder``: дефолты, типы, {% if %},
         position placeholders);
      4. выполнение SQL через ``CacheProvider.query_sql`` (rows — список
         dict по именам колонок).

    Возвращает dict в формате ``{"mode", "status", "data": {...}}``,
    совместимом с ``scripts.output.prepare_output``.
    """
    return predefined_run(script_name, db, params)
