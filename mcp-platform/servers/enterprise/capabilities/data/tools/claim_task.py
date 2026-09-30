"""Операция ``claim_task`` — атомарный захват одной задачи из очереди.

Обслуживает канал агента, а не модель: право на операцию есть только у профиля
``runtime``. Модель, захватившая задачу, увела бы её у живого воркера.
"""

from __future__ import annotations

import json
from typing import Any

from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.registry import ToolDefinition
from servers.enterprise.capabilities.data.service.main import (
    AUDIENCE_RUNTIME,
    DataService,
)

#: Контейнер подставляется загрузчиком при регистрации операции.
container: ToolContainer | None = None


def handle_claim_task(task_table: str, worker_id: str) -> str:
    """Захватить одну задачу. Пустая очередь — это ``{"task": null}``."""
    if container is None:  # pragma: no cover - защита от неверной сборки
        raise RuntimeError("контейнер не инициализирован")
    service: DataService = container.get("data")
    task = service.claim_task(
        task_table=task_table,
        worker_id=worker_id,
        audience=AUDIENCE_RUNTIME,
    )
    if task is None:
        return json.dumps({"task": None})
    return json.dumps(
        {
            "task": {
                "id": task["id"],
                "payload": task["payload"],
                "session_id": task["session_id"],
                "created_at": str(task["created_at"]),
            }
        },
        ensure_ascii=False,
    )


def create_tool(registry_container: ToolContainer) -> ToolDefinition:
    global container
    container = registry_container
    return ToolDefinition(
        name="claim_task",
        description=(
            "Атомарно захватить одну задачу из очереди. Доступно только рантайму "
            "агента, не модели."
        ),
        handler=handle_claim_task,
        category="data",
        tags=("queue", "runtime-only"),
        permissions=("queue:claim",),
    )
