"""Конверт события — общий для агента и ``enterprise-mcp``.

Структура одна: журнал читается одним запросом, и поломка восстанавливается одним
разрезом по ``request_id``. Раньше здесь стояло утверждение, что проектный
писатель пишет только часть полей и корреляция оборота теряется на границе
процессов; это было неверно — агентский писатель
(``lib/services/db_logging_service.py``) давно пишет тот же набор из двенадцати
полей, что и платформенный, поэтому обе половины журнала читаются по одной схеме.
Список ниже — источник истины для обоих писателей, и страж сверяет с ним
фактические колонки ``INSERT``.

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

#: Уровни журнала. **Регистр совпадает с CHECK-ограничением** ``valid_level``
#: в ``sql/logs/create_public_agent_gateway_logs.sql``
#: (``level IN ('DEBUG','INFO','WARN','ERROR')``).
#:
#: Раньше здесь стоял нижний регистр, а у ``loader.py`` — верхний. Оба списка
#: были верными по отдельности и неверными вместе: внутренние события
#: платформы (``tool.started``, ``quality.check``) проверялись по нижнему
#: списку, писались в базу нижним регистром и отвергались её ``CHECK`` —
#: сброс буфера падал целиком, а вместе с ним терялся весь батч. Живой прогон
#: 2026-10-01 показал это как «сброс буфера не удался, событий потеряно: 29».
#:
#: Теперь набор один, и он верхний; ``AgentEvent`` нормализует вход, поэтому
#: вызывающий может писать ``"info"`` — в базу уйдёт ``"INFO"``.
LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARN", "ERROR")

#: ``WARNING`` — синоним, который встречается в конфигурации и в привычном
#: ``logging``. В базу он не пишется: ``CHECK`` его не принимает.
_LEVEL_ALIASES: dict[str, str] = {"WARNING": "WARN"}

#: Порядок важности уровней — **единственный** числовой счёт в проекте.
#:
#: Выводится из :data:`LEVELS`, а не объявляется рядом с ним: вторая
#: числовая шкала разъедется с первой при первой же правке одной из них, и
#: разъезд будет молчащим — фильтр по уровню отбрасывает событие, не объясняя
#: почему. Агент до сих пор держит такую копию у себя
#: (``lib/services/db_logging_service.py``, ``_should_log``), и страж
#: ``tests/test_journal_noise_policy.py`` сверяет её с этим значением.
LEVEL_RANKS: Mapping[str, int] = MappingProxyType({name: rank for rank, name in enumerate(LEVELS)})

#: Порог по умолчанию. Факт оборота (``INFO``) пишется, диагностика
#: (``DEBUG``) — нет. Замеренное обоснование: в таблице журнала не было ни
#: одной строки ``DEBUG`` и ни одной ``WARN``, то есть четверть объявленной
#: шкалы не использовалась, а разделить «шум» и «сигнал» по уровню было
#: нечем. Дефолт ``INFO`` — это то, что приводит журнал к двум осмысленным
#: состояниям по умолчанию: факт оборота и отказ.
DEFAULT_MIN_LEVEL = "INFO"

#: Уровень диагностики: «событие существует, но само по себе оно ничего не
#: сообщает». Именно сюда попадает проверка качества, у которой не было ни
#: одного замечания, — по замеру 46 из 48 таких событий несли ноль
#: информации, то есть 13 % таблицы.
DIAGNOSTIC_LEVEL = "DEBUG"


def normalize_level(value: str | None) -> str:
    """Привести уровень к тому, что принимает ``valid_level`` в базе.

    Пустое значение — ``INFO``. Неизвестное — отказ, а не тихая замена:
    опечатка в уровне, съеденная молча, выглядит в журнале как событие иной
    важности, а читатель журнала фильтрует по важности.
    """
    candidate = (value or "").strip().upper()
    candidate = _LEVEL_ALIASES.get(candidate, candidate)
    if not candidate:
        return "INFO"
    if candidate not in LEVELS:
        raise ValueError(
            f"неизвестный уровень журнала: {value!r}; допустимы {', '.join(LEVELS)}"
        )
    return candidate


def level_rank(value: str | None) -> int:
    """Числовая важность уровня. Регистр и синоним ``WARNING`` допускаются.

    Пустое значение — ``INFO``, как и в :func:`normalize_level`: событие без
    уровня не «лёгкое» и не «тяжёлое», оно обычное, и трактовать его как
    диагностику значит выбросить по настройке, о которой никто не знал.
    Неизвестное значение — отказ, а не молчаливая замена: иначе фильтр
    сравнивал бы уровень с порядком, которого нет.
    """
    return LEVEL_RANKS[normalize_level(value)]


def is_at_least(value: str | None, minimum: str) -> bool:
    """Не ниже ли уровень ``value`` порога ``minimum``."""
    return level_rank(value) >= level_rank(minimum)


def is_diagnostic(value: str | None) -> bool:
    """Уровень ли это диагностики, то есть ниже порога по умолчанию.

    Отдельная функция, а не ``value == "DEBUG"``: появление в шкале ещё одного
    уровня ниже порога не должно требовать правки всех сравнений по одной.
    """
    return level_rank(value) < level_rank(DEFAULT_MIN_LEVEL)


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
        # Нормализация здесь, а не у писателя: ``to_row()`` и файловая копия
        # видят один и тот же снимок, поэтому строка, ушедшая в базу, и
        # строка на диске не могут разойтись регистром уровня.
        object.__setattr__(self, "level", normalize_level(self.level))
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
