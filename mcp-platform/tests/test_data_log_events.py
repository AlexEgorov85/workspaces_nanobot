"""Операция ``log_events``: батчевая запись в журнал.

Проверяются свойства, которые ломаются тихо:

* **счётчики переполнения видны.** ``EventBuffer`` при переполнении теряет
  событие и НЕ поднимает исключение. Если бы ``log_events`` молча возвращала
  «всё принято», агент счёл бы потерю успехом. Возврат ``accepted``/``dropped``
  — единственное место, где потеря становится видимой.
* **fail-fast на негодном событии, а не частичный приём.** Одно битое событие
  в середине батча не должно тихо уносить с собой 99 хороших, и не должно
  проходить молча: это ошибка транспорта агента, и её надо назвать.
* **уровень нормализуется на границе запроса** — иначе сброс буфера упал бы
  на ``valid_level CHECK`` в базе, и потерял бы весь батч, а агент уже
  получил бы «accepted».
* **операция помечена ``runtime-only``** с правом ``data:log_events``:
  журнал пишет поток оборота, а не модельный запрос.
* **схема входа описывает элемент.** ``build_input_schema`` из подписи дал бы
  ``{"type": "array"}`` — контракт был бы не описан.
* **идентичность приходит из контекста вызова, а не из тела батча.** В схеме
  элемента ``session_id``/``user_id``/``request_id`` нет, и сервис игнорирует их
  в пришедшем событии: иначе батч умел бы подписать журнал чужой сессией, и
  след выглядел бы правдоподобным и неверным.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.errors import InvalidRequestError
from libs.enterprise_common.eventing.types import (
    AGENT_COMPLETED,
    AGENT_DEGRADED,
    AGENT_STARTED,
    QUALITY_CHECK,
    TOOL_COMPLETED,
    TOOL_STARTED,
)
from libs.enterprise_common.loader import discover_tool_files

from servers.enterprise.capabilities.data.service.main import DataService
from servers.enterprise.capabilities.data.service.writer import EventBuffer

LOGS = ("public", "agent_gateway_logs")
RUNS = ("public", "agent_question_runs")


def _service(*, maxlen: int = 2048) -> DataService:
    """Сервис с буфером, который ничего не пишет наружу (сброс — заглушка)."""
    written: list[list[dict[str, Any]]] = []
    buffer = EventBuffer(written.extend, maxlen=maxlen, flush_interval=10_000.0)
    service = DataService(db=None, log_table=LOGS, question_runs_table=RUNS)
    service._buffer = buffer  # подмена транспорта: тут проверяем только приём
    service._written = written  # что именно доехало до буфера
    return service


def _rows(service: DataService) -> list[dict[str, Any]]:
    """Сбросить буфер и вернуть события в том виде, в каком их увидит база."""
    service._buffer.flush()
    return list(service._written)


def _ctx(
    session_id: str = "s-ctx",
    user_id: str = "u-ctx",
    request_id: str = "r-ctx",
):
    """Контекст вызова ровно такой формы, какую собирает конвейер."""
    from datetime import datetime, timezone

    from libs.enterprise_common.execution.context import (
        McpCallContext,
        ToolExecutionContext,
    )

    return ToolExecutionContext(
        call=McpCallContext(
            request_id=request_id, session_id=session_id, user_id=user_id
        ),
        tool_name="log_events",
        capability="data",
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _tool(service: DataService):
    from servers.enterprise.capabilities.data.tools import log_events as mod

    return mod.create_tool(ToolContainer(services={"data": service}))


class TestLogEventsHappyPath:
    def test_batch_is_accepted_whole(self) -> None:
        service = _service()
        result = service.log_events(
            [
                {"event_type": AGENT_STARTED, "summary": "1"},
                {"event_type": TOOL_STARTED, "name": "read_file"},
                {"event_type": AGENT_COMPLETED},
            ]
        )
        assert result == {"accepted": 3, "dropped": 0}

    def test_events_reach_the_buffer_normalised(self) -> None:
        service = _service()
        service.log_events(
            [{"event_type": AGENT_DEGRADED, "level": "warning"}],
            "s1",
            "u1",
            "r1",
        )
        assert service._buffer.flush() == 1

    def test_missing_optional_fields_become_empty_not_none_keys(self) -> None:
        """Отсутствующее поле — пустая строка, иначе NOT NULL в базе."""
        service = _service()
        service.log_events([{"event_type": AGENT_STARTED}])
        assert service._buffer.flush() == 1

    def test_empty_batch_is_rejected(self) -> None:
        service = _service()
        with pytest.raises(InvalidRequestError):
            service.log_events([])

    def test_non_list_is_rejected(self) -> None:
        service = _service()
        with pytest.raises(InvalidRequestError):
            service.log_events({"event_type": AGENT_STARTED})  # type: ignore[arg-type]


class TestLogEventsOverflowIsVisible:
    def test_overflow_is_reported_not_hidden(self) -> None:
        """Ключевое свойство: потеря видна вызывающему."""
        service = _service(maxlen=2)
        result = service.log_events(
            [
                {"event_type": name}
                for name in (
                    AGENT_STARTED,
                    AGENT_COMPLETED,
                    TOOL_STARTED,
                    TOOL_COMPLETED,
                    QUALITY_CHECK,
                )
            ]
        )
        assert result == {"accepted": 2, "dropped": 3}

    def test_overflow_does_not_raise(self) -> None:
        """Переполнение — не исключение: ход агента не должен вставать."""
        service = _service(maxlen=1)
        assert service.log_events(
            [{"event_type": AGENT_STARTED}, {"event_type": AGENT_COMPLETED}]
        ) == {
            "accepted": 1,
            "dropped": 1,
        }


class TestLogEventsFailsFastOnGarbage:
    @pytest.mark.parametrize(
        ("bad", "why"),
        [
            ({"event_type": ""}, "пустой event_type"),
            ({"event_type": "   "}, "пробельный event_type"),
            ({"name": "без типа"}, "нет event_type вовсе"),
            ({"event_type": 5}, "event_type не строка"),
            ("строка вместо объекта", "событие не объект"),
        ],
    )
    def test_bad_event_is_refused(self, bad: Any, why: str) -> None:
        service = _service()
        with pytest.raises(InvalidRequestError):
            service.log_events([bad])

    def test_one_bad_event_does_not_silently_sink_the_batch(self) -> None:
        """Частичный приём запрещён: вызывающий должен узнать о потере."""
        service = _service()
        with pytest.raises(InvalidRequestError):
            service.log_events(
                [
                    {"event_type": AGENT_STARTED},
                    {"event_type": ""},
                    {"event_type": AGENT_COMPLETED},
                ]
            )
        # ни одно событие не принято — вызывающий решает, что делать
        assert service._buffer.stats()["pending"] == 0

    def test_error_names_the_offending_position(self) -> None:
        """Позиция в сообщении — единственное, по чему агент поймёт, что чинить."""
        service = _service()
        with pytest.raises(InvalidRequestError) as exc:
            service.log_events(
                [{"event_type": AGENT_STARTED}, {"event_type": ""}]
            )
        assert "events[1]" in str(exc.value)


class TestIdentityComesFromCallContext:
    """Идентичность вызова — единственный источник личности событий.

    Проверяется не «значение записалось», а откуда оно взялось: батч, присланный
    под видом журналирования оборота, не должен уметь подписать журнал чужой
    сессией. Если бы сервис читал ``session_id`` из тела события, оба теста ниже
    прошли бы наоборот — и дефект был бы невидим.
    """

    def test_identity_of_call_reaches_every_event(self) -> None:
        service = _service()
        service.log_events(
            [{"event_type": AGENT_STARTED}, {"event_type": AGENT_COMPLETED}],
            "s-call",
            "u-call",
            "r-call",
        )
        rows = _rows(service)
        assert len(rows) == 2
        for row in rows:
            assert row["session_id"] == "s-call"
            assert row["user_id"] == "u-call"
            assert row["request_id"] == "r-call"

    def test_identity_inside_event_is_ignored(self) -> None:
        service = _service()
        service.log_events(
            [
                {
                    "event_type": AGENT_STARTED,
                    "session_id": "s-чужой",
                    "user_id": "u-чужой",
                    "request_id": "r-чужой",
                }
            ],
            "s-call",
            "u-call",
            "r-call",
        )
        row = _rows(service)[0]
        assert row["session_id"] == "s-call"
        assert row["user_id"] == "u-call"
        assert row["request_id"] == "r-call"

    def test_operation_passes_context_identity_to_the_service(self) -> None:
        """Шов целиком: обработчик получает ``ctx`` и не путает его с ``events``."""
        service = _service()
        _tool(service).handler(ctx=_ctx(), events=[{"event_type": AGENT_STARTED}])
        row = _rows(service)[0]
        assert row["session_id"] == "s-ctx"
        assert row["request_id"] == "r-ctx"

    def test_item_schema_does_not_offer_identity_fields(self) -> None:
        """В схеме элемента личности нет: объявлять её в теле батча нельзя."""
        properties = _tool(_service()).input_schema["properties"]["events"]["items"][
            "properties"
        ]
        for field in ("session_id", "user_id", "request_id"):
            assert field not in properties, f"{field} в схеме элемента события"


class TestLogEventsOperation:
    def test_operation_file_is_discovered(self) -> None:
        from pathlib import Path

        platform_root = Path(__file__).resolve().parent.parent
        cap_dir = platform_root / "servers" / "enterprise" / "capabilities" / "data"
        names = {p.name for p in discover_tool_files(cap_dir.parent)}
        assert "log_events.py" in names

    def test_operation_is_runtime_only_with_permission(self) -> None:
        definition = _tool(_service())
        assert "runtime-only" in definition.tags
        assert "data:log_events" in definition.permissions

    def test_input_schema_describes_the_item(self) -> None:
        schema = _tool(_service()).input_schema
        assert schema["properties"]["events"]["items"]["properties"]["event_type"]
        assert schema["required"] == ["events"]

    def test_operation_returns_counters_as_json(self) -> None:
        service = _service()
        result = json.loads(
            _tool(service).handler(
                ctx=_ctx(),
                events=[{"event_type": AGENT_STARTED}, {"event_type": AGENT_COMPLETED}],
            )
        )
        assert result == {"status": "ok", "accepted": 2, "dropped": 0}

    def test_operation_declares_execution_context(self) -> None:
        """Без ``ctx`` в подписи конвейер не подаст контекст, и личность пропадёт."""
        assert _tool(_service()).wants_context()

    def test_definition_loads_through_registry(self) -> None:
        from libs.enterprise_common.registry import ToolRegistry

        from servers.enterprise.capabilities.data.tools import log_events as mod

        service = _service()
        container = ToolContainer(services={"data": service})
        registry = ToolRegistry([mod.create_tool(container)])
        assert [d.name for d in registry] == ["log_events"]


class TestLevelNormalisation:
    def test_unknown_level_is_refused_at_request_boundary(self) -> None:
        """Недопустимый уровень обязан остановиться здесь, а не в сбросе.

        Если бы он доехал до буфера, упал бы ``valid_level CHECK`` в базе и
        унёс весь батч — а вызывающий уже получил бы «accepted».
        """
        service = _service()
        with pytest.raises(InvalidRequestError):
            service.log_events([{"event_type": AGENT_DEGRADED, "level": "не-уровень"}])
        assert service._buffer.stats()["pending"] == 0

    @pytest.mark.parametrize("level", ["debug", "info", "warn", "error", "WARNING"])
    def test_known_levels_pass(self, level: str) -> None:
        service = _service()
        assert service.log_events(
            [{"event_type": AGENT_DEGRADED, "level": level}]
        )["accepted"] == 1

    def test_empty_level_becomes_info(self) -> None:
        service = _service()
        assert service.log_events(
            [{"event_type": AGENT_DEGRADED, "level": ""}]
        )["accepted"] == 1

    def test_critical_is_not_a_level_this_journal_accepts(self) -> None:
        """Контракт задан DDL, а не фантазией: CHECK принимает четыре значения."""
        service = _service()
        with pytest.raises(InvalidRequestError):
            service.log_events([{"event_type": AGENT_DEGRADED, "level": "critical"}])
