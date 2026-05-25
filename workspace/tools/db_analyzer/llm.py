"""Кешированный LLM-провайдер на основе конфига агента.

Провайдер создаётся один раз через ``make_provider()`` и переиспользуется.
При изменении конфига (проверяется сигнатура через ``provider_signature()``)
провайдер пересоздаётся автоматически — как это делает ``AgentLoop._refresh_provider_snapshot()``.

Функция ``chat()`` сама получает кешированный провайдер, вызывающему коду
не нужно думать о жизненном цикле.
"""

from typing import Optional

from nanobot.providers.base import LLMProvider
from nanobot.providers.factory import make_provider, provider_signature

from .config import load_agent_config

_provider: Optional[LLMProvider] = None
_last_signature: tuple = ()


def create_provider() -> LLMProvider:
    """Вернуть кешированный LLM-провайдер.

    Если конфиг изменился (провайдер/модель/api_base/api_key и т.д.),
    провайдер пересоздаётся. В остальных случаях возвращается
    существующий экземпляр — полная переписка с ``AgentLoop._refresh_provider_snapshot()``.
    """
    global _provider, _last_signature

    config = load_agent_config()
    sig = _compute_signature(config)

    if _provider is None or sig != _last_signature:
        _provider = make_provider(config)
        _last_signature = sig

    return _provider


def _compute_signature(config):
    """Вычислить сигнатуру конфига для отслеживания изменений.

    Использует ``provider_signature()`` из nanobot — тот же механизм,
    которым ``AgentLoop`` проверяет, нужно ли пересоздавать провайдер.
    """
    try:
        return provider_signature(config)
    except Exception:
        cfg = config.agents.defaults
        p = config.get_provider(cfg.model)
        return (
            cfg.model, cfg.provider,
            config.get_provider_name(cfg.model),
            p.api_key if p else None,
            p.api_base if p else None,
        )


def refresh_provider() -> LLMProvider:
    """Принудительно пересоздать провайдер (сброс кеша)."""
    global _provider, _last_signature
    _provider = None
    _last_signature = ()
    return create_provider()


def get_model() -> str:
    """Имя модели из defaults агента."""
    return load_agent_config().agents.defaults.model


def get_defaults():
    """Вернуть объект ``AgentDefaults`` (max_tokens, temperature и т.д.)."""
    return load_agent_config().agents.defaults


async def chat(messages: list[dict], *, context: Optional[list[dict]] = None, **kwargs) -> str:
    """Отправить чат-запрос, вернуть текст ответа.

    Провайдер берётся из кеша (``create_provider()``), конфиг из кеша.
    При передаче ``context`` он добавляется перед основными сообщениями.

    Args:
        messages: основные сообщения для LLM.
        context: опциональная история чата (добавляется перед messages).
        **kwargs: model, max_tokens, temperature (по умолчанию из config).

    Returns:
        Текстовый ответ LLM.

    Raises:
        RuntimeError: если LLM вернул finish_reason == "error".
    """
    provider = create_provider()
    defaults = get_defaults()

    full = (context or []) + messages

    model = kwargs.pop("model", None) or get_model()
    max_tokens = kwargs.pop("max_tokens", None) or getattr(defaults, "max_tokens", 1024)
    temperature = kwargs.pop("temperature", None) or getattr(defaults, "temperature", 0.7)

    resp = await provider.chat(
        messages=full,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        **kwargs,
    )
    if resp.finish_reason == "error":
        raise RuntimeError(f"LLM error: {resp.content}")
    return (resp.content or "").strip()


def get_provider_config():
    """Вернуть (api_base, api_key) текущего провайдера для прямых HTTP-вызовов

    (например, для /v1/embeddings в vector mode).
    """
    provider = create_provider()
    api_base = (
        getattr(provider, "api_base", None)
        or getattr(provider, "_effective_base", None)
    )
    api_key = getattr(provider, "api_key", None) or ""
    return api_base, api_key
