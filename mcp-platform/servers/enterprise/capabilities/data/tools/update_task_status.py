"""Операция ``update_task_status`` — возврат задачи в очередь или снятие её.

Тот же профиль ``runtime``, что и у захвата: операция меняет состояние задачи,
а значит принадлежит воркеру, а не модели.
"""

from __future__ import annotations

import json

from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.registry import ToolDefinition
from servers.enterprise.capabilities.data.service.main import (
    AUDIENCE_RUNTIME,
    DataService,
)

#: Контейнер подставляется загрузчиком при регистрации операции.
container: ToolContainer | None = None


def handle_update_task_status(
    task_table: str,
    task_id: str,
    status: str,
    error: str | None = None,
    retry_after_sec: int | None = None,
) -> str:
    """Сменить статус задачи. ``false`` означает, что такой задачи нет."""
    if container is None:  # pragma: no cover - защита от неверной сборки
        raise RuntimeError("контейнер не инициализирован")
    service: DataService = container.get("data")
    updated = service.update_task_status(
        task_table=task_table,
        task_id=task_id,
        status=status,
        error=error,
        retry_after_sec=retry_after_sec,
        audience=AUDIENCE_RUNTIME,
    )
    return json.dumps({"updated": updated})


def create_tool(registry_container: ToolContainer) -> ToolDefinition:
    global container
    container = registry_container
    return ToolDefinition(
        name="update_task_status",
        description=(
            "Сменить статус задачи в очереди. Доступно только рантайму агента, "
            "не модели."
        ),
        handler=handle_update_task_status,
        category="data",
        tags=("queue", "runtime-only"),
        permissions=("queue:write",),
    )
