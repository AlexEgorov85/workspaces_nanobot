"""Операция ``append_history_notice``: записать служебную заметку в историю.

Служебная, ``runtime-only``. Permission: ``data:append_history_notice``.

Заметка о сжатии контекста — это строка диалога, а не ответ на задачу: у неё
нет ``reply_to`` и сразу ``status='completed'``. Поэтому это отдельная
операция, а не ``append_assistant_message``: тот создаёт плейсхолдер, который
обязан закрыть ``finalize_turn``, и незакрытый плейсхолдер остаётся висеть в
``processing`` навсегда.

Имя таблицы — только у платформы. Раньше сервис агента писал эту строку
напрямую по DSN и имени таблицы из своего конфига, из-за чего при тестовом
профиле заметка уходила в БОЕВУЮ ``agent_conversation_messages``: оверлей
профиля объявлен в ``mcp-platform/platform.json`` и ещё в конфиге агента, и
сервис видел только второй. Побочный эффект был тихим — запись проходила, но
не туда.
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
        "chat_id": {
            "type": "string",
            "description": "Идентификатор чата, в который попадёт заметка.",
        },
        "text": {
            "type": "string",
            "description": "Текст заметки, видимый пользователю в истории диалога.",
        },
        "metadata": {
            "type": "object",
            "description": "Машиночитаемая часть: kind и сводка отчёта о сжатии.",
        },
        "media": {
            "type": "array",
            "items": {},
            "description": "Вложения строки; по умолчанию пустой список.",
        },
        "buttons": {
            "type": "array",
            "items": {},
            "description": "Кнопки строки; по умолчанию пустой список.",
        },
    },
    "required": ["chat_id", "text"],
}


def create_tool(registry_container: ToolContainer) -> ToolDefinition:
    service: DataService = registry_container.get("data")

    def handle_append_history_notice(
        ctx: ToolExecutionContext,
        chat_id: str,
        text: str,
        metadata: dict | None = None,
        media: list | None = None,
        buttons: list | None = None,
    ) -> str:
        """Записать служебную заметку в историю диалога."""
        result = service.append_history_notice(
            chat_id=chat_id,
            text=text,
            metadata=metadata,
            media=media,
            buttons=buttons,
            audience=AUDIENCE_RUNTIME,
        )
        return json.dumps({"status": "ok", **result}, ensure_ascii=False)

    description = (
        "Записать служебную заметку в историю диалога (например, отметку о "
        "сжатии контекста). Служебная операция — вызывается сервисом сжатия, "
        "не моделью. Строка сразу в состоянии completed и не привязана к "
        "задаче."
    )

    return ToolDefinition(
        name="append_history_notice",
        description=description,
        handler=handle_append_history_notice,
        category="data",
        tags=("queue", "infrastructure", "runtime-only"),
        permissions=("data:append_history_notice",),
        input_schema=INPUT_SCHEMA,
    )
