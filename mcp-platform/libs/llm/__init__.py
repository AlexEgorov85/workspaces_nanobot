"""LLM: **внутренний сервис общения с моделью** — владелец вызовов в процессе.

Слой закрывает три вещи и только три:

* ``config.py`` — резолв конфигурации провайдера (источник: реестр
  ``libs.enterprise_common.settings``, то есть ``platform.json`` +
  окружение процесса);
* ``client.py`` / ``embeddings.py`` — единственный HTTP-вызов к провайдеру;
* ``gateway.py`` — сервис, к которому обращается весь код платформы:
  ``llm.ask(...)``, ``llm.send(...)``, ``llm.embed(...)``, ``llm.describe()``.

Простой метод отправки и получения ответа::

    from libs import llm

    llm.ask("Сожми этот текст в три пункта")
    llm.ask_json("Верни JSON со списком найденных нарушений")
    llm.embed("текст для векторизации")
    llm.describe()   # что настроено, без ключа

Правила слоя:

* Вне ``libs/llm`` не встречается HTTP-вызов провайдера (правило 8 README,
  проверяется стражем). Capability получают сервис из контейнера и не
  создают свой клиент.
* Импорт ``config`` агента запрещён архитектурным стражем: конфигурации
  агента в этом процессе не существует.
* Настройки объявлены в ``platform.json`` (секция ``llm``). Окружение
  процесса остаётся запасным источником и имеет приоритет, поэтому
  развёртывание, задающее ``ENTERPRISE_LLM_*`` вручную, работает как
  раньше, а файл — единственное место, где значения живут в репозитории.
"""

from libs.llm.client import call_llm, call_llm_json
from libs.llm.config import LlmConfig, ensure_llm_env, resolve_llm_config
from libs.llm.embeddings import get_embedding
from libs.llm.gateway import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SEC,
    LlmGateway,
    gateway,
    reset_gateway,
    set_gateway,
)

__all__ = [
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_TIMEOUT_SEC",
    "LlmConfig",
    "LlmGateway",
    "call_llm",
    "call_llm_json",
    "describe",
    "embed",
    "ensure_llm_env",
    "gateway",
    "get_embedding",
    "ask",
    "ask_json",
    "reset_gateway",
    "resolve_llm_config",
    "send",
    "send_json",
    "set_gateway",
]


def ask(prompt: str, **kwargs) -> str:
    """Отправить запрос провайдеру и получить текст ответа.

    Обёртка над ``gateway().ask`` — самый короткий путь от кода до модели.
    Аргументы принимаются как у :meth:`LlmGateway.ask`.
    """
    return gateway().ask(prompt, **kwargs)


def ask_json(prompt: str, **kwargs):
    """Как :func:`ask`, но ответ разбирается как JSON-объект.

    ``None`` — ответ не разобрался; исключения не пробрасываются.
    """
    return gateway().ask_json(prompt, **kwargs)


def send(messages, **kwargs) -> str:
    """Вызвать провайдера готовым списком сообщений ``{role, content}``."""
    return gateway().send(messages, **kwargs)


def send_json(messages, **kwargs):
    """Как :func:`send`, но ответ разбирается как JSON-объект."""
    return gateway().send_json(messages, **kwargs)


def embed(text: str, **kwargs) -> list[float]:
    """Вернуть эмбеддинг текста."""
    return gateway().embed(text, **kwargs)


def describe() -> dict:
    """Настройки сервиса без ``api_key``: провайдер, модель, адрес, эмбеддер."""
    return gateway().describe()
