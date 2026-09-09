"""LLM-клиент (OpenAI-compatible HTTP API) — тонкая обёртка над общим клиентом.

Единая реализация — ``lib.services.llm_client.call_llm`` (та же, что
использует ``audit_analyzer`` и бенчмарк). Этот модуль сохраняет
прежний публичный API ``chat`` и читает конфигурацию навыка через
``get_llm_config()`` / ``get_cli_config()``.

LLM-trace (``--llm-trace`` или ``LEGAL_SUMMARIZER_LLM_TRACE=1``) —
диагностическое логирование в stderr для долгих прогонов
``legal_summarizer``. Помогает увидеть, где в map-reduce теряются
данные / зацикливается LLM.
"""


import os
import sys
import time as _time

from llm.config import get_cli_config, get_llm_config

from lib.services.llm_client import call_llm


_LLM_TRACE_ENABLED = (
    "--llm-trace" in sys.argv
    or os.environ.get("LEGAL_SUMMARIZER_LLM_TRACE") == "1"
)


def _trace(stage: str, **fields) -> None:
    if not _LLM_TRACE_ENABLED:
        return
    parts = [f"{k}={v}" for k, v in fields.items()]
    sys.stderr.write(
        f"[llm-trace {_time.monotonic():.2f}s] {stage} " + " ".join(parts) + "\n"
    )
    sys.stderr.flush()


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
    system_chars = len(messages[0]["content"]) if messages else 0
    user_chars = len(messages[1]["content"]) if len(messages) > 1 else 0
    _trace(
        "begin",
        n_msgs=len(messages) + (len(context) if context else 0),
        system=system_chars,
        user=user_chars,
        model=kwargs.get("model") or cfg.get("model", "?"),
        max_tokens=kwargs.get("max_tokens") or cfg.get("max_tokens", "?"),
        timeout=float(cli.get("timeout_sec", 120)),
    )
    start = _time.monotonic()
    try:
        response = call_llm(
            messages,
            cfg=cfg,
            context=context,
            model=kwargs.get("model"),
            max_tokens=kwargs.get("max_tokens"),
            temperature=kwargs.get("temperature"),
            max_retries=int(cli.get("max_retries", 3)),
            timeout=float(cli.get("timeout_sec", 120)),
        )
    except Exception as exc:
        _trace(
            "error",
            duration=f"{_time.monotonic() - start:.2f}s",
            err=type(exc).__name__,
            msg=str(exc)[:200],
        )
        raise
    response_chars = len(response)
    _trace(
        "done",
        duration=f"{_time.monotonic() - start:.2f}s",
        response=response_chars,
    )
    if _LLM_TRACE_ENABLED and response_chars == 0:
        sys.stderr.write(
            f"[llm-trace WARNING] LLM returned EMPTY response "
            f"(user_chars={user_chars})\n"
        )
        sys.stderr.flush()
    return response

