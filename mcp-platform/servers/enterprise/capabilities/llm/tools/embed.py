"""Операция ``embed`` — эмбеддинг текста.

Обслуживает capability ``vectors``. Публичная операция по двум причинам:
векторный поиск нужен и агенту (спросить «похожие документы»), и внутреннему
владельцу индексов, который прежде держал собственный HTTP-клиент.

Ошибки — доменные, без traceback наружу: ``invalid_request`` на некорректных
аргументах, ``infrastructure_error`` на недоступности провайдера.
"""

from __future__ import annotations

import json

from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.registry import ToolDefinition, build_input_schema

from servers.enterprise.capabilities.llm.service.main import (
    AUDIENCE_RUNTIME,
    LlmService,
)


def create_tool(registry_container: ToolContainer) -> ToolDefinition:
    # Сервис замыкается обработчиком, а не лежит в модульной переменной:
    # значение глобальной зависело бы от порядка регистрации операций, и вторая
    # регистрация в процессе тихо переписала бы первую.
    service: LlmService = registry_container.get("llm")

    def handle_embed(
        text: str,
        model: str | None = None,
        max_retries: int = 3,
        timeout: float = 60.0,
    ) -> str:
        """Вернуть эмбеддинг текста.

        Адрес провайдера и заголовки аргументами не принимаются — ровно как у
        ``complete``: их собирает владелец HTTP-клиента из своего конфига, поэтому
        подсунуть операции свой endpoint нельзя.
        """
        result = service.embed(
            text=text,
            model=model,
            max_retries=max_retries,
            timeout=timeout,
            audience=AUDIENCE_RUNTIME,
        )
        return json.dumps(
            {"vector": list(result.vector), "dimension": result.dimension,
             "model": result.model},
            ensure_ascii=False,
        )

    return ToolDefinition(
        name="embed",
        description=(
            "Вернуть эмбеддинг текста. Используется поиском по векторам; "
            "в обычном диалоге вызывать не нужно."
        ),
        handler=handle_embed,
        category="llm",
        permissions=("llm:embed",),
        tags=("infrastructure", "runtime-only"),
        quality_policy="llm_result",
        input_schema=build_input_schema(handle_embed),
    )
