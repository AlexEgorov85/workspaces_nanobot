"""Конверт события — общий для агента и ``enterprise-mcp``.

Структура одна: журнал читается одним запросом, и поломка восстанавливается одним
разрезом по ``request_id``. Проектный писатель сегодня пишет подмножество этих
полей (теряются ``request_id``, ``metadata``, ``channel``, ``actor``), то есть
корреляция оборота теряется ровно на границе процессов. Список ниже — источник
истины для всех писателей, и страж сверяет с ним фактические колонки ``INSERT``.

``source`` и ``component`` едут в ``metadata``, а не в собственные колонки: колонки
потребовали бы миграции Greenplum 6.5 ради значений, которые сегодня читаются из
одного JSONB, а корреляция по ``request_id`` индексирована и работает.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

from libs.enterprise_common.eventing import types as event_types

#: Источник события: кто его произвёл.
SOURCE_NANOBOT = "nanobot"
SOURCE_ENTERPRISE_MCP = "enterprise_mcp"

#: Компонент — подсистема внутри источника (``data``, ``tool_execution``, ``llm``).
COMPONENT_TOOL_EXECUTION = "tool_execution"

LEVELS: tuple[str, ...] = ("debug", "info", "warn", "error")

#: Поля события, которые попадают в колонки журнала. Порядок совпадает с
#: ``agent_gateway_logs`` — так проверка писателя становится сравнением множеств.
JOURNAL_FIELDS: tuple[str, ...] = (
    "id",
    "event_type",
    "level",
    "session_id",
    "user_id",
    "request_id",
    "channel",
    "actor",
    "name",
    "summary",
    "payload",
    "metadata",
)


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """Одно событие. Неизменяемое: журнал и файловая копия видят один снимок."""

    event_type: str
    summary: str = ""
    name: str = ""
    level: str = "info"
    session_id: str | None = None
    user_id: str | None = None
    request_id: str | None = None
    channel: str | None = None
    actor: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    event_id: str = ""
    timestamp: datetime | None = None

    def __post_init__(self) -> None:
        event_types.require_known(self.event_type)
        if self.level not in LEVELS:
            raise ValueError(f"неизвестный уровень журнала: {self.level!r}")
        if not self.event_id:
            object.__setattr__(self, "event_id", str(uuid.uuid4()))
        if self.timestamp is None:
            object.__setattr__(self, "timestamp", datetime.now(UTC))

    @property
    def id(self) -> str:
        """PK события. Рождается в приложении: в базе колонка без DEFAULT."""
        return self.event_id

    def to_row(self) -> dict[str, Any]:
        """Словарь для записи в журнал.

        Порядок ключей и состав — из :data:`JOURNAL_FIELDS`; писатель сверяется с
        этим списком, поэтому новый столбец нельзя забыть.
        """
        raw = {
            "id": self.id,
            "event_type": self.event_type,
            "level": self.level,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "request_id": self.request_id,
            "channel": self.channel,
            "actor": self.actor,
            "name": self.name,
            "summary": self.summary,
            "payload": _as_json(self.payload),
            "metadata": _as_json(self.metadata),
        }
        return {key: raw[key] for key in JOURNAL_FIELDS}


def _as_json(value: Mapping[str, Any]) -> dict[str, Any]:
    """Привести значение к JSON-совместимому словарю.

    ``MappingProxyType`` из контекста не сериализуется, а контекст кладёт в
    ``metadata`` именно его — поэтому приведение здесь, а не в писателе.
    """
    return json.loads(json.dumps(dict(value), default=str))


def with_identity(
    event: AgentEvent,
    *,
    session_id: str | None = None,
    user_id: str | None = None,
    request_id: str | None = None,
) -> AgentEvent:
    """Копия события с проставленной идентичностью оборота.

    Отдельная функция, а не мутация: событие уже могло уйти в буфер, и «дописать
    личность позже» означало бы два разных события в одном журнале.
    """
    values = asdict(event)
    values.pop("event_id", None)
    values.pop("timestamp", None)
    values.pop("payload", None)
    values.pop("metadata", None)
    # Идентичность уходит из ``values`` явно: она передаётся ниже отдельными
    # аргументами, и оставить её в словаре означало бы «одно значение дважды».
    # Пустое значение означает «оставить как было» — идентичность события
    # вызывающая сторона обязана знать заранее, а не достраивать по месту.
    for key in ("session_id", "user_id", "request_id"):
        values.pop(key, None)
    payload = dict(event.payload)
    metadata = dict(event.metadata)
    return AgentEvent(
        payload=payload,
        metadata=metadata,
        session_id=session_id if session_id is not None else event.session_id,
        user_id=user_id if user_id is not None else event.user_id,
        request_id=request_id if request_id is not None else event.request_id,
        **values,
    )


EMPTY_METADATA: Mapping[str, Any] = MappingProxyType({})
