"""
LLM-клиент (OpenAI-compatible HTTP API) — тонкая обёртка над общим клиентом.

Раньше здесь жил собственный httpx-POST с ретраями; теперь единая
реализация — ``lib/services/llm_client.py`` (та же, что использует
бенчмарк). Этот модуль сохраняет прежний публичный API ``chat`` и
источник конфигурации навыка (``get_llm_config()`` / ``get_cli_config()``).
"""

import time
from typing import Optional

from lib.services.llm_client import call_llm

from skill_config import get_llm_config, get_cli_config


def chat(messages: list[dict], *, context: Optional[list[dict]] = None, **kwargs) -> str:
    """
    Отправить сообщения в LLM и получить текстовый ответ.

    Поддерживает опциональный context — историю чата, которая
    добавляется в начало payload перед основными сообщениями.

    Args:
        messages: Список сообщений (system / user / assistant).
        context: История чата (опционально, добавляется перед messages).
        **kwargs: Переопределение параметров из конфига (model, max_tokens,
                  temperature).

    Returns:
        Текстовый ответ LLM (только content, без обёрток).

    Raises:
        httpx.HTTPStatusError: При ошибке HTTP.
        RuntimeError: Если LLM вернул пустой ответ.
    """
    cfg = get_llm_config()
    cli = get_cli_config()
    return call_llm(
        messages,
        cfg=cfg,
        context=context,
        model=kwargs.get("model"),
        max_tokens=kwargs.get("max_tokens"),
        temperature=kwargs.get("temperature"),
        max_retries=int(cli.get("max_retries", 3)),
        timeout=float(cli.get("timeout_sec", 60)),
    )
