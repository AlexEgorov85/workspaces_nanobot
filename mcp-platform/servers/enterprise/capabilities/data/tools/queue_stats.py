"""Операция ``queue_stats``: сколько задач ждут обработки и повтора.

Служебная, ``runtime-only``. Permission: ``data:queue_stats``.

Служит только для строки активности в терминале воркера. Считаются лишь
user-строки: assistant-заглушки в этих числах не имеют смысла и завышали бы
очередь вдвое на каждой задаче в полёте.

Называть это «счётчиком» без уточнения опасно: число ждущих повтора растёт
и от нормальной работы (задача упала и будет повторена), и от залипшей. Пока
вызывающий печатает, а не решает, — расхождение безвредно.
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

INPUT_SCHEMA = {"type": "object", "properties": {}, "required": []}


def create_tool(registry_container: ToolContainer) -> ToolDefinition:
    service: DataService = registry_container.get("data")

    def handle_queue_stats(ctx: ToolExecutionContext) -> str:
        """Посчитать очередь."""
        stats = service.queue_stats(audience=AUDIENCE_RUNTIME)
        return json.dumps({"status": "ok", **stats}, ensure_ascii=False)

    description = (
        "Число задач в очереди: pending ждут обработки, error ждут повтора. "
        "Служебная операция — вызывается каналом для вывода активности, не "
        "моделью."
    )

    return ToolDefinition(
        name="queue_stats",
        description=description,
        handler=handle_queue_stats,
        category="data",
        tags=("queue", "infrastructure", "runtime-only"),
        permissions=("data:queue_stats",),
        input_schema=INPUT_SCHEMA,
    )
