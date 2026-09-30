"""Операция ``log_event`` — запись события в долговечный журнал.

Неблокирующий вход: событие уходит в буфер, батч сбрасывается воркером. Ход
агента не ждёт свободного соединения пула — иначе запрос модели конкурирует с
журналом, а потеря события при этом не сопровождается ошибкой.
"""

from __future__ import annotations

from typing import Any

from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.registry import ToolDefinition
from servers.enterprise.capabilities.data.service.main import (
    AUDIENCE_MODEL,
    DataService,
)


def handle_log_event(
    event_type: str,
    name: str = "",
    level: str = "info",
    summary: str = "",
    payload: dict[str, Any] | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
) -> str:
    """Записать событие в журнал и вернуть ``accepted`` либо ``dropped``."""
    service: DataService = container_get("data")
    return service.log_event(
        event_type=event_type,
        name=name,
        level=level,
        summary=summary,
        payload=payload,
        session_id=session_id,
        user_id=user_id,
        audience=AUDIENCE_MODEL,
    )


#: Контейнер подставляется загрузчиком; глобальная привязка нужна, чтобы
#: сигнатура обработчика оставалась плоской и читалась в discovery агента.
container: ToolContainer | None = None


def container_get(capability: str) -> Any:
    if container is None:  # pragma: no cover - защита от неверной сборки
        raise RuntimeError("контейнер не инициализирован: операция вызвана вне загрузчика")
    return container.get(capability)


def create_tool(registry_container: ToolContainer) -> ToolDefinition:
    global container
    container = registry_container
    return ToolDefinition(
        name="log_event",
        description=(
            "Записать событие в долговечный журнал gateway. Неблокирующая: "
            "возвращает accepted или dropped при переполнении буфера."
        ),
        handler=handle_log_event,
        category="data",
        tags=("logging", "infrastructure"),
    )
