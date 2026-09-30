"""
Follow Up 2.0 — LLM Client.

Поддерживает три режима:
  - local    : vLLM локально (OpenAI-compatible, api_key='EMPTY')
  - api      : любой внешний OpenAI-compatible endpoint (Fireworks, OpenAI, etc.)
  - gigachat : GigaChat через langchain_gigachat (внутренняя сеть банка)
               Использует access_token из JPY_API_TOKEN / GIGACHAT_ACCESS_TOKEN
               и base_url из GIGACHAT_API_URL, как в ноутбуке.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from time import perf_counter, sleep
from typing import AsyncGenerator, Dict, List, Optional

from openai import AsyncOpenAI, OpenAI

from backend.config import get_settings

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────
# OpenAI-compatible clients (mode = local | api)
# ──────────────────────────────────────────────────────────────────

def get_sync_client() -> OpenAI:
    cfg = get_settings()
    return OpenAI(api_key=cfg.llm_api_key, base_url=cfg.llm_base_url,
                  timeout=cfg.llm_request_timeout)


@lru_cache(maxsize=1)
def get_async_client() -> AsyncOpenAI:
    cfg = get_settings()
    return AsyncOpenAI(api_key=cfg.llm_api_key, base_url=cfg.llm_base_url,
                       timeout=cfg.llm_request_timeout)


# ──────────────────────────────────────────────────────────────────
# GigaChat client (mode = gigachat)
# Использует паттерн из GigaChatApi.ipynb (JPY_API_TOKEN / GIGACHAT_API_URL)
# ──────────────────────────────────────────────────────────────────

_gigachat_llm = None
_gigachat_last_invoke: float = 0.0


def _get_gigachat_llm():
    global _gigachat_llm
    if _gigachat_llm is not None:
        return _gigachat_llm

    cfg = get_settings()
    try:
        from langchain_gigachat.chat_models import GigaChat
    except ImportError:
        raise RuntimeError(
            "langchain_gigachat не установлен. Выполните: pip install langchain-gigachat"
        )

    url = cfg.gigachat_api_url
    token = cfg.gigachat_access_token
    if not url or not token:
        raise RuntimeError(
            "Для режима gigachat укажите GIGACHAT_API_URL и GIGACHAT_ACCESS_TOKEN в .env"
        )

    _gigachat_llm = GigaChat(
        base_url=url,
        access_token=token,
        model=cfg.gigachat_model,
        streaming=False,
        # Без таймаута зависший запрос висит вечно (SSE-карточка не доедет)
        timeout=cfg.llm_request_timeout,
    )
    logger.info(f"[LLM] GigaChat инициализирован: {cfg.gigachat_model} @ {url}")
    return _gigachat_llm


def _gigachat_call(messages: List[Dict], max_tokens: int, temperature: float) -> str:
    """Вызов GigaChat с rate limiter (как в ноутбуке)."""
    global _gigachat_last_invoke
    cfg = get_settings()
    delay = cfg.gigachat_delay

    elapsed = perf_counter() - _gigachat_last_invoke
    if elapsed < delay:
        sleep(delay - elapsed)
    _gigachat_last_invoke = perf_counter()

    llm = _get_gigachat_llm()
    from langchain_core.messages import HumanMessage, SystemMessage, AIMessage

    lc_messages = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "system":
            lc_messages.append(SystemMessage(content=content))
        elif role == "assistant":
            lc_messages.append(AIMessage(content=content))
        else:
            lc_messages.append(HumanMessage(content=content))

    result = llm.invoke(lc_messages)
    return _content_of(result)


# ──────────────────────────────────────────────────────────────────
# Model resolution
# ──────────────────────────────────────────────────────────────────

_resolved_model: Optional[str] = None


def resolve_model_sync(preferred: Optional[str] = None) -> str:
    """Определяет название модели. Приоритет: аргумент → конфиг → автоопределение."""
    global _resolved_model
    cfg = get_settings()

    if preferred:
        return preferred
    if cfg.llm_model_name:
        return cfg.llm_model_name
    if _resolved_model:
        return _resolved_model

    if cfg.llm_mode == "gigachat":
        return cfg.gigachat_model

    try:
        client = get_sync_client()
        models = client.models.list()
        if models.data:
            _resolved_model = models.data[0].id
            logger.info(f"[LLM] Автоопределена модель: {_resolved_model}")
            return _resolved_model
    except Exception as e:
        logger.warning(f"[LLM] Не удалось получить список моделей: {e}")

    fallback = "accounts/fireworks/models/llama-v3p3-70b-instruct"
    logger.warning(f"[LLM] Fallback модель: {fallback}")
    return fallback


def list_available_models() -> List[Dict]:
    """Возвращает список доступных моделей для UI."""
    cfg = get_settings()

    if cfg.llm_mode == "gigachat":
        try:
            llm = _get_gigachat_llm()
            models = llm.get_models().data
            return [
                {"id": m.id_, "display_name": m.id_}
                for m in models
            ]
        except Exception as e:
            logger.error(f"[LLM] Ошибка GigaChat models: {e}")
            return [{"id": cfg.gigachat_model, "display_name": cfg.gigachat_model}]

    try:
        client = get_sync_client()
        models = client.models.list()
        return [
            {"id": m.id, "display_name": Path(m.id).name if "/" in m.id else m.id}
            for m in models.data
        ]
    except Exception as e:
        logger.error(f"[LLM] Ошибка при получении моделей: {e}")
        return []


# ──────────────────────────────────────────────────────────────────
# Ретрай на транзиентные сбои шлюза
# ──────────────────────────────────────────────────────────────────

def _content_of(message) -> str:
    """Текст ответа модели. content бывает None (reasoning-модели, обрыв по
    длине, пустой ответ) — .strip() по нему ронял агента с AttributeError."""
    text = getattr(message, "content", None)
    if not text:
        # у reasoning-моделей полезный текст иногда в отдельном поле
        text = getattr(message, "reasoning_content", None) or ""
    return (text or "").strip()


_RETRY_DELAYS = (2, 6)          # две повторные попытки
_TRANSIENT_MARKERS = ("503", "502", "504", "upstream connect error",
                      "connection termination", "timeout", "timed out",
                      "temporarily unavailable", "connection reset")


def _is_transient(exc: Exception) -> bool:
    """Кратковременный сбой шлюза/сети, а не ошибка запроса.
    Прод: 503 «upstream connect error» от шлюза GigaChat ронял ответ агента."""
    name = type(exc).__name__.lower()
    if "timeout" in name or "connection" in name or name == "servererror":
        return True
    msg = str(exc).lower()
    return any(m in msg for m in _TRANSIENT_MARKERS)


def _call_with_retry(fn, what: str):
    """Синхронный вызов LLM с ретраем на транзиентных ошибках."""
    import time as _time
    last: Optional[Exception] = None
    for attempt in range(len(_RETRY_DELAYS) + 1):
        try:
            return fn()
        except Exception as e:
            last = e
            if attempt >= len(_RETRY_DELAYS) or not _is_transient(e):
                raise
            delay = _RETRY_DELAYS[attempt]
            logger.warning(f"[LLM] {what}: транзиентный сбой ({e.__class__.__name__}), "
                           f"повтор через {delay}с "
                           f"(попытка {attempt + 2}/{len(_RETRY_DELAYS) + 1})")
            _time.sleep(delay)
    raise last  # недостижимо, но явнее для читателя


# ──────────────────────────────────────────────────────────────────
# Generation — sync (для индексации / extraction)
# ──────────────────────────────────────────────────────────────────

def generate_sync(
    messages: List[Dict],
    model: Optional[str] = None,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
) -> str:
    cfg = get_settings()
    mt = max_tokens or cfg.llm_max_tokens
    temp = temperature if temperature is not None else cfg.llm_temperature

    if cfg.llm_mode == "gigachat":
        return _call_with_retry(
            lambda: _gigachat_call(messages, mt, temp), "generate_sync")

    client = get_sync_client()
    model_id = resolve_model_sync(model)

    def _do():
        response = client.chat.completions.create(
            model=model_id,
            messages=messages,
            max_tokens=mt,
            temperature=temp,
            stream=False,
        )
        return _content_of(response.choices[0].message)

    return _call_with_retry(_do, "generate_sync")


# ──────────────────────────────────────────────────────────────────
# Generation — async (для API чата)
# ──────────────────────────────────────────────────────────────────

class LLMUnavailable(RuntimeError):
    """Модель не ответила, и это НЕ повод показать «Ошибка».

    Несёт типизированный исход шлюза (`Outcome`), чтобы вызывающий мог
    деградировать осмысленно: 503 после трёх попыток и протухший токен требуют
    разного поведения, а `except Exception` делает их одинаковыми.
    """

    def __init__(self, outcome) -> None:
        super().__init__(outcome.human)
        self.outcome = outcome


async def generate_async(
    messages: List[Dict],
    model: Optional[str] = None,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    profile: Optional[str] = None,
) -> str:
    """Вызов модели ЧЕРЕЗ ОЧЕРЕДЬ (`core/llm_gateway.py`).

    Очередь подставлена здесь, а не в скиллах: карточка и гипотезы зовут
    `generate_async` и попадают в неё без единой правки внутри себя. Профиль,
    ключ справедливости, дедлайн и токен отмены приходят через `contextvars` —
    их выставляет обработчик хода.

    Профиль задаёт max_tokens/temperature/таймаут, но явные аргументы всегда
    сильнее: скиллы годами калибровали свои значения, и подменять их нельзя.
    """
    from backend.core import llm_gateway as gw

    cfg = get_settings()
    ctx = gw.current_context()
    if profile:
        ctx = gw.CallContext(fairness_key=ctx.fairness_key, profile=profile,
                             deadline_at=ctx.deadline_at, cancel=ctx.cancel,
                             on_wait=ctx.on_wait, stage=ctx.stage)
    prof = gw.profile(ctx.profile)
    mt = max_tokens or cfg.llm_max_tokens or prof.max_tokens
    temp = temperature if temperature is not None else cfg.llm_temperature

    if cfg.llm_mode == "gigachat":
        def _run() -> str:
            return _call_with_retry(
                lambda: _gigachat_call(messages, mt, temp), "generate_async")
    else:
        model_id = resolve_model_sync(model)

        def _run() -> str:
            client = get_sync_client()
            last: Optional[Exception] = None
            for attempt in range(len(_RETRY_DELAYS) + 1):
                try:
                    response = client.chat.completions.create(
                        model=model_id, messages=messages, max_tokens=mt,
                        temperature=temp, stream=False)
                    return _content_of(response.choices[0].message)
                except Exception as e:
                    last = e
                    if attempt >= len(_RETRY_DELAYS) or not _is_transient(e):
                        raise
                    delay = _RETRY_DELAYS[attempt]
                    logger.warning(f"[LLM] generate_async: транзиентный сбой "
                                   f"({e.__class__.__name__}), повтор через {delay}с")
                    sleep(delay)
            raise last

    outcome = await gw.call(_run, ctx=ctx)
    if outcome.ok:
        return outcome.text or ""
    raise LLMUnavailable(outcome)


async def generate_stream(
    messages: List[Dict],
    model: Optional[str] = None,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
) -> AsyncGenerator[str, None]:
    cfg = get_settings()

    if cfg.llm_mode == "gigachat":
        # GigaChat не поддерживает streaming через наш wrapper — эмулируем
        text = await generate_async(messages, model, max_tokens, temperature)
        for word in text.split(" "):
            yield word + " "
        return

    client = get_async_client()
    model_id = resolve_model_sync(model)
    stream = await client.chat.completions.create(
        model=model_id,
        messages=messages,
        max_tokens=max_tokens or cfg.llm_max_tokens,
        temperature=temperature if temperature is not None else cfg.llm_temperature,
        stream=True,
    )
    async for chunk in stream:
        delta = chunk.choices[0].delta.content
        if delta:
            yield delta
