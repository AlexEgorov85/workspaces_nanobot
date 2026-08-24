"""Единый HTTP-клиент к LLM (OpenAI-compatible /chat/completions).

Консолидация: раньше каждый потребитель писал собственный httpx-POST с
ретраями — навык ``audit_analyzer`` (``scripts/llm.py``) и бенчмарк
(``benchmarks/evaluator.py::_call_llm_json``). Здесь — единственный
клиент с общим retry-циклом (exponential backoff через
``lib/utils/retry.py``), который умеет возвращать текст или JSON.

Конфиг (provider/model/api_base/api_key/…) передаётся готовым словарём
от ``lib/services/llm_config.py::resolve_llm_config()`` — клиент сам
конфигурацию не резолвит, чтобы не плодить вторую точку сборки настроек.

Поведение retry (сохранено из ``scripts/llm.py``):
  * ретраятся 429, TimeoutException, ConnectError;
  * любой другой HTTPStatusError пробрасывается сразу;
  * после ``max_retries`` неудач последнее исключение пробрасывается.

Модуль импортируется БЕЗ nanobot: httpx подключается лениво внутри
функций, поэтому импорт остаётся лёгким и без побочных эффектов.
"""

from __future__ import annotations

import json
import re
from typing import Any

from lib.utils.retry import retry_on_exception


def _resolve_cfg(cfg: dict[str, Any] | None) -> dict[str, Any]:
    """Вернуть переданный конфиг или резолвнуть агентский (без переопределений).

    ``resolve_llm_config`` импортируется лениво, чтобы тесты могли
    подменять его через ``monkeypatch`` на ``lib.services.llm_config.*``.
    """
    if cfg is not None:
        return cfg
    from lib.services.llm_config import resolve_llm_config

    return resolve_llm_config()


def call_llm(
    messages: list[dict[str, Any]],
    *,
    cfg: dict[str, Any] | None = None,
    context: list[dict[str, Any]] | None = None,
    model: str | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    max_retries: int = 3,
    timeout: float = 60.0,
) -> str:
    """Вызвать LLM и вернуть текстовый ответ (только ``content``).

    Args:
        messages: Сообщения (system / user / assistant).
        cfg: Резолвнутый LLM-конфиг от ``resolve_llm_config`` (если не
            передан — резолвится здесь без переопределений).
        context: История чата — добавляется в начало перед ``messages``.
        model/max_tokens/temperature: переопределение параметров запроса.
        max_retries: максимум повторов при 429/timeout/connect.
        timeout: таймаут HTTP-запроса в секундах.

    Returns:
        Текстовый ответ LLM (stripped).

    Raises:
        httpx.HTTPStatusError: при не-retryable ошибке HTTP.
        RuntimeError: если LLM вернул пустой ответ или исчерпаны ретраи.
    """
    resolved = _resolve_cfg(cfg)
    api_base = resolved.get("api_base", "").rstrip("/")
    api_key = resolved.get("api_key", "")
    model_name = model or resolved.get("model", "")
    url = f"{api_base}/chat/completions"

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "model": model_name,
        "messages": (context or []) + messages,
        "max_tokens": int(max_tokens or resolved.get("max_tokens", 8192)),
        "temperature": float(
            temperature if temperature is not None
            else resolved.get("temperature", 0.1)
        ),
    }

    data = _post_json(url, payload, headers, timeout, max_retries)
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not content:
        raise RuntimeError("LLM вернул пустой ответ")
    return content.strip()


def call_llm_json(
    messages: list[dict[str, Any]],
    *,
    cfg: dict[str, Any] | None = None,
    context: list[dict[str, Any]] | None = None,
    model: str | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    max_retries: int = 0,
    timeout: float = 60.0,
) -> dict[str, Any] | None:
    """Вызвать LLM и распарсить ответ как JSON-объект.

    При любом сбое (сеть, невалидный JSON, не dict) возвращает ``None`` —
    исключения наружу не пробрасываются. Ответ очищается от markdown-обёрток
    `` ```json ... ``` `` и, если чистый parse не удался, JSON извлекается
    из фрагмента ``{...}`` (тот же фолбэк, что был в evaluator'е).

    Args:
        messages/cfg/context/model/max_tokens/temperature/timeout: как в ``call_llm``.
        max_retries: повторов при 429/timeout/connect (по умолчанию 0 —
            одноразовый вызов, как в бенчмарке).

    Returns:
        Словарь с ответом LLM или ``None`` при ошибке.
    """
    try:
        text = call_llm(
            messages,
            cfg=cfg,
            context=context,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            max_retries=max_retries,
            timeout=timeout,
        )
    except Exception:
        return None

    return _parse_json_object(text)


def _post_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: float,
    max_retries: int,
) -> dict[str, Any]:
    """httpx POST + retry-цикл с exponential backoff через ``retry_on_exception``.

    Ретраит только 429 / TimeoutException / ConnectError; остальные
    HTTPStatusError пробрасываются сразу (хук в ``on_retry`` рейзит).
    """
    import httpx

    def _on_retry(attempt: int, max_r: int, delay: float, exc: Exception) -> None:
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code != 429:
            raise exc

    def _request() -> dict[str, Any]:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            return resp.json()

    return retry_on_exception(
        _request,
        exceptions=(httpx.HTTPStatusError, httpx.TimeoutException, httpx.ConnectError),
        max_retries=max_retries,
        base_delay=1.0,
        max_delay=16.0,
        label="llm",
        on_retry=_on_retry,
    )


def _parse_json_object(text: str) -> dict[str, Any] | None:
    """Распарсить ответ LLM в JSON-объект (с чисткой markdown-обёрток)."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    cleaned = cleaned.strip()
    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            return None
        try:
            result = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    if not isinstance(result, dict):
        return None
    return result
