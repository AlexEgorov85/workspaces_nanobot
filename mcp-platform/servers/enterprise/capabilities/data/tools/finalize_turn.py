"""Операция ``finalize_turn``: закрыть оборот — записать ответ и снять задачу.

Служебная, ``runtime-only``. Permission: ``data:finalize_turn``.

Две строки меняются в одной транзакции по назначению: закрыть задачу можно
только вместе с её ответом. Разрыв оставил бы ``completed`` на задаче без
ответа, а это читается как «обработано» — то есть молча и неверно.

Проверка отмены пользователем сделана частью той же транзакции. Отдельное
чтение статуса перед ней оставляло окно, в котором отмена успевала прийти, а
ответ всё равно записывался: между ``SELECT`` и ``UPDATE`` проходит вся
транзакция, а при длинном ответе — ещё и время сборки текста.

Возвращает ``outcome``:

* ``completed`` — ответ записан, обе строки закрыты;
* ``cancelled_drop`` — задачу отменили, заглушка удалена, а сама задача не
  тронута: её закрыл тот, кто отменил.

Вызывающий по ``outcome`` снимает или, наоборот, не снимает локальный клейм и
слот.

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
        "user_msg_id": {
            "type": "string",
            "description": "Идентификатор user-сообщения (строки задачи).",
        },
        "assistant_msg_id": {
            "type": "string",
            "description": "Идентификатор assistant-ответа, который закрывается.",
        },
        "content": {
            "type": "string",
            "description": (
                "Финальный текст ответа. Пустая строка означает «взять уже "
                "накопленное промежуточными вызовами», а не «очистить ответ»."
            ),
        },
        "metadata_patch": {
            "type": ["object", "null"],
            "description": "Поля metadata, которые надо записать поверх текущих.",
        },
        "buttons": {
            "type": ["array", "null"],
            "description": "Кнопки ответа.",
        },
        "media": {
            "type": ["array", "null"],
            "description": (
                "Вложения финального ответа. Заменяют прежние, а не сливаются: "
                "к моменту финализации перечень известен целиком."
            ),
        },
    },
    "required": ["user_msg_id", "assistant_msg_id"],
}


def create_tool(registry_container: ToolContainer) -> ToolDefinition:
    service: DataService = registry_container.get("data")

    def handle_finalize_turn(
        ctx: ToolExecutionContext,
        user_msg_id: str,
        assistant_msg_id: str,
        content: str = "",
        metadata_patch: dict | None = None,
        buttons: list | None = None,
        media: list | None = None,
    ) -> str:
        """Закрыть оборот."""
        result = service.finalize_turn(
            user_msg_id=user_msg_id,
            assistant_msg_id=assistant_msg_id,
            content=content,
            metadata_patch=metadata_patch,
            buttons=buttons,
            media=media,
            audience=AUDIENCE_RUNTIME,
        )
        return json.dumps({"status": "ok", **result}, ensure_ascii=False)

    description = (
        "Записать финальный ответ и перевести задачу из processing в "
        "completed одной транзакцией. Если задачу успели отменить — ответ не "
        "пишется, возвращается cancelled_drop. Служебная операция — "
        "вызывается каналом в конце оборота, не моделью."
    )

    return ToolDefinition(
        name="finalize_turn",
        description=description,
        handler=handle_finalize_turn,
        category="data",
        tags=("queue", "infrastructure", "runtime-only"),
        permissions=("data:finalize_turn",),
        input_schema=INPUT_SCHEMA,
    )
