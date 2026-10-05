"""Операция ``lookup`` — пример доменной ошибки «не найдено»."""

from __future__ import annotations

from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.registry import ToolDefinition
from servers._template.capabilities.template.service.main import EchoService


def create_tool(registry_container: ToolContainer) -> ToolDefinition:
    """Собрать операцию: сервис замыкается обработчиком (см. ``echo``)."""
    service: EchoService = registry_container.get("template")

    def handle_lookup(key: str) -> str:
        """Найти запись. В эталоне всегда бросает ``not_found``."""
        return service.find(key).text

    return ToolDefinition(
        name="template.lookup",
        description="Найти запись по ключу. Пример доменной ошибки not_found.",
        handler=handle_lookup,
        capability="template",
    )
