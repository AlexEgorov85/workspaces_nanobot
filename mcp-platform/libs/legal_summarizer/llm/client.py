"""LLM-доступ навыка — через MCP, без собственного HTTP.

Единая реализация общения с моделью — в платформе
(``mcp-platform/libs/llm``), а навык вызывает её операцию ``complete``.
Собственного HTTP-клиента у навыка больше нет, и настройки провайдера
(адрес, модель, ключ) в его распоряжении не остаётся: они живут в
``mcp-platform/platform.json``.

Почему это важно именно здесь
-----------------------------

Конвейер суммаризации шлёт по запросу на каждый чанк — за проход это сотни
вызовов. Свой клиент означал бы вторую копию retry, разбора ответа и резолва
настроек, а расхождение с платформенной копией показало бы себя первой же
сменой модели. Один сервис и один набор настроек вместо этого.

Процесс платформы поднимается один на навык и живёт до конца прогона, хотя
вызовов много: поднимать его на каждый запрос было бы дороже самой модели.

LLM-trace (``--llm-trace`` или ``LEGAL_SUMMARIZER_LLM_TRACE=1``) —
диагностическое логирование в stderr для долгих прогонов
``legal_summarizer``. Помогает увидеть, где в map-reduce теряются
данные / зацикливается LLM.
"""


import os
import sys
import time as _time
from pathlib import Path

from libs.legal_summarizer.llm.config import get_cli_config

#: Корень платформы — от этого файла, а не от ``cwd``:
#: ``scripts/llm/client.py`` → ``parents[5]`` = корень репозитория.
_PLATFORM_ROOT = Path(__file__).resolve().parents[5] / "mcp-platform"
if str(_PLATFORM_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLATFORM_ROOT))

from libs.enterprise_client import (  # noqa: E402
    LlmOperationError,
    LlmUnavailable,
    complete,
)
from libs.enterprise_common.execution.context import (  # noqa: E402
    McpCallContext as _McpCallContext,
)


__all__ = ["chat", "LlmOperationError", "LlmUnavailable"]

def _trace_enabled() -> bool:
    """Флаг трассировки: аргумент запуска ИЛИ настройка домена.

    Раньше второй источник - ``LEGAL_SUMMARIZER_LLM_TRACE`` в окружении -
    недоступен: на платформе окружение читает только реестр настроек.
    """
    if "--llm-trace" in sys.argv:
        return True
    from libs.legal_summarizer.llm.config import llm_trace_enabled

    return llm_trace_enabled()


def _identity() -> _McpCallContext | None:
    """Идентичность оборота, переданная tool'ом в окружение подпроцесса.

    Tool ``legal_summarizer_query`` кладёт её в ``env`` конкретного запуска, а не
    в аргументы командной строки: аргументы пишет модель, и названное ею имя
    сессии границей изоляции не является.

    Читает окружение навык, а не платформенный клиент: у того свой запрет — он
    вообще не разбирает окружение, потому что читает его только реестр, и
    настройки, которые реестр не объявил, не должны выглядеть как настройки.

    Пусто — когда навык запустили вне оборота (например, из shell вручную). Тогда
    ``_meta`` не уйдёт вовсе, и сервер ответит ``identity_missing``: это точнее,
    чем выдуманная сессия, которая потом попадёт в журнал как настоящая.
    ``request_id`` необязателен — клиент платформы досоставит самостоятельный.
    """
    from libs.legal_summarizer.llm.config import get_identity

    identity = get_identity()
    session_id = identity.get("session_id")
    user_id = identity.get("user_id")
    if not session_id or not user_id:
        return None
    return _McpCallContext(
        session_id=session_id,
        user_id=user_id,
        request_id=identity.get("request_id") or None,
    )


def _trace(stage: str, **fields) -> None:
    if not _trace_enabled():
        return
    parts = [f"{k}={v}" for k, v in fields.items()]
    sys.stderr.write(
        f"[llm-trace {_time.monotonic():.2f}s] {stage} " + " ".join(parts) + "\n"
    )
    sys.stderr.flush()


def chat(
    messages: list[dict],
    *,
    context: list[dict] | None = None,
    **kwargs,
) -> str:
    """Отправить сообщения в LLM и получить текстовый ответ.

    Поддерживает опциональный ``context`` — история чата, которая
    добавляется в начало ``messages``.

    Args:
        messages: Список сообщений (system / user / assistant).
        context: История чата (опционально).
        **kwargs: Переопределение параметров запроса (``model``,
            ``max_tokens``, ``temperature``).

    Returns:
        Текстовый ответ LLM (stripped).

    Raises:
        LlmOperationError: Платформа ответила доменной ошибкой — сервис не
            настроен либо провайдер отказал.
        LlmUnavailable: Процесс платформы не поднялся, сессия оборвалась
            или не ответила вовремя.
    """
    cli = get_cli_config()
    system_chars = len(messages[0]["content"]) if messages else 0
    user_chars = len(messages[1]["content"]) if len(messages) > 1 else 0
    timeout = float(cli.get("timeout_sec", 120))
    _trace(
        "begin",
        n_msgs=len(messages) + (len(context) if context else 0),
        system=system_chars,
        user=user_chars,
        # Модель и потолок токенов навыку неизвестны: они в настройках
        # платформы. Раньше тут печаталось значение из собственного конфига,
        # и именно из-за него навык знал про копию настроек.
        model=kwargs.get("model") or "из настроек платформы",
        max_tokens=kwargs.get("max_tokens") or "из настроек платформы",
        timeout=timeout,
    )
    start = _time.monotonic()
    try:
        response = complete(
            messages,
            context=context,
            identity=_identity(),
            model=kwargs.get("model"),
            max_tokens=kwargs.get("max_tokens"),
            temperature=kwargs.get("temperature"),
            max_retries=int(cli.get("max_retries", 3)),
            timeout=timeout,
        )
    except Exception as exc:
        _trace(
            "error",
            duration=f"{_time.monotonic() - start:.2f}s",
            err=type(exc).__name__,
            msg=str(exc)[:200],
        )
        raise
    response_chars = len(response)
    _trace(
        "done",
        duration=f"{_time.monotonic() - start:.2f}s",
        response=response_chars,
    )
    if _LLM_TRACE_ENABLED and response_chars == 0:
        sys.stderr.write(
            f"[llm-trace WARNING] LLM returned EMPTY response "
            f"(user_chars={user_chars})\n"
        )
        sys.stderr.flush()
    return response
