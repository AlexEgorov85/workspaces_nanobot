"""LLM: клиент провайдера — ВЛАДЕЛЕЦ LLM-вызовов в процессе.

Слой закрывает ровно две вещи: **резолв конфигурации провайдера**
(``config.py``) и **единственный HTTP-клиент к провайдеру** (``client.py``).
Обе перенесены из агента без изменения поведения.

Правила слоя:
* Вне ``libs/llm`` не встречается HTTP-вызов провайдера (правило 8 README,
  проверяется стражем по подстроке эндпойнта в URL). Capability получают
  готовый сервис из контейнера и не создают свой клиент.
* Конфигурация приходит параметром или из ``os.environ``: ``config.json``
  агента в этом процессе не существует, и импорт ``config`` запрещён.
* Агентские копии ``lib/services/llm_client.py`` и ``lib/services/llm_config.py``
  ещё существуют — их импортируют ``skill_config.get_llm_config`` и оба
  skill-конвейера. Их удаление — пункт 3.13 плана, заблокирован до фазы 9.
"""

from libs.llm.client import call_llm, call_llm_json
from libs.llm.config import LlmConfig, ensure_llm_env, resolve_llm_config

__all__ = [
    "LlmConfig",
    "call_llm",
    "call_llm_json",
    "ensure_llm_env",
    "resolve_llm_config",
]
