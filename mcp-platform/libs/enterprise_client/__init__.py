"""Клиент платформы: разговор с процессом enterprise-mcp по протоколу MCP.

Сервер — единственный, кто говорит с провайдером; клиент не делает
HTTP-вызовов и не знает настроек. Он нужен там, где сервер недоступен
изнутри: skill-конвейер запускается отдельным процессом и своего клиента
не имеет.

Сейчас здесь только LLM — единственная capability, которую процесс вне
агента действительно вызывает (см. ``llm.py``).
"""

from libs.enterprise_client.llm import (
    LlmClient,
    LlmOperationError,
    LlmUnavailable,
    close_default,
    complete,
    complete_json,
    default_client,
    embed,
    platform_root,
)

__all__ = [
    "LlmClient",
    "LlmOperationError",
    "LlmUnavailable",
    "close_default",
    "complete",
    "complete_json",
    "default_client",
    "embed",
    "platform_root",
]
