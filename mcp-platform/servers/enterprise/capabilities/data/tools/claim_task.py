"""Операция ``claim_task``: атомарный захват одной задачи очереди.

Служебная, ``runtime-only``: очередь обслуживает канал агента, а не модель.
Значит, и ``permission`` своя — ``data:claim_task``.

Возвращается одна задача в доменном виде либо ``None``, когда задач нет.
``None`` — не ошибка: пустая очередь это нормальное состояние, и оборачивать
его в исключение заставило бы канал отличать «нечего делать» от
«сломалось» по тексту ответа.

Имя таблицы — из ``platform.json`` (``data.task_table``), а не из тела вызова.
Именно это и было причиной удаления обеих операций очереди в пункте 2.18
спеки ``enterprise-mcp-platform``: объявляя имя таблицы, вызывающая сторона
выбирала, чьи данные трогать, то есть вход в данные агента шёл мимо его
конфигурации. Change 2026-10-02-task-queue-into-mcp возвращает операции с
другой схемой — имя объявляет платформа.
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

#: Схема входа задана руками, а не ``build_input_schema``: та выводит из
#: подписи ``{"type": "array"}`` без описания элемента, а контракт здесь и
#: есть в элементах. Операция ``runtime-only``, так что схему модель не
#: видит — она нужна как исполняемый контракт для канала.
INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "error_retry_delay_sec": {
            "type": "number",
            "description": (
                "Через сколько секунд задача в статусе error становится "
                "доступной повторно. По умолчанию 5."
            ),
        },
        "priority_contents": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Список команд, которые берутся раньше очереди (priority-"
                "поллинг канала). Не задан — обычный поллинг по времени "
                "создания."
            ),
        },
    },
}


def create_tool(registry_container: ToolContainer) -> ToolDefinition:
    # Сервис замыкается обработчиком: глобальная привязка означала бы, что
    # вызов уходит в сервис той регистрации, которая отработала последней.
    service: DataService = registry_container.get("data")

    def handle_claim_task(
        ctx: ToolExecutionContext,
        error_retry_delay_sec: float = 5.0,
        priority_contents: list[str] | None = None,
    ) -> str:
        """Захватить одну задачу очереди и вернуть её.

        Захват атомарный: два конкурирующих вызова не получат одну строку.
        Задача считается занятой, если в том же чате уже есть необработанное
        сообщение, — поэтому воркер одного канала не возьмёт вторую задачу
        того же чата, даже если слотов несколько.

        ``session_id``/``user_id`` из контекста вызова в захват не входят:
        отбор ведётся по данным самой строки задачи, и подмена личности
        вызывающим здесь ничего бы не меняла.
        """
        claimed = service.claim_task(
            audience=AUDIENCE_RUNTIME,
            error_retry_delay_sec=error_retry_delay_sec,
            priority_contents=priority_contents,
        )
        return json.dumps(
            {"status": "ok", "claimed": claimed},
            ensure_ascii=False,
            default=str,
        )

    description = (
        "Атомарно захватить одну задачу очереди: самую старую доступную "
        "задачу пользователя, если её чат не занят. Служебная операция — "
        "вызывается каналом при поллинге, не моделью. Возвращает задачу или "
        "null, если задач нет."
    )

    return ToolDefinition(
        name="claim_task",
        description=description,
        handler=handle_claim_task,
        category="data",
        tags=("queue", "infrastructure", "runtime-only"),
        permissions=("data:claim_task",),
        input_schema=INPUT_SCHEMA,
    )
