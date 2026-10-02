"""Операция ``append_assistant_message``: создать assistant-заглушку.

Служебная, ``runtime-only``. Permission: ``data:append_assistant_message``.

Заглушка нужна, чтобы веб-клиент начал опрашивать ответ **до** конца
генерации. Как только агент закончит, ту же строку дополняет
``update_task_status(status='completed', content=…)``.

Связь ``reply_to`` ставится здесь, на сервере: собирать её на стороне канала
значило бы держать правило «ответ принадлежит задаче» в двух местах, и
возврат зависших задач искал бы заглушки по чужому правилу.

Имя таблицы — из ``platform.json`` (``data.task_table``).
"""

from __future__ import annotations

import json
from typing import Any

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
        "chat_id": {"type": "string", "description": "Идентификатор чата, обязателен."},
        "reply_to": {
            "type": "string",
            "description": "Идентификатор задачи пользователя, на которую отвечаем.",
        },
        "content": {
            "type": "string",
            "description": "Начальный текст. Обычно пустой.",
        },
        "media": {"type": ["array", "null"], "items": {}, "description": "Вложения."},
        "metadata": {
            "type": ["object", "null"],
            "description": "Служебные признаки сообщения.",
        },
    },
    "required": ["chat_id", "reply_to"],
}


def create_tool(registry_container: ToolContainer) -> ToolDefinition:
    service: DataService = registry_container.get("data")

    def handle_append_assistant_message(
        ctx: ToolExecutionContext,
        chat_id: str,
        reply_to: str,
        content: str = "",
        media: list[Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Вставить assistant-заглушку и вернуть её идентификатор."""
        assistant_msg_id = service.append_assistant_message(
            chat_id=chat_id,
            reply_to=reply_to,
            content=content,
            media=media,
            metadata=metadata,
            audience=AUDIENCE_RUNTIME,
        )
        return json.dumps(
            {"status": "ok", "assistant_msg_id": assistant_msg_id},
            ensure_ascii=False,
        )

    description = (
        "Создать заготовку ответа ассистента (статус processing) на задачу "
        "пользователя и вернуть её идентификатор. Служебная операция — "
        "вызывается каналом, не моделью."
    )

    return ToolDefinition(
        name="append_assistant_message",
        description=description,
        handler=handle_append_assistant_message,
        category="data",
        tags=("queue", "infrastructure", "runtime-only"),
        permissions=("data:append_assistant_message",),
        input_schema=INPUT_SCHEMA,
    )
