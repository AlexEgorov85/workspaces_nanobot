"""Операция ``fail_task``: пометить оборот ошибочным, увеличив счётчик попыток.

Служебная, ``runtime-only``. Permission: ``data:fail_task``.

Одна транзакция правит две строки: user-задачу и assistant-ответ. Это
неоптимизация, а требование — ``metadata.retry_count`` читается, увеличивается
и записывается обратно вместе со сменой статуса. Разрыв между вызовами дал бы
задачу с увеличенным счётчиком и старым статусом, и она исчерпала бы лимит
повторов, ни разу не будучи обработана.

Ветвление по лимиту повторов тоже атомарно:

* попытки ещё есть → user ``error``, assistant-заглушка удаляется (пользователь
  не должен видеть ошибочный статус до следующей обработки);
* попытки исчерпаны → user ``failed``, на assistant пишется текст ошибки.

Возвращает итоговый статус и счётчик — вызывающий логирует фактический
результат, а не пересчитывает его у себя.
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
            "type": ["string", "null"],
            "description": (
                "Идентификатор assistant-ответа. Может отсутствовать: "
                "ошибка может случиться до создания заглушки."
            ),
        },
        "reason": {
            "type": "string",
            "description": "Краткая причина; попадает в ответ и в metadata.",
        },
        "max_stuck_retries": {
            "type": "integer",
            "description": "Сколько попыток до терминального failed, минимум 1.",
        },
    },
    "required": ["user_msg_id", "reason", "max_stuck_retries"],
}


def create_tool(registry_container: ToolContainer) -> ToolDefinition:
    service: DataService = registry_container.get("data")

    def handle_fail_task(
        ctx: ToolExecutionContext,
        user_msg_id: str,
        reason: str,
        max_stuck_retries: int,
        assistant_msg_id: str | None = None,
    ) -> str:
        """Пометить оборот ошибочным."""
        result = service.fail_task(
            user_msg_id=user_msg_id,
            assistant_msg_id=assistant_msg_id,
            reason=reason,
            max_stuck_retries=max_stuck_retries,
            audience=AUDIENCE_RUNTIME,
        )
        return json.dumps({"status": "ok", **result}, ensure_ascii=False)

    description = (
        "Пометить задачу ошибочной, увеличив счётчик попыток. Пока попытки "
        "есть — статус error и ответ удаляется, при исчерпании — failed с "
        "текстом ошибки. Служебная операция — вызывается каналом при ошибке "
        "оборота, не моделью."
    )

    return ToolDefinition(
        name="data.fail_task",
        description=description,
        handler=handle_fail_task,
        capability="data",
        tags=("queue", "infrastructure", "runtime-only"),
        permissions=("data:fail_task",),
        input_schema=INPUT_SCHEMA,
    )
