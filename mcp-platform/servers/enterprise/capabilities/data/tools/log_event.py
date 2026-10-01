"""Операция ``log_event`` — запись события в долговечный журнал.

Неблокирующий вход: событие уходит в буфер, батч сбрасывается воркером. Ход
агента не ждёт свободного соединения пула — иначе запрос модели конкурирует с
журналом, а потеря события при этом не сопровождается ошибкой.
"""

from __future__ import annotations

from typing import Any

from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.execution.context import ToolExecutionContext
from libs.enterprise_common.registry import ToolDefinition, build_input_schema
from servers.enterprise.capabilities.data.service.main import (
    AUDIENCE_MODEL,
    DataService,
)


def create_tool(registry_container: ToolContainer) -> ToolDefinition:
    # Сервис достаётся здесь и замыкается: глобальная привязка ради «плоской»
    # сигнатуры означала, что значение зависит от порядка регистрации операций,
    # а вторая ``create_tool`` в процессе тихо уводила вызовы в чужой сервис.
    service: DataService = registry_container.get("data")

    def handle_log_event(
        ctx: ToolExecutionContext,
        event_type: str,
        name: str = "",
        level: str = "info",
        summary: str = "",
        payload: dict[str, Any] | None = None,
    ) -> str:
        """Записать событие в журнал и вернуть ``accepted`` либо ``dropped``.

        ``request_id`` и идентичность сессии проставляются здесь, из контекста
        вызова: событие без ``request_id`` невозможно связать с оборотом, а
        вписывать его вручную — значит надеяться, что агент не забудет.
        """
        return service.log_event(
            event_type=event_type,
            name=name,
            level=level,
            summary=summary,
            payload=payload,
            session_id=ctx.session_id,
            user_id=ctx.user_id,
            request_id=ctx.request_id,
            audience=AUDIENCE_MODEL,
        )

    return ToolDefinition(
        name="log_event",
        description=(
            "Записать событие в долговечный журнал gateway. Неблокирующая: "
            "возвращает accepted или dropped при переполнении буфера."
        ),
        handler=handle_log_event,
        category="data",
        tags=("logging", "infrastructure"),
        quality_policy="none",
        input_schema=build_input_schema(handle_log_event),
    )
