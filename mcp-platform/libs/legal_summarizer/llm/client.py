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

Вызов ``complete`` идёт **в том же процессе**: ``libs.enterprise_client``
поднимает лёгкий экземпляр сервера только capability ``llm`` и держит одну
сессию на весь прогон. Отдельного процесса платформы на навык больше нет —
текст ниже описывал старую схему, где поднимался ``python -m
servers.enterprise.server``, и вводил в заблуждение при разборе поломок:
кликнуть «процесс не поднялся» там, где его и не было.

LLM-trace (``--llm-trace`` или ``LEGAL_SUMMARIZER_LLM_TRACE=1``) —
диагностическое логирование в stderr для долгих прогонов
``legal_summarizer``. Помогает увидеть, где в map-reduce теряются
данные / зацикливается LLM.
"""


import sys
import time as _time
from pathlib import Path

from libs.legal_summarizer.llm.config import get_cli_config
from libs.legal_summarizer.llm.sanitize import strip_think_blocks

#: Корень платформы — от этого файла, а не от ``cwd``.
#:
#: Индекс 3, а не 5. В агенте модуль лежал по пути
#: ``workspace/skills/legal_summarizer/scripts/llm/client.py``, где
#: ``parents[5]`` действительно был корнем репозитория, и платформа бралась
#: приписыванием ``/ "mcp-platform"``. После переноса в платформу тот же
#: индекс указывал на каталог **над** репозиторием, то есть в домашний
#: каталог пользователя, а ``sys.path`` получал несуществующий путь.
#:
#: Не заметил этого никто по простой причине: процесс платформы и так
#: поднимается с корнем в ``sys.path``, поэтому неверная добавка была
#: безвредной. Опасна она в любом другом входе — CLI, запущенный как файл, или
#: тест, — где импорт ``libs.enterprise_client`` упал бы. Стража
#: ``tests/legal_summarizer/architecture/test_cache_root_source.py`` этот
#: модуль пропускала: ``llm/client.py`` не был в её перечне.
_PLATFORM_ROOT = Path(__file__).resolve().parents[3]
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
from libs.enterprise_common.execution.errors import FAILURE_CODES  # noqa: E402

__all__ = [
    "chat",
    "classify_llm_failure",
    "LlmEmptyResponse",
    "LlmOperationError",
    "LlmUnavailable",
]


class LlmEmptyResponse(Exception):
    """Провайдер ответил, но **текста в ответе нет**.

    Отдельный тип, а не подкласс ``LlmOperationError``: тот означает «сервер
    вернул код отказа», а здесь сервер отказа не возвращал — он вернул ответ
    без содержимого. Причина типовая: рассуждающая модель исчерпала
    ``max_tokens`` на рассуждение и до контента не дошла (замерено на
    боевом прогоне 2026-10-06: ``<think>`` без ответа после него).

    Пока ответ был пустой строкой, отказ уходил наверх как ``REDUCE_INPUT_EMPTY``
    — бизнес-факт «свод действительно пуст», который платформа считает
    **неповторяемым**. Повторный вызов того же документа не помогал, хотя
    от был временным: тот же файл через минуту разбирался. Решение «пустой
    свод — это только про свод» было верным, но код его не обеспечивал.
    """


def classify_llm_failure(exc: BaseException) -> str:
    """Код платформы для отказа LLM-вызова.

    Отказ вызова и пустой результат — разные вещи, и платформа обязана видеть
    разные коды. Когда они сливались в один, отказ провайдера выглядел как
    «свод не получился»: повторять его бессмысленно, а на самом деле у него
    может быть ровно противоположный смысл.

    Повторяемость здесь **не выдумывается**: её платформа уже определила в
    ``RETRYABLE_CODES``, поэтому код обязан быть её собственным. Отсюда и
    решение не заводить здесь новый код ``reduce_*``: неизвестный код платформа
    трактует как ``internal``, то есть как «повторять бессмысленно» — ровно
    противоположность того, что нужно при отказе провайдера.

    ``FAILURE_CODES`` берётся у платформы, а не составляется здесь: свой список
    рано или поздно разойдётся с её словарём, и молча начнёт выдавать коды,
    которых на проводе не бывает.
    """
    if isinstance(exc, LlmEmptyResponse):
        # Ответ без текста — свойство **вызова**, а не документа, поэтому код
        # повторяемый: повтор продолжит с сохранённого состояния, и на живом
        # прогоне тот же документ через минуту разбирался. ``infrastructure_error``
        # — тот же код, которым платформа сама отвечает за «capability llm
        # поднята, а пользоваться ею нечем», и он же в её ``RETRYABLE_CODES``.
        #
        # Свой ``LLM_ERROR`` из словаря платформа считает **неповторяемым**,
        # поэтому им такой отказ не назвать: тот же самый отказ второй раз
        # повторился бы ровно с тем же результатом.
        return "infrastructure_error"
    if isinstance(exc, LlmUnavailable):
        # Сессия не поднялась, оборвалась или не ответила вовремя. Платформа
        # считает ``upstream_unavailable`` повторяемым, и это верно: тот же
        # вызов на живой сессии имеет смысл.
        return "upstream_unavailable"
    if isinstance(exc, LlmOperationError):
        code = str(exc.code or "").strip()
        # Код, который сервер вернул и платформа знает, отдаём как есть.
        if code in FAILURE_CODES:
            return code
        return "internal"
    if isinstance(exc, (TimeoutError,)):
        return "timeout"
    return "internal"

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

    # Ответ без текста — это отказ вызова, а не пустой результат.
    #
    # Раньше здесь стоял только предупреждающий вывод под флагом трассировки,
    # а пустая строка уходила дальше: ``strip_think_blocks("")`` давал пустой
    # свод, домен объявлял ``REDUCE_INPUT_EMPTY``, и вопрос «модель не ответила
    # или своду нечего summarить?» терялся. Ответ без текста поднимается
    # **здесь**, потому что дальше отличить его уже нечем: одинаковый пустой
    # результат получается и от неудачного вызова, и от честного пустого свода.
    #
    # ``response`` возвращается как есть (сырым, включая ``<think>``): смена
    # формы ответа — это отдельное решение, а проверка ниже ничего в нём не
    # меняет и вызывающих не удивляет.
    answer = strip_think_blocks(response)
    if not answer.strip():
        raise LlmEmptyResponse(
            f"модель не вернула текста ответа: в ответе {response_chars} симв., "
            f"из них рассуждения {response_chars - len(answer)}; "
            f"user_chars={user_chars}; max_tokens={kwargs.get('max_tokens') or 'из настроек платформы'}"
        )
    return response
