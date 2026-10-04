"""Операция ``update_task_status``: смена статуса задачи и её содержимого.

Служебная, ``runtime-only``: очередь обслуживает канал агента, а не модель.
Значит, и ``permission`` своя — ``data:update_task_status``.

Операция закрывает дыру, которой не было у удалённой в п. 2.18 версии: чтение
и запись ``metadata`` происходят **внутри одного задания**. Счётчик ретраев
живёт в этой колонке, и пара вызовов «прочитать → посчитать → записать» из
двух конкурирующих обработчиков записала бы одинаковый ``retry_count`` — задача
получила бы бесконечные повторы, и это выглядело бы как «задача никак не
обрабатывается», а не как гонка.

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

# Поля с дефолтом None объявлены как список типов вместе с null:
# обработчик это значение принимает, и сериализация вызывающей стороны
# (json) присылает именно null, а не отсутствие поля. Узкий тип здесь
# отвергал бы законный вызов на проводе, до конвейера и без следа в
# журнале. Страж: tests/test_operation_schema_permissiveness.py.
INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string", "description": "Идентификатор задачи, обязателен."},
        "status": {
            "type": "string",
            "description": (
                "Статус: pending, processing, error, failed, cancelled или "
                "completed. При заданном error игнорируется — его выводит "
                "сервер по счётчику ретраев."
            ),
        },
        "role": {
            "type": ["string", "null"],
            "description": (
                "Ограничить обновление ролью строки (user или assistant). "
                "Пусто — любая роль."
            ),
        },
        "content": {
            "type": ["string", "null"],
            "description": "Текст сообщения. Не задан — колонка не трогается.",
        },
        "media": {
            "type": ["array", "null"],
            "items": {},
            "description": "Вложения. Не заданы — колонка не трогается.",
        },
        "metadata_patch": {
            "type": ["object", "null"],
            "description": (
                "Поля, которые нужно дописать в metadata. Существующие ключи "
                "не сохраняются, если не перечислены здесь."
            ),
        },
        "error": {
            "type": ["string", "null"],
            "description": (
                "Текст ошибки. При заданном значении счётчик ретраев "
                "увеличивается, а статус выводится сервером; max_stuck_retries "
                "становится обязательным."
            ),
        },
        "max_stuck_retries": {
            "type": ["integer", "null"],
            "description": (
                "Сколько попыток до терминального failed. Обязателен вместе "
                "с error."
            ),
        },
    },
    "required": ["task_id", "status"],
}


def create_tool(registry_container: ToolContainer) -> ToolDefinition:
    service: DataService = registry_container.get("data")

    def handle_update_task_status(
        ctx: ToolExecutionContext,
        task_id: str,
        status: str,
        role: str | None = None,
        content: str | None = None,
        media: list[Any] | None = None,
        metadata_patch: dict[str, Any] | None = None,
        error: str | None = None,
        max_stuck_retries: int | None = None,
    ) -> str:
        """Сменить статус задачи.

        ``updated=false`` означает «такой задачи нет», а не «запись
        состоялась»: вызывающий обязан различать их, иначе потерянная задача
        будет считаться обработанной.
        """
        result = service.update_task_status(
            task_id,
            status=status,
            role=role,
            content=content,
            media=media,
            metadata_patch=metadata_patch,
            error=error,
            max_stuck_retries=max_stuck_retries,
            audience=AUDIENCE_RUNTIME,
        )
        return json.dumps({"status": "ok", **result}, ensure_ascii=False, default=str)

    description = (
        "Сменить статус задачи очереди, при необходимости записав текст, "
        "вложения и поля metadata. Служебная операция — вызывается каналом, "
        "не моделью. Возвращает итоговый статус и счётчик повторов."
    )

    return ToolDefinition(
        name="update_task_status",
        description=description,
        handler=handle_update_task_status,
        category="data",
        tags=("queue", "infrastructure", "runtime-only"),
        permissions=("data:update_task_status",),
        input_schema=INPUT_SCHEMA,
    )
