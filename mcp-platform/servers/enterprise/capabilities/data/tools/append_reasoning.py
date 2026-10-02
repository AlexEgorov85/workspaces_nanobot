"""Операция ``append_reasoning``: дописать дельту рассуждений к ответу.

Служебная, ``runtime-only``. Permission: ``data:append_reasoning``.

Стрим присылает рассуждение по кускам, и каждый кусок должен **дописаться** к
уже накопленному. Схема «прочитать, склеить, записать» для этого не годится:
чтение и запись в одном вызове не делают их атомарными относительно другой
корутины, которая дописывает своё. В канале эту гонку прикрывал
``asyncio.Lock`` — то есть отсутствие атомарности признавалось кодом, просто
молча.

Здесь конкатенация выполняется в SQL поверх уже обновлённого значения, поэтому
два параллельных сброса не могут потерять кусок друг друга.

Пустая дельта — не вызов: она ничего не добавляет, и поход в базу из-за
пустой строки создавал бы запись без смысла.
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
        "assistant_msg_id": {
            "type": "string",
            "description": "Идентификатор assistant-ответа, к которому дописывается рассуждение.",
        },
        "delta": {
            "type": "string",
            "description": "Новый кусок рассуждения; дописывается к имеющемуся.",
        },
    },
    "required": ["assistant_msg_id", "delta"],
}


def create_tool(registry_container: ToolContainer) -> ToolDefinition:
    service: DataService = registry_container.get("data")

    def handle_append_reasoning(
        ctx: ToolExecutionContext,
        assistant_msg_id: str,
        delta: str,
    ) -> str:
        """Дописать рассуждение к ответу."""
        result = service.append_reasoning(
            assistant_msg_id=assistant_msg_id,
            delta=delta,
            audience=AUDIENCE_RUNTIME,
        )
        return json.dumps({"status": "ok", **result}, ensure_ascii=False)

    description = (
        "Дописать кусок рассуждений к metadata ответа ассистента. Служебная "
        "операция — вызывается каналом при потоковой доставке, не моделью."
    )

    return ToolDefinition(
        name="append_reasoning",
        description=description,
        handler=handle_append_reasoning,
        category="data",
        tags=("queue", "infrastructure", "runtime-only"),
        permissions=("data:append_reasoning",),
        input_schema=INPUT_SCHEMA,
    )
