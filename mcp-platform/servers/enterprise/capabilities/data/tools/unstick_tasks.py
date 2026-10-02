"""Операция ``unstick_tasks``: вернуть зависшие задачи в очередь.

Служебная, ``runtime-only``. Permission: ``data:unstick_tasks``.

Задача считается зависшей, если она в ``processing`` дольше таймаута. Дальше
либо ``retry_count`` не исчерпан и задача возвращается в ``pending`` (а её
assistant-заглушка удаляется — пользователь не должен видеть «отвечаю…» от
ответа, который не доставят), либо попытки исчерпаны и задача становится
терминально ``failed``.

Счётчик попыток ведётся здесь, а не у вызывающей стороны: он живёт в
``metadata``, и раздельные вызовы из двух разных воркеров записали бы
одинаковый счётчик — задача получила бы бесконечные повторы.

Возвращает идентификаторы тронутых задач: вызывающий по ним снимает локальное
состояние, иначе «забытые» слоты останутся занятыми и polling перестанет брать
сообщения.

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
        "processing_timeout_sec": {
            "type": "number",
            "description": "Сколько секунд задача может висеть в processing.",
        },
        "max_stuck_retries": {
            "type": "integer",
            "description": "Сколько попыток до терминального failed, минимум 1.",
        },
    },
    "required": ["processing_timeout_sec", "max_stuck_retries"],
}


def create_tool(registry_container: ToolContainer) -> ToolDefinition:
    service: DataService = registry_container.get("data")

    def handle_unstick_tasks(
        ctx: ToolExecutionContext,
        processing_timeout_sec: float,
        max_stuck_retries: int,
    ) -> str:
        """Освободить зависшие задачи и вернуть их идентификаторы."""
        recovered = service.unstick_tasks(
            processing_timeout_sec=processing_timeout_sec,
            max_stuck_retries=max_stuck_retries,
            audience=AUDIENCE_RUNTIME,
        )
        return json.dumps(
            {"status": "ok", "recovered": recovered}, ensure_ascii=False
        )

    description = (
        "Вернуть зависшие задачи очереди в обработку: те, что дольше таймаута "
        "остаются в статусе processing. Служебная операция — вызывается "
        "каналом по расписанию, не моделью."
    )

    return ToolDefinition(
        name="unstick_tasks",
        description=description,
        handler=handle_unstick_tasks,
        category="data",
        tags=("queue", "infrastructure", "runtime-only"),
        permissions=("data:unstick_tasks",),
        input_schema=INPUT_SCHEMA,
    )
