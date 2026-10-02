"""Словарь типов событий.

Строка типа приходит из кода, поэтому словарь закрытый: ``agent.completed`` и
``mcp_history_search_done`` — не одно и то же, но выглядят одинаково, и через полгода
в журнале будет пять похожих имён одного события. Проверка на входе дешевле, чем
разбор журнала постфактум.

Префикс отвечает за источник ответственности, а не за процесс: ``tool.*``
порождается и агентом, и платформой, а различаются они полем ``component``.

Зарезервированный MCP префикс ``io.modelcontextprotocol/`` в словарь не входит и
не должен: это служебный обмен протокола, а не события проекта.
"""

from __future__ import annotations

#: События агента.
AGENT_STARTED = "agent.started"
AGENT_COMPLETED = "agent.completed"
AGENT_FAILED = "agent.failed"

#: Обращения к модели.
LLM_REQUESTED = "llm.requested"
LLM_COMPLETED = "llm.completed"
LLM_FAILED = "llm.failed"

#: Исполнение операций. Их порождает конвейер исполнения (§ runtime/tool-execution).
TOOL_STARTED = "tool.started"
TOOL_COMPLETED = "tool.completed"
TOOL_FAILED = "tool.failed"
TOOL_TIMEOUT = "tool.timeout"

#: Вложения.
ARTIFACT_CREATED = "artifact.created"
ARTIFACT_READ = "artifact.read"

#: Проверки качества.
QUALITY_CHECK = "quality.check"

#: Полный словарь. Порядок — по источнику ответственности, не по алфавиту.
EVENT_TYPES: frozenset[str] = frozenset(
    {
        AGENT_STARTED,
        AGENT_COMPLETED,
        AGENT_FAILED,
        LLM_REQUESTED,
        LLM_COMPLETED,
        LLM_FAILED,
        TOOL_STARTED,
        TOOL_COMPLETED,
        TOOL_FAILED,
        TOOL_TIMEOUT,
        ARTIFACT_CREATED,
        ARTIFACT_READ,
        QUALITY_CHECK,
    }
)

#: Префиксы допустимых типов. Отдельный список, чтобы опечатка в последнем
#: компоненте имени (``tool.complted``) не прошла проверку по полному совпадению
#: и не плодила новый тип в словаре.
ALLOWED_PREFIXES: tuple[str, ...] = ("agent.", "llm.", "tool.", "artifact.", "quality.")


#: Имена пробного характера. Ровно те, что перечислены в требовании
#: «Пробные события не пишутся в продовую таблицу»: префиксы ``smoke.`` и
#: ``probe_`` и имя ``live.db_probe``.
#:
#: Список объявлен здесь, а не у каждой проверки, потому что проверка одна:
#: и писатель, и страж, и будущий запрет на пути ``log_events`` обязаны
#: отвечать на один и тот же вопрос одинаково. Добавление сюда нового имени
#: автоматически закрывает его на всех путях, а не в том модуле, где про него
#: вспомнили.
PROBE_EVENT_PREFIXES: tuple[str, ...] = ("smoke.", "probe_")
PROBE_EVENT_NAMES: frozenset[str] = frozenset({"live.db_probe"})


def is_probe_event_type(event_type: str) -> bool:
    """Пробное ли это имя события.

    Повторяет условие замера из требования — ``event_type LIKE 'smoke.%' OR
    event_type LIKE 'probe_%' OR event_type = 'live.db_probe'``. Проверка по
    регистру нечувствительна: имя приходит извне, и ``SMOKE.x`` отличается от
    ``smoke.x`` только написанием, а в таблице это один и тот же мусор.
    """
    value = str(event_type or "").strip().lower()
    if value in PROBE_EVENT_NAMES:
        return True
    return any(value.startswith(prefix) for prefix in PROBE_EVENT_PREFIXES)


class UnknownEventType(ValueError):
    """Тип события вне словаря."""

    code = "invalid_params"


def is_known(event_type: str) -> bool:
    return str(event_type or "") in EVENT_TYPES


def is_declared_prefix(event_type: str) -> bool:
    """Префикс имени объявлен в :data:`ALLOWED_PREFIXES`, а само имя — нет.

    Список префиксов объявлен именно для такого вопроса: он отделяет опечатку
    в последнем компоненте (``tool.complted`` — человек уже в правильном
    соглашении, поправлять нужно одно имя) от чужой схемы имён целиком
    (``tool_call`` — здесь нужно менять соглашение, а не слово). Поэтому он
    используется на пути ``log_events`` (``DataService._observe_event_type``),
    где имя приходит извне, иначе объявление осталось бы просто текстом.
    """
    value = str(event_type or "")
    return any(value.startswith(prefix) for prefix in ALLOWED_PREFIXES)


def require_known(event_type: str) -> str:
    """Проверить тип и вернуть его же.

    Бросает, а не пропускает молча: событие с неизвестным типом в журнале
    нечитаемо, и обнаружится это через месяц в запросе, где его уже не разобрать.
    """
    value = str(event_type or "").strip()
    if value not in EVENT_TYPES:
        raise UnknownEventType(
            f"тип события {value!r} вне словаря; допустимы: "
            + ", ".join(sorted(EVENT_TYPES))
        )
    return value
