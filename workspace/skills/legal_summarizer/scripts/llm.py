"""LLM-клиент (OpenAI-compatible HTTP API) — тонкая обёртка над общим клиентом.

Единая реализация — ``lib.services.llm_client.call_llm`` (та же, что
использует ``audit_analyzer`` и бенчмарк). Этот модуль сохраняет
прежний публичный API ``chat`` и читает конфигурацию навыка через
``get_llm_config()`` / ``get_cli_config()``.
"""


from skill_config import get_cli_config, get_llm_config

from lib.services.llm_client import call_llm


def chat(
    messages: list[dict],
    *,
    context: list[dict] | None = None,
    **kwargs,
) -> str:
    """Отправить сообщения в LLM и получить текстовый ответ.

    Поддерживает опциональный ``context`` — историю чата, которая
    добавляется в начало ``messages``.

    Args:
        messages: Список сообщений (system / user / assistant).
        context: История чата (опционально).
        **kwargs: Переопределение параметров запроса (``model``,
            ``max_tokens``, ``temperature``).

    Returns:
        Текстовый ответ LLM (stripped).
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
        timeout=float(cli.get("timeout_sec", 120)),
    )
