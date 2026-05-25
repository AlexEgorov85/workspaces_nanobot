"""Предопределённые SQL-скрипты — обёртка над SCRIPTS_REGISTRY + DynamicQueryBuilder."""

from typing import Any, Dict, List, Optional, Tuple

from .scripts_registry import (
    SCRIPTS_REGISTRY,
    DynamicQueryBuilder,
    ScriptDefinition,
)


def build_sql(
    script: ScriptDefinition,
    params: Dict[str, Any],
) -> Tuple[str, List[Any]]:
    """Подставить параметры, срендерить шаблон, вернуть (sql, values_for_asyncpg)."""
    return DynamicQueryBuilder.build(script, params)


def get_script_by_name(name: str) -> Optional[ScriptDefinition]:
    """Получить ScriptDefinition по имени."""
    return SCRIPTS_REGISTRY.get(name)


def list_all_scripts() -> List[Dict[str, Any]]:
    """Список всех скриптов для отображения."""
    return [
        {
            "name": s.name,
            "description": s.description,
            "parameters": list(s.parameters.keys()),
        }
        for s in SCRIPTS_REGISTRY.values()
    ]


def list_available() -> str:
    """Человекочитаемый список имён скриптов."""
    return ", ".join(SCRIPTS_REGISTRY.keys())
