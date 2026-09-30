"""Резолв LLM-конфигурации: параметры и окружение, без конфига агента.

Портировано из агента ``lib/services/llm_config.py``. Изменён ровно один
компонент — **источник конфигурации**: агент читал ``config.json``
(``agents.defaults.*`` и ``providers.<provider>.*``), платформа читает
переданный ``Mapping``/``os.environ``. Конфига агента в этом процессе не
существует, а импорт ``config`` запрещён архитектурным стражем, поэтому
источник сделан явной зависимостью, а не спрятанным чтением глобала.

**Что сохранено без изменений**

* приоритет: ``overrides`` → окружение → дефолт;
* дефолты: провайдер ``"openai-compatible"``, ``max_tokens=8192``,
  ``temperature=0.1``, пустой ключ (ключ не обязателен — бывают провайдеры
  без авторизации);
* отказ вместо подстановки значения по умолчанию, если не заданы модель или
  ``api_base``: молчаливая подстановка отправила бы запрос не туда и
  выглядела бы как «модель не отвечает»;
* форма ключей ``overrides`` (``llm_provider``/``llm_model``/``llm_api_base``/
  ``llm_api_key``/``llm_max_tokens``/``llm_temperature``) — по ней приходят
  переопределения skill'ов.

**Что изменено и почему**

* Тексты двух ошибок называют переменные окружения платформы вместо путей в
  ``config.json`` агента. Префикс ``resolve_llm_config:`` и формулировка
  сохранены; упоминание источника, которого в этом процессе нет, было бы
  прямой ложью.
* Вместо ``RuntimeError`` — ``InfrastructureError``: незаданная в окружении
  модель означает, что процесс поднялся с неполной конфигурацией, и retry
  здесь бессмыслен (см. докстринг ``InfrastructureError``).
* Нечисловое значение ``max_tokens``/``temperature`` даёт
  ``InfrastructureError`` с именем переменной, а не ``ValueError`` из
  ``int()``: на проводе это разница между ``[infrastructure_error] ...`` и
  ``[internal_error] invalid literal for int() ...``.

**Переменные окружения.** Основные — с префиксом ``ENTERPRISE_`` (как у
остальных настроек процесса сервера). Короткие ``LLM_*`` — второе имя, а не
вторая сущность: агент уже экспортирует ``LLM_API_KEY`` в своё окружение, а
клиент ``enterprise-mcp`` наследует окружение целиком, поэтому сервер
поднимается с тем же ключом без дублирования секрета в конфиге.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from typing import Any

from libs.enterprise_common.errors import InfrastructureError

#: Провайдер по умолчанию, когда не задан ни overrides, ни окружение.
DEFAULT_PROVIDER = "openai-compatible"
#: Те же значения, что и в агенте: смена дефолта тихо меняет стоимость и
#: качество каждого вызова во всей системе.
DEFAULT_MAX_TOKENS = 8192
DEFAULT_TEMPERATURE = 0.1

#: ``(переменная платформы, короткое имя)`` — по порядку приоритета.
_ENV_NAMES: dict[str, tuple[str, ...]] = {
    "provider": ("ENTERPRISE_LLM_PROVIDER", "LLM_PROVIDER"),
    "model": ("ENTERPRISE_LLM_MODEL", "LLM_MODEL"),
    "api_base": ("ENTERPRISE_LLM_API_BASE", "LLM_API_BASE"),
    "api_key": ("ENTERPRISE_LLM_API_KEY", "LLM_API_KEY"),
    "max_tokens": ("ENTERPRISE_LLM_MAX_TOKENS", "LLM_MAX_TOKENS"),
    "temperature": ("ENTERPRISE_LLM_TEMPERATURE", "LLM_TEMPERATURE"),
    "embed_api_base": ("ENTERPRISE_EMBED_API_BASE",),
    "embed_api_key": ("ENTERPRISE_EMBED_API_KEY",),
    "embed_model": ("ENTERPRISE_EMBED_MODEL",),
    "embed_path": ("ENTERPRISE_EMBED_PATH",),
}


@dataclass(frozen=True)
class LlmConfig:
    """Конфигурация одного вызова провайдера.

    Неизменяемая: конфиг переживает несколько вызовов подряд, и правка на
    середине ретрая означала бы, что первый и последний запрос ушли к разным
    провайдерам.
    """

    provider: str
    model: str
    api_base: str
    api_key: str = ""
    max_tokens: int = DEFAULT_MAX_TOKENS
    temperature: float = DEFAULT_TEMPERATURE
    # Эмбеддер не обязан быть тем же провайдером, что и чат. Пустая
    # строка = «тот же», и это поведение по умолчанию: конфигурация
    # с одним провайдером не должна требовать лишних переменных.
    embed_api_base: str = ""
    embed_api_key: str = ""
    embed_model: str = ""
    embed_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Словарь с теми же ключами, что отдавал агентский резолв.

        Нужен потребителям, ожидающим словарь (логирование, передача в
        дочерний процесс) — смена формы здесь означала бы правку всех таких
        мест сразу при переносе.
        """
        return {
            "provider": self.provider,
            "model": self.model,
            "api_base": self.api_base,
            "api_key": self.api_key,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "embed_api_base": self.embed_api_base,
            "embed_api_key": self.embed_api_key,
            "embed_model": self.embed_model,
            "embed_path": self.embed_path,
        }

    @classmethod
    def from_mapping(cls, raw: LlmConfig | Mapping[str, Any]) -> LlmConfig:
        """Собрать конфиг из словаря агентской формы.

        Отдельная точка входа, а не магия в конструкторе: словарь без
        ``model``/``api_base`` должен падать с тем же сообщением, что и пустое
        окружение, а не с ``TypeError`` от отсутствующего аргумента.
        """
        if isinstance(raw, cls):
            return raw
        provider = str(raw.get("provider") or DEFAULT_PROVIDER)
        model = raw.get("model")
        api_base = raw.get("api_base")
        if not model:
            raise InfrastructureError(
                "resolve_llm_config: не задана модель (ENTERPRISE_LLM_MODEL/LLM_MODEL "
                "или llm_model в overrides)"
            )
        if not api_base:
            raise InfrastructureError(
                f"resolve_llm_config: не задан api_base для провайдера {provider!r} "
                "(ENTERPRISE_LLM_API_BASE/LLM_API_BASE или llm_api_base в overrides)"
            )
        return cls(
            provider=provider,
            model=str(model),
            api_base=str(api_base),
            api_key=str(raw.get("api_key") or ""),
            max_tokens=int(raw.get("max_tokens", DEFAULT_MAX_TOKENS)),
            temperature=float(raw.get("temperature", DEFAULT_TEMPERATURE)),
            embed_api_base=str(raw.get("embed_api_base") or ""),
            embed_api_key=str(raw.get("embed_api_key") or ""),
            embed_model=str(raw.get("embed_model") or ""),
            embed_path=str(raw.get("embed_path") or ""),
        )


