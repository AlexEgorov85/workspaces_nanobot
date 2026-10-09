"""Подключение upstream ``LLMUsageStore`` через observer-pipeline.

См. спеку ``openspec/specs/storage/usage-store/spec.md`` и design D3.

Единственный стабильный путь подключения ``LLMUsageStore`` как
LLM-call observer в нашем runtime — обернуть ``provider_snapshot_loader``
(``AgentLoop`` создаёт провайдер внутри ``from_config(...)`` и не
возвращает его наружу). Каждый раз, когда ``AgentLoop`` запрашивает
``ProviderSnapshot``, обёртка вызывает
``snapshot.provider.set_llm_call_observer(store.record)``.

Для ``FallbackProvider`` дополнительно подключается
``set_fallback_model_observer(...)`` (по образцу
``nanobot.cli.gateway_runtime._observe_provider``).

Fail-soft: если ``set_llm_call_observer`` бросает исключение,
обёртка логирует WARNING и возвращает snapshot без observer —
агент продолжает работать (usage tracking — observability concern,
не business-critical).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from loguru import logger
from nanobot.providers.base import LLMProvider


def attach_llm_observer(
    provider: LLMProvider,
    store: Any | None,
) -> bool:
    """Подключить ``store.record`` как LLM-call observer.

    Returns:
        ``True`` если observer успешно подключён, ``False`` если
        произошла ошибка или ``store is None``.
    """
    if store is None:
        return False
    try:
        provider.set_llm_call_observer(store.record)
        return True
    except Exception as exc:
        logger.warning(
            "LLMUsageStore observer failed to attach: {}",
            exc,
        )
        return False


def attach_fallback_model_observer(
    provider: LLMProvider,
    bus_or_handler: Any | None,
) -> bool:
    """Подключить ``set_fallback_model_observer`` для FallbackProvider.

    Это отдельный observer для семантических событий смены модели
    (НЕ для LLM calls). Подключается только если провайдер —
    ``FallbackProvider``.
    """
    if bus_or_handler is None:
        return False
    try:
        from nanobot.providers.fallback_provider import FallbackProvider
    except ImportError:
        return False
    if not isinstance(provider, FallbackProvider):
        return False
    try:
        provider.set_fallback_model_observer(bus_or_handler)
        return True
    except Exception as exc:
        logger.warning(
            "Fallback model observer failed to attach: {}",
            exc,
        )
        return False


def wrap_provider_snapshot_loader(
    base_loader: Callable[..., Any],
    store: Any | None,
    bus: Any | None = None,
) -> Callable[..., Any]:
    """Обернуть ``provider_snapshot_loader`` для подключения observer.

    На каждом вызове ``base_loader(...)`` обёртка:
    1. вызывает ``base_loader(...)`` для получения ``ProviderSnapshot``;
    2. вызывает ``attach_llm_observer(snapshot.provider, store)``;
    3. вызывает ``attach_fallback_model_observer(snapshot.provider, bus)``;
    4. возвращает snapshot.

    Если ``store is None`` — обёртка возвращает snapshot без
    модификаций (LLM usage отключён в конфигурации).
    """

    def wrapped(*, preset_name: str | None = None, **kwargs: Any) -> Any:
        snapshot = base_loader(preset_name=preset_name, **kwargs)
        if snapshot is None:
            return snapshot
        provider = getattr(snapshot, "provider", None)
        if provider is not None:
            attach_llm_observer(provider, store)
            attach_fallback_model_observer(provider, bus)
        return snapshot

    return wrapped
