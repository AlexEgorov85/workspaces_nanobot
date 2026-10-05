"""Операция ``patch_message_metadata``: дописать поля ``metadata`` строки.

Служебная, ``runtime-only``. Permission: ``data:patch_message_metadata``.

Служит для потоковых вещей — дельт рассуждений, окна контекста, — которые
пишутся часто и по одной. Чтение и запись идут в одном задании: дельта может
прийти в тот же момент, что и финализация ответа, а две транзакции на одном
идентификаторе — это либо потерянная дельта, либо затертый ответ.

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
        "task_id": {"type": "string", "description": "Идентификатор строки, обязателен."},
        "patch": {
            "type": "object",
            "description": (
                "Поля для дописывания. Вложенные объекты мержатся в глубину на "
                "один уровень; ключ со значением null удаляет поле."
            ),
        },
        "role": {
            "type": ["string", "null"],
            "description": "Ограничить правку ролью строки. Пусто — любая.",
        },
    },
    "required": ["task_id", "patch"],
}


def create_tool(registry_container: ToolContainer) -> ToolDefinition:
    service: DataService = registry_container.get("data")

    def handle_patch_message_metadata(
        ctx: ToolExecutionContext,
        task_id: str,
        patch: dict[str, Any],
        role: str | None = None,
    ) -> str:
        """Дописать поля metadata и вернуть итоговый объект."""
        result = service.patch_message_metadata(
            task_id, patch, role=role, audience=AUDIENCE_RUNTIME
        )
        return json.dumps(
            {"status": "ok", **result}, ensure_ascii=False, default=str
        )

    description = (
        "Дописать поля metadata сообщения и вернуть итоговый объект. "
        "Служебная операция для потоковых обновлений хода ответа — вызывается "
        "каналом, не моделью."
    )

    return ToolDefinition(
        name="data.patch_message_metadata",
        description=description,
        handler=handle_patch_message_metadata,
        capability="data",
        tags=("queue", "infrastructure", "runtime-only"),
        permissions=("data:patch_message_metadata",),
        input_schema=INPUT_SCHEMA,
    )
