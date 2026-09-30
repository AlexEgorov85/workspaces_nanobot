"""Операция ``echo`` — эталон формы файла операции."""

from __future__ import annotations

from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.registry import ToolDefinition
from servers._template.capabilities.template.service.main import EchoService

#: Контейнер и сервис подставляются загрузчиком при регистрации.
container: ToolContainer | None = None
service: EchoService | None = None


def handle_echo(text: str) -> str:
    """Вернуть текст и его длину."""
    if service is None:  # pragma: no cover - защита от неверной сборки
        raise RuntimeError("сервис не инициализирован")
    result = service.echo(text)
    return f"{result.text} (len={result.length})"


def create_tool(registry_container: ToolContainer) -> ToolDefinition:
    global container, service
    container = registry_container
    service = EchoService()
    return ToolDefinition(
        name="echo",
        description="Эхо с указанием длины. Пример операции capability.",
        handler=handle_echo,
        category="template",
    )
