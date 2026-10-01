"""Единый HTTP-клиент к LLM (OpenAI-compatible эндпойнт чата).

Портировано из агента ``lib/services/llm_client.py`` **без изменения
поведения**: те же сигнатуры, те же дефолты, тот же retry-цикл (exponential
backoff через ``libs.enterprise_common/retry.py``), та же чистка ответа от
markdown-обёрток. Отличается только источник конфигурации — см.
``libs/llm/config.py``.

Поведение retry (сохранено из агента):
  * ретраятся 429, TimeoutException, ConnectError;
  * любой другой HTTPStatusError пробрасывается сразу;
  * после ``max_retries`` неудач последнее исключение пробрасывается.

Модуль импортируется без HTTP-библиотеки на верхнем уровне: она
подключается лениво внутри функций, поэтому импорт остаётся лёгким и без
побочных эффектов, а ``libs/llm`` можно импортировать в процессе, где сети
нет (тесты сервисов, проверка конфигурации при старте).

Агентская копия ``lib/services/llm_client.py`` ещё существует: её
импортируют ``audit_analyzer`` и ``legal_summarizer``. Удаление — пункт 3.13
плана, заблокирован до фазы 9.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from libs.enterprise_common.retry import retry_on_exception
from libs.llm.config import LlmConfig

#: Путь эндпойнта чата. Живёт только здесь: вне ``libs/llm`` строка
#: запрещена стражем (правило 8 README) — иначе вторая копия клиента
#: проскочила бы мимо единственного владельца.
CHAT_PATH = "chat/completions"


def _resolve_cfg(cfg: LlmConfig | Mapping[str, Any]) -> LlmConfig:
    """Принять конфиг в виде объекта или словаря.

    Резолвить конфиг здесь было нельзя: это читало бы процесс изнутри
    библиотеки, и тогда значение приходило бы из двух мест — из реестра и
    из окружения мимо него. Конфиг обязан прийти от вызывающей стороны:
    сервер достаёт его из ``Settings``, и всё, что ниже, работает с уже
    разрешённым значением.

    Принимается и словарь: потребители, пришедшие из агента, отдают конфиг
    словарём, и перевод каждого из них на новый тип не должен быть условием
    переезда.
    """
    if isinstance(cfg, LlmConfig):
        return cfg
    return LlmConfig.from_mapping(cfg)


def call_llm(
    messages: list[dict[str, Any]],
    *,
    cfg: LlmConfig | Mapping[str, Any],
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
        cfg: конфиг от :func:`resolve_llm_config` (то есть от реестра).
            Обязателен: клиент не знает, где настройки, и не должен решать
            это за вызывающую сторону.
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
    api_base = resolved.api_base.rstrip("/")
    model_name = model or resolved.model
    url = f"{api_base}/{CHAT_PATH}"

    headers = {"Content-Type": "application/json"}
    if resolved.api_key:
        headers["Authorization"] = f"Bearer {resolved.api_key}"

    payload = {
        "model": model_name,
        "messages": (context or []) + messages,
        "max_tokens": int(max_tokens or resolved.max_tokens),
        "temperature": float(
            temperature if temperature is not None else resolved.temperature
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
    cfg: LlmConfig | Mapping[str, Any],
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
    из фрагмента ``{...}``.

    Args:
        messages/cfg/context/model/max_tokens/temperature/timeout: как в :func:`call_llm`.
        max_retries: повторов при 429/timeout/connect (по умолчанию 0 —
            одноразовый вызов).

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

    return parse_json_object(text)


def _post_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: float,
    max_retries: int,
) -> dict[str, Any]:
    """POST + retry-цикл с exponential backoff через ``retry_on_exception``.

    Ретраит только 429 / TimeoutException / ConnectError; остальные
    HTTPStatusError пробрасываются сразу (хук в ``on_retry`` рейзит).

    Исключения наружу идут как есть — в ``httpx``. Доменная ошибка
    ``InfrastructureError`` появляется на границе capability, где понятно,
    что именно провайдер не ответил.
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


def parse_json_object(text: str) -> dict[str, Any] | None:
    """Распарсить ответ LLM в JSON-объект (с чисткой markdown-обёрток).

    Имена функции, а не ``_parse_json_object``, потому что её использует
    сервис ``libs/llm/gateway.py``: ответ модели в JSON приходит одним и тем
    же способом — с markdown-обёрткой ```json и внятным объектом в середине
    мусора, и вторая копия этого разбора разъехалась бы с первой при первой
    же правке чистки.
    """
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
