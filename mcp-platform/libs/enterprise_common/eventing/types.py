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


class UnknownEventType(ValueError):
    """Тип события вне словаря."""

    code = "invalid_params"


def is_known(event_type: str) -> bool:
    return str(event_type or "") in EVENT_TYPES


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
