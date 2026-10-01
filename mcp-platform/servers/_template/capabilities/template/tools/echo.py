"""Операция ``echo`` — эталон формы файла операции."""

from __future__ import annotations

import json
from typing import Any

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
        """Вернуть текст и его длину.

        Тело — JSON, и это не вкусовое предпочтение. Слой исполнения дописывает
        ``_execution`` в объект, но строку оставляет байт-в-байт: обернуть её
        значило бы отдать вызывающему описание ответа вместо ответа. Операция,
        возвращающая голый текст, теряет метаданные вызова молча — вызывающий
        не прочитает из ответа ``request_id`` и не сможет связать ответ со
        своим вызовом.

        Все операции платформы возвращают JSON, и заготовка учит тому же.
        """
        result = service.echo(text)
        payload: dict[str, Any] = {"text": result.text, "length": result.length}
        return json.dumps(payload, ensure_ascii=False)

    return ToolDefinition(
        name="echo",
        description="Эхо с указанием длины. Пример операции capability.",
        handler=handle_echo,
        category="template",
    )
