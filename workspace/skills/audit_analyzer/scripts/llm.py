"""LLM-доступ навыка — через MCP, без собственного HTTP.

Навык работает отдельным процессом, поэтому свой HTTP-клиент к провайдеру
у него больше нет: говорить с моделью может только платформа, и
единственный способ — операция ``complete`` её capability ``llm``.

Что это меняет для вызывающей стороны
-------------------------------------

Раньше модуль брал ``get_llm_config()`` — словарь с адресом, моделью и
**ключом** — и передавал его в клиент. Теперь навык не знает ни адреса, ни
модели, ни ключа: настройки лежат в ``mcp-platform/platform.json`` (секция
``llm``), и секрет в процесс навыка не попадает вовсе. Отсюда же исчезает
причина правок на две стороны: смена модели трогает один файл.

Процесс сервера поднимается один на навык и живёт до конца прогона, хотя
вызовов бывает много (генерация SQL с повторами). За ``max_retries`` и
``timeout`` отвечает конфигурация навыка — она описывает бюджет прогона,
а не подключение к провайдеру.
"""

from __future__ import annotations

import sys
from pathlib import Path

from skill_config import get_cli_config

#: Корень платформы. Считается от этого файла, а не от ``cwd``: скрипты
#: навыка запускают из произвольного каталога, и «случайно угаданный корень»
#: означал бы, что в одном месте навык работает, а в другом — нет.
#: ``scripts/llm.py`` → ``parents[4]`` = корень репозитория.
_PLATFORM_ROOT = Path(__file__).resolve().parents[4] / "mcp-platform"
if str(_PLATFORM_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLATFORM_ROOT))

from libs.enterprise_client import LlmOperationError, LlmUnavailable, complete  # noqa: E402

__all__ = ["chat", "LlmOperationError", "LlmUnavailable"]


def chat(messages: list[dict], *, context: list[dict] | None = None, **kwargs) -> str:
    """Отправить сообщения в LLM и получить текстовый ответ.

    Args:
        messages: Список сообщений (system / user / assistant).
        context: История чата (опционально, добавляется перед messages).
        **kwargs: Переопределение параметров запроса (``model``,
            ``max_tokens``, ``temperature``).

    Returns:
        Текстовый ответ LLM (только content, без обёрток).

    Raises:
        LlmOperationError: Платформа ответила доменной ошибкой — сервис не
            настроен либо провайдер отказал.
        LlmUnavailable: Процесс платформы не поднялся, сессия оборвалась
            или не ответила вовремя.
    """
    cli = get_cli_config()
    return complete(
        messages,
        context=context,
        model=kwargs.get("model"),
        max_tokens=kwargs.get("max_tokens"),
        temperature=kwargs.get("temperature"),
        max_retries=int(cli.get("max_retries", 3)),
        timeout=float(cli.get("timeout_sec", 60)),
    )
