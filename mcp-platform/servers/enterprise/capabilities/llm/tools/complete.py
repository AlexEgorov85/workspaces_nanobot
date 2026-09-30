"""Операция ``complete`` — вызов настроенного провайдера, ответ текстом.

Инфраструктурная операция: ею пользуются capability ``audit`` (генерация
SQL) и skill-конвейер. Модели она не предназначена — профиль вызова
проверяет сервис, а объявление помечено тегом ``runtime-only`` (см.
``LlmService._require_runtime``).

Адрес провайдера, заголовки и тело запроса аргументами **не принимаются**:
вход операции — текст запроса и параметры генерации. Всё остальное
собирает владелец HTTP-клиента из своего конфига, поэтому подсунуть вызову
операции свой endpoint или заголовок ``Authorization`` нельзя.
"""

from __future__ import annotations

from typing import Any

from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.registry import ToolDefinition
from servers.enterprise.capabilities.llm.service.main import (
    AUDIENCE_RUNTIME,
    LlmService,
)

#: Контейнер подставляется загрузчиком при регистрации операции.
container: ToolContainer | None = None


def handle_complete(
    prompt: str,
    system: str = "",
    context: list[dict[str, Any]] | None = None,
    model: str | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    max_retries: int = 3,
    timeout: float = 60.0,
) -> str:
    """Получить текстовый ответ провайдера по ``prompt``."""
    if container is None:  # pragma: no cover - защита от неверной сборки
        raise RuntimeError("контейнер не инициализирован")
    service: LlmService = container.get("llm")
    result = service.complete(
        prompt=prompt,
        system=system,
        context=context,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        max_retries=max_retries,
        timeout=timeout,
        audience=AUDIENCE_RUNTIME,
    )
    return result.text


def create_tool(registry_container: ToolContainer) -> ToolDefinition:
    global container
    container = registry_container
    return ToolDefinition(
        name="complete",
        description=(
            "Вызвать настроенного LLM-провайдера и вернуть текст ответа. "
            "Инфраструктурная операция: доступна рантайму агента, не модели."
        ),
        handler=handle_complete,
        category="llm",
        tags=("infrastructure", "runtime-only"),
        permissions=("llm:complete",),
    )
