"""Операция ``purge_logs``: очистка журнала по сроку хранения.

``runtime-only`` по существу, а не по формальности: это удаление данных, и
отдавать такую операцию модели нельзя.

Правило очистки задаёт платформа (``data.log_retention_days`` и
``data.log_purge_empty_outbound`` в ``platform.json``), а не вызывающая
сторона. Аргументы остались переопределением — ими пользуются тесты и
разовые чистки; обычный вызов их не задаёт и получает серверное решение.
"""

from __future__ import annotations

import json

from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.registry import ToolDefinition, build_input_schema

def create_tool(container: ToolContainer) -> ToolDefinition:
    def purge_logs(
        retention_days: int | None = None,
        remove_empty_outbound: bool | None = None,
    ) -> str:
        counters = container.get("data").purge_logs(
            retention_days,
            remove_empty_outbound=remove_empty_outbound,
            audience="runtime",
        )
        return json.dumps({"status": "ok", **counters}, ensure_ascii=False)

    description = (
        "Очистить журнал событий и контекст вопросов. Сколько дней хранить "
        "запись и чистить ли пустые stream-чанки, задаёт сервер; без "
        "аргументов применяется его правило. retention_days=0 означает, что "
        "старые записи не трогаются. Возвращает счётчики удалённых строк по "
        "таблицам. Служебная операция: вызывается агентом по расписанию, не "
        "моделью."
    )

    return ToolDefinition(
        name="data.purge_logs",
        description=description,
        handler=purge_logs,
        capability="data",
        tags=("infrastructure", "runtime-only"),
        permissions=("data:purge_logs",),
        input_schema=build_input_schema(purge_logs),
    )
