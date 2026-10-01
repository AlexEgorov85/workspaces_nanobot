"""Операция ``echo`` — эталон формы файла операции."""

from __future__ import annotations

from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.registry import ToolDefinition
from servers._template.capabilities.template.service.main import EchoService


def create_tool(registry_container: ToolContainer) -> ToolDefinition:
    """Собрать операцию.

    Сервис достаётся здесь и замыкается обработчиком. Модульная глобальная
    переменная означала бы, что вторая регистрация в том же процессе тихо
    перепишет сервис первой операции, а отсутствие сервиса обнаружилось бы
    уже на вызове — вместо сборки, где это ещё можно назвать своим именем.
    """
    service: EchoService = registry_container.get("template")

    def handle_echo(text: str) -> str:
        """Вернуть текст и его длину."""
        result = service.echo(text)
        return f"{result.text} (len={result.length})"

    return ToolDefinition(
        name="echo",
        description="Эхо с указанием длины. Пример операции capability.",
        handler=handle_echo,
        category="template",
    )
