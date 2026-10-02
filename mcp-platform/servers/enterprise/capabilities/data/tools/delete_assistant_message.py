"""Операция ``delete_assistant_message``: удалить запись ответа.

Служебная, ``runtime-only``. Permission: ``data:delete_assistant_message``.

Используется при откате: задача уходит на повтор, а пользователь не должен
видеть ошибочный статус ответа, который уже не будет доставлен.

Роль ограничена в ``WHERE``, а не в ``SET``: вызывающий не может снести
запись пользователя, назвав её своей.

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
        "task_id": {
            "type": "string",
            "description": "Идентификатор удаляемой записи, обязателен.",
        },
        "role": {
            "type": "string",
            "description": "Роль строки: assistant (по умолчанию) или user.",
        },
    },
    "required": ["task_id"],
}


def create_tool(registry_container: ToolContainer) -> ToolDefinition:
    service: DataService = registry_container.get("data")

    def handle_delete_assistant_message(
        ctx: ToolExecutionContext, task_id: str, role: str = "assistant"
    ) -> str:
        """Удалить запись и сообщить, была ли она."""
        deleted = service.delete_assistant_message(
            task_id, role=role, audience=AUDIENCE_RUNTIME
        )
        return json.dumps({"status": "ok", "deleted": deleted}, ensure_ascii=False)

    description = (
        "Удалить запись сообщения по идентификатору. Служебная операция — "
        "вызывается каналом при откате задачи, не моделью. Возвращает признак "
        "того, была ли запись."
    )

    return ToolDefinition(
        name="delete_assistant_message",
        description=description,
        handler=handle_delete_assistant_message,
        category="data",
        tags=("queue", "infrastructure", "runtime-only"),
        permissions=("data:delete_assistant_message",),
        input_schema=INPUT_SCHEMA,
    )
