"""Модель событий платформы: словарь, конверт, писатель.

Один словарь типов на агента и MCP. Событие, смысл которого непонятен из
самого имени, не пишется вовсе: журнал, который читают через `history_search`,
должен оставаться разбираемым без знания внутренностей каждой capability.
"""

from .models import (
    COMPONENT_TOOL_EXECUTION,
    JOURNAL_FIELDS,
    LEVELS,
    SOURCE_ENTERPRISE_MCP,
    SOURCE_NANOBOT,
    AgentEvent,
    with_identity,
)
from .types import (
    AGENT_COMPLETED,
    AGENT_FAILED,
    AGENT_STARTED,
    ALLOWED_PREFIXES,
    ARTIFACT_CREATED,
    ARTIFACT_READ,
    EVENT_TYPES,
    LLM_COMPLETED,
    LLM_FAILED,
    LLM_REQUESTED,
    QUALITY_CHECK,
    TOOL_COMPLETED,
    TOOL_FAILED,
    TOOL_STARTED,
    TOOL_TIMEOUT,
    UnknownEventType,
    is_known,
    require_known,
)
from .writer import ACCEPTED, DROPPED, NO_SINK, REJECTED, EventSink, EventWriter

__all__ = [
    "ACCEPTED",
    "AGENT_COMPLETED",
    "AGENT_FAILED",
    "AGENT_STARTED",
    "ALLOWED_PREFIXES",
    "ARTIFACT_CREATED",
    "ARTIFACT_READ",
    "AgentEvent",
    "COMPONENT_TOOL_EXECUTION",
    "DROPPED",
    "EVENT_TYPES",
    "EventSink",
    "EventWriter",
    "JOURNAL_FIELDS",
    "LEVELS",
    "LLM_COMPLETED",
    "LLM_FAILED",
    "LLM_REQUESTED",
    "NO_SINK",
    "QUALITY_CHECK",
    "REJECTED",
    "SOURCE_ENTERPRISE_MCP",
    "SOURCE_NANOBOT",
    "TOOL_COMPLETED",
    "TOOL_FAILED",
    "TOOL_STARTED",
    "TOOL_TIMEOUT",
    "UnknownEventType",
    "is_known",
    "require_known",
    "with_identity",
]