def _from_env(env: Mapping[str, str], key: str) -> str:
    """Первое непустое значение из переменных с данным логическим именем."""
    for name in _ENV_NAMES[key]:
        value = (env.get(name) or "").strip()
        if value:
            return value
    return ""


def _number(env: Mapping[str, str], overrides: Mapping[str, Any], key: str, default: Any) -> Any:
    """Взять число из overrides или окружения, назвав источник при отказе.

    Имя источника в тексте ошибки — не деталь: «max_tokens должен быть числом»
    оставляет вопрос «где он задан», а ответ на него — это и есть работа
    диагностики.
    """
    override = overrides.get(f"llm_{key}")
    if override is None or override == "":
        from_env = _from_env(env, key)
        if not from_env:
            return default
        raw: Any = from_env
        source = _ENV_NAMES[key][0]
    else:
        raw = override
        source = f"llm_{key} в overrides"
    try:
        return int(raw) if key == "max_tokens" else float(raw)
    except (TypeError, ValueError) as exc:
        raise InfrastructureError(
            f"resolve_llm_config: {source} должен быть числом, получено {raw!r}"
        ) from exc


def resolve_llm_config(
    overrides: Mapping[str, Any] | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> LlmConfig:
    """Собрать конфигурацию провайдера.

    Args:
        overrides: специфичные переопределения (секция skill'а с ключами
            ``llm_*``). Перекрывают окружение.
        env: источник окружения; по умолчанию ``os.environ``. Параметр
            существует, чтобы резолв был проверяемым тестом без правки
            глобального окружения процесса.

    Returns:
        :class:`LlmConfig` с полями provider, model, api_base, api_key,
        max_tokens, temperature.

    Raises:
        InfrastructureError: не задана модель или ``api_base``, либо
            ``max_tokens``/``temperature`` нечисловые. Подстановки дефолта
            нет: запрос к несуществующей модели неотличим от «модель молчит».
    """
    source: Mapping[str, str] = os.environ if env is None else env
    cfg: Mapping[str, Any] = overrides or {}

    provider = (
        cfg.get("llm_provider")
        or _from_env(source, "provider")
        or DEFAULT_PROVIDER
    )
    model = cfg.get("llm_model") or _from_env(source, "model")
    api_base = cfg.get("llm_api_base") or _from_env(source, "api_base")
    api_key = cfg.get("llm_api_key") or _from_env(source, "api_key") or ""
    embed_api_base = _from_env(source, "embed_api_base")
    embed_api_key = _from_env(source, "embed_api_key")
    embed_model = _from_env(source, "embed_model")
    embed_path = _from_env(source, "embed_path")

    if not model:
        raise InfrastructureError(
            "resolve_llm_config: не задана модель (ENTERPRISE_LLM_MODEL/LLM_MODEL "
            "или llm_model в overrides)"
        )
    if not api_base:
        raise InfrastructureError(
            f"resolve_llm_config: не задан api_base для провайдера {provider!r} "
            "(ENTERPRISE_LLM_API_BASE/LLM_API_BASE или llm_api_base в overrides)"
        )

    return LlmConfig(
        provider=str(provider),
        model=str(model),
        api_base=str(api_base),
        api_key=str(api_key),
        max_tokens=_number(source, cfg, "max_tokens", DEFAULT_MAX_TOKENS),
        temperature=_number(source, cfg, "temperature", DEFAULT_TEMPERATURE),
        embed_api_base=str(embed_api_base),
        embed_api_key=str(embed_api_key),
        embed_model=str(embed_model),
        embed_path=str(embed_path),
    )


def ensure_llm_env(env: MutableMapping[str, str] | None = None) -> None:
    """Гарантировать ``LLM_API_KEY`` в окружении для резолва ``${...}``.

    Нужен потребителям, которые читают ключ через подстановку
    ``${LLM_API_KEY}`` (запуск дочернего процесса, конфиг SDK), а не самому
    резолву платформы: тот читает ``os.environ`` напрямую. Существующее
    значение не перетирается — иначе вызов, пришедший с правильным ключом
    окружения, был бы молча заменён на другой.
    """
    target: MutableMapping[str, str] = os.environ if env is None else env
    if "LLM_API_KEY" in target:
        return
    key = resolve_llm_config(env=target).api_key
    if key:
        target["LLM_API_KEY"] = key
