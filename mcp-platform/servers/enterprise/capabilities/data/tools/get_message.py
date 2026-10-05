"""Операция ``get_message``: прочитать одну строку очереди.

Служебная, ``runtime-only``. Permission: ``data:get_message``.

Нужна каналу в двух местах: перепроверка статуса сразу после захвата (AW мог
пометить задачу отменённой между ``SELECT`` подзапроса и ``UPDATE`` захвата)
и обратный поиск user-сообщения по ``reply_to`` ответа.

Отдаёт фиксированный набор полей строки, а не «колонку по запросу»: операция,
принимающая имя колонки, — это уже SQL навылет, и тогда граница
«платформа владеет данными» перестаёт что-то значить.

``None`` в ответе означает «строки нет» — вызывающий обязан отличать это от
«прочиталась», потому что отсутствие строки и её чтение ведут себя по-разному.
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
        "task_id": {
            "type": "string",
            "description": "Идентификатор строки очереди.",
        },
    },
    "required": ["task_id"],
}


def create_tool(registry_container: ToolContainer) -> ToolDefinition:
    service: DataService = registry_container.get("data")

    def handle_get_message(ctx: ToolExecutionContext, task_id: str) -> str:
        """Прочитать строку очереди."""
        row = service.get_message(task_id=task_id, audience=AUDIENCE_RUNTIME)
        return json.dumps(
            {"status": "ok", "message": row, "found": row is not None},
            ensure_ascii=False,
        )

    description = (
        "Прочитать одну строку очереди: id, роль, статус, reply_to и chat_id. "
        "Служебная операция — вызывается каналом при разборе оборота, не "
        "моделью."
    )

    return ToolDefinition(
        name="data.get_message",
        description=description,
        handler=handle_get_message,
        capability="data",
        tags=("queue", "infrastructure", "runtime-only"),
        permissions=("data:get_message",),
        input_schema=INPUT_SCHEMA,
    )
