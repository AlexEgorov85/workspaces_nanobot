"""
LLM-клиент с OpenAI-compatible HTTP API.

Поддерживает любой провайдер с эндпоинтом /chat/completions
(Mistral AI, OpenAI, Ollama, vLLM и т.д.).

Конфигурация читается из config.json -> секция "llm":
    {
      "provider": "mistral",
      "model": "mistral-large-latest",
      "api_base": "https://api.mistral.ai/v1",
      "api_key": "секретный-ключ",
      "max_tokens": 8192,
      "temperature": 0.1
    }
"""

from typing import Optional

import httpx

from config import get_llm_config


async def chat(messages: list[dict], *, context: Optional[list[dict]] = None, **kwargs) -> str:
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

    Пример:
        >>> import asyncio
        >>> msgs = [
        ...     {"role": "system", "content": "Ты SQL-эксперт."},
        ...     {"role": "user", "content": "Напиши SELECT для таблицы audits"},
        ... ]
        >>> asyncio.run(chat(msgs))  # doctest: +SKIP
        'SELECT * FROM oarb.audits LIMIT 100'

    Пример с контекстом (история чата):
        >>> history = [{"role": "user", "content": "Привет"}, {"role": "assistant", "content": "Здравствуйте"}]
        >>> asyncio.run(chat(msgs, context=history))  # doctest: +SKIP
    """
    cfg = get_llm_config()
    api_base = cfg.get("api_base", "").rstrip("/")
    api_key = cfg.get("api_key", "")
    model = kwargs.get("model") or cfg.get("model", "mistral-large-latest")
    max_tokens = kwargs.get("max_tokens") or cfg.get("max_tokens", 8192)
    temperature = kwargs.get("temperature") or cfg.get("temperature", 0.1)
    url = f"{api_base}/chat/completions"

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "model": model,
        "messages": (context or []) + messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not content:
        raise RuntimeError("LLM вернул пустой ответ")
    return content.strip()
