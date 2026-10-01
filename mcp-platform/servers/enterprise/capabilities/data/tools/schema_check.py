"""Операция ``schema_check`` — проверка наличия обязательных таблиц.

Один запрос к ``information_schema`` вместо попытки открыть каждую таблицу:
«таблицы нет» и «соединения нет» должны различаться, и различать их дешевле
по одному списку.
"""

from __future__ import annotations

import json
from typing import Any

from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.registry import ToolDefinition
from servers.enterprise.capabilities.data.service.main import (
    AUDIENCE_MODEL,
    DataService,
)

#: Сервис замыкается обработчиком, а не лежит в модульной переменной: значение
#: глобальной зависело бы от порядка регистрации операций.
def create_tool(registry_container: ToolContainer) -> ToolDefinition:
    service: DataService = registry_container.get("data")

    def handle_schema_check(expected: list[str] | None = None) -> str:
        """Проверить наличие таблиц и вернуть отчёт с недостающими."""
        report = service.schema_check(
            expected=tuple(expected) if expected else None,
            audience=AUDIENCE_MODEL,
        )
        return json.dumps(report, ensure_ascii=False)
    return ToolDefinition(
        name="schema_check",
        description=(
            "Проверить наличие обязательных таблиц в PostgreSQL. Возвращает "
            "список недостающих таблиц и признак ok."
        ),
        handler=handle_schema_check,
        category="data",
        tags=("infrastructure", "diagnostics"),
    )
