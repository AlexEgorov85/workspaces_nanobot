"""Операция ``merge_tool_delivery``: дописать промежуточную доставку в ответ.

Служебная, ``runtime-only``. Permission: ``data:merge_tool_delivery``.

Накопление содержимого, слияние ``media`` без дублей и обновление
``metadata`` — это read-modify-write по строке ответа, и оно обязано быть
одним вызовом. Два конкурирующих вызова прочитали бы одно и то же старое
значение, и правка одного из них потерялась бы молча: обе версии выглядят
удачными.

``status`` намеренно не меняется — ответ ещё ``processing``; закрывает его
``finalize_turn``.

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

# Поля с дефолтом None объявлены как список типов вместе с null:
# обработчик это значение принимает, и сериализация вызывающей стороны
# (json) присылает именно null, а не отсутствие поля. Узкий тип здесь
# отвергал бы законный вызов на проводе, до конвейера и без следа в
# журнале. Страж: tests/test_operation_schema_permissiveness.py.
INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "assistant_msg_id": {
            "type": "string",
            "description": "Идентификатор assistant-ответа, который дополняется.",
        },
        "content": {
            "type": "string",
            "description": (
                "Текст для дописывания. Пустая строка означает «взять уже "
                "накопленное», а не «очистить ответ»."
            ),
        },
        "metadata_patch": {
            "type": ["object", "null"],
            "description": "Поля metadata, которые надо записать поверх текущих.",
        },
        "buttons": {
            "type": ["array", "null"],
            "description": "Кнопки ответа; перезаписывают прежние.",
        },
        "media": {
            "type": ["array", "null"],
            "description": "Вложения к добавлению; сливаются с имеющимися без дублей.",
        },
    },
    "required": ["assistant_msg_id"],
}


def create_tool(registry_container: ToolContainer) -> ToolDefinition:
    service: DataService = registry_container.get("data")

    def handle_merge_tool_delivery(
        ctx: ToolExecutionContext,
        assistant_msg_id: str,
        content: str = "",
        metadata_patch: dict | None = None,
        buttons: list | None = None,
        media: list | None = None,
    ) -> str:
        """Дописать промежуточную доставку в ответ."""
        result = service.merge_tool_delivery(
            assistant_msg_id=assistant_msg_id,
            content=content,
            metadata_patch=metadata_patch,
            buttons=buttons,
            media=media,
            audience=AUDIENCE_RUNTIME,
        )
        return json.dumps({"status": "ok", **result}, ensure_ascii=False)

    description = (
        "Дописать текст, вложения и metadata в ещё не завершённый ответ "
        "ассистента, не меняя его статус. Служебная операция — вызывается "
        "каналом при доставке результата tool'а, не моделью."
    )

    return ToolDefinition(
        name="data.merge_tool_delivery",
        description=description,
        handler=handle_merge_tool_delivery,
        capability="data",
        tags=("queue", "infrastructure", "runtime-only"),
        permissions=("data:merge_tool_delivery",),
        input_schema=INPUT_SCHEMA,
    )
