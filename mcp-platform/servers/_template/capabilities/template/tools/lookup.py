"""Операция ``lookup`` — пример доменной ошибки «не найдено»."""

from __future__ import annotations

from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.registry import ToolDefinition
from servers._template.capabilities.template.service.main import EchoService

#: Контейнер и сервис подставляются загрузчиком при регистрации.
container: ToolContainer | None = None
service: EchoService | None = None


def handle_lookup(key: str) -> str:
    """Найти запись. В эталоне всегда бросает ``not_found``."""
    if service is None:  # pragma: no cover - защита от неверной сборки
        raise RuntimeError("сервис не инициализирован")
    return service.find(key).text


def create_tool(registry_container: ToolContainer) -> ToolDefinition:
    global container, service
    container = registry_container
    service = EchoService()
    return ToolDefinition(
        name="lookup",
        description="Найти запись по ключу. Пример доменной ошибки not_found.",
        handler=handle_lookup,
        category="template",
    )
