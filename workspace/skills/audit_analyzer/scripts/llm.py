"""
LLM-клиент с OpenAI-compatible HTTP API.

Поддерживает любой провайдер с эндпоинтом /chat/completions
(Mistral AI, OpenAI, MiniMax, Ollama, vLLM и т.д.).

Конфигурация читается из project.json → skills.audit_analyzer.llm_*:
    {
      "provider": "openai-compatible",
      "model": "gpt-4o-mini",
      "api_base": "https://api.openai.com/v1",
      "api_key": "секретный-ключ",
      "max_tokens": 8192,
      "temperature": 0.1
    }
"""

import time
from typing import Optional

import httpx

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
    provider = cfg.get("provider", "openai-compatible")
    api_base = cfg.get("api_base", "").rstrip("/")
    api_key = cfg.get("api_key", "")
    model = kwargs.get("model") or cfg.get("model", "gpt-4o-mini")
    max_tokens = kwargs.get("max_tokens") or cfg.get("max_tokens", 8192)
    temperature = kwargs.get("temperature") or cfg.get("temperature", 0.1)
    url = f"{api_base}/chat/completions"
    print(f"[LLM] provider={provider} model={model} api_base={api_base}")

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "model": model,
        "messages": (context or []) + messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    max_retries = get_cli_config().get("max_retries", 3)
    timeout = get_cli_config().get("timeout_sec", 60)

    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
            break
        except httpx.HTTPStatusError as e:
            last_exc = e
            if e.response.status_code == 429 and attempt < max_retries:
                sleep_sec = 2 ** attempt
                print(f"[LLM] 429 Too Many Requests, retrying in {sleep_sec}s...")
                time.sleep(sleep_sec)
                continue
            raise
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            last_exc = e
            if attempt < max_retries:
                sleep_sec = 2 ** attempt
                print(f"[LLM] {type(e).__name__}, retrying in {sleep_sec}s...")
                time.sleep(sleep_sec)
                continue
            raise
    else:
        raise RuntimeError(f"LLM call failed after {max_retries + 1} attempts: {last_exc}")

    data = resp.json()
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not content:
        raise RuntimeError("LLM вернул пустой ответ")
    return content.strip()
