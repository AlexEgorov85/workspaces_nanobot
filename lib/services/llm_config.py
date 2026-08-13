"""Единый резолв LLM-конфигурации из глобальных SETTINGS.

Подход (вынесен из навыка audit_analyzer, scripts/skill_config.py):
дефолт берётся из ``agents.defaults`` (модель/провайдер) и
``providers.<provider>`` (api_base/api_key) конфигурации агента,
а переопределения (например, ``skills.audit_analyzer.llm_*`` из
project.json) передаются через ``overrides``.

SETTINGS уже прошли резолв ``${VAR}`` в ``config.py``, поэтому
``providers.<provider>.apiKey`` содержит реальный ключ — зависеть
от ``os.environ`` и побочных импортов не нужно.

Используется:
  * навыком audit_analyzer — ``get_llm_config()``;
  * бенчмарками (benchmarks/runner.py) — чтобы брать ТУ ЖЕ LLM,
    что и агент (модель/провайдер/ключ из одних настроек).

Модуль импортируется БЕЗ nanobot: config.py подключается лениво.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# Дефолты, совпадающие с прежними значениями навыка (fallback, если
# в SETTINGS нет ни agents.defaults, ни providers).
_DEFAULT_MODEL = "gpt-4o-mini"
_DEFAULT_API_BASE = "https://api.openai.com/v1"


def resolve_llm_config(overrides: Optional[dict] = None) -> Dict[str, Any]:
    """Собрать LLM-конфиг (provider/model/api_base/api_key/параметры).

    Args:
        overrides: Специфичные переопределения (например, секция
            ``skills.audit_analyzer`` с ключами ``llm_*``). Перекрывают
            дефолт агента.

    Returns:
        Словарь: provider, model, api_base, api_key, max_tokens, temperature.
    """
    from config import SETTINGS

    _cfg = overrides or {}
    defaults = SETTINGS.get("agents", {}).get("defaults", {}) or {}

    provider = _cfg.get("llm_provider") or defaults.get("provider") or "openai-compatible"
    provider_cfg = SETTINGS.get("providers", {}).get(provider) or {}

    model = _cfg.get("llm_model") or defaults.get("model") or _DEFAULT_MODEL
    api_base = _cfg.get("llm_api_base") or provider_cfg.get("apiBase") or ""
    api_key = _cfg.get("llm_api_key") or provider_cfg.get("apiKey") or ""

    return {
        "provider": provider,
        "model": model,
        "api_base": api_base or _DEFAULT_API_BASE,
        "api_key": api_key,
        "max_tokens": int(_cfg.get("llm_max_tokens", 8192)),
        "temperature": float(_cfg.get("llm_temperature", 0.1)),
    }


def ensure_llm_env() -> None:
    """Гарантировать ``LLM_API_KEY`` в окружении для резолва ``${...}``.

    Лоадеры конфигурации (nanobot ``resolve_config_env_vars``, config.py)
    подставляют ``${LLM_API_KEY}`` из переменных окружения; без явной
    установки они падают ``ValueError``. Ключ берётся из того же резолва,
    что использует агент и навык, — без побочного импорта config.py.
    """
    import os

    if "LLM_API_KEY" in os.environ:
        return
    key = resolve_llm_config().get("api_key")
    if key:
        os.environ["LLM_API_KEY"] = key
