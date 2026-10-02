"""Операция ``release_claimed_tasks``: вернуть захваты в очередь при остановке.

Служебная, ``runtime-only``. Permission: ``data:release_claimed_tasks``.

Вызывается один раз — при остановке worker'а, с перечнем задач, которые он
удерживал. Одна операция, а не цикл вызовов по одной задаче: между вызовами
процесс можно убить, и тогда часть задач осталась бы в ``processing`` до
таймаута, а их assistant-заглушки — до следующего захвата.

Счётчик попыток не растёт: задача не провалилась, её не успели обработать.

Возвращает, сколько задач вернулось в ``pending`` и сколько заглушек
удалено, чтобы вызывающий знал фактический результат, а не рассчитывал его
по своему списку.

Имя таблицы — из ``platform.json`` (``data.task_table``).
"""

from __future__ import annotations

import json

from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.execution.context import ToolExecutionContext
from libs.enterprise_common.registry import ToolDefinition
from servers.enterprise.capabilities.data.service.main import (
    AUDIENCE_RUNTIME,
    DataService,
)

INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "task_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Идентификаторы задач, которые воркер держит захваченными.",
        },
    },
    "required": ["task_ids"],
}


def create_tool(registry_container: ToolContainer) -> ToolDefinition:
    service: DataService = registry_container.get("data")

    def handle_release_claimed_tasks(
        ctx: ToolExecutionContext,
        task_ids: list[str],
    ) -> str:
        """Вернуть захваченные задачи в очередь."""
        result = service.release_claimed_tasks(
            task_ids=task_ids,
            audience=AUDIENCE_RUNTIME,
        )
        return json.dumps({"status": "ok", **result}, ensure_ascii=False)

    description = (
        "Вернуть незавершённые задачи в очередь при остановке worker'а: статус "
        "processing сменяется на pending, assistant-заглушки удаляются. "
        "Служебная операция — вызывается каналом при остановке, не моделью."
    )

    return ToolDefinition(
        name="release_claimed_tasks",
        description=description,
        handler=handle_release_claimed_tasks,
        category="data",
        tags=("queue", "infrastructure", "runtime-only"),
        permissions=("data:release_claimed_tasks",),
        input_schema=INPUT_SCHEMA,
    )
