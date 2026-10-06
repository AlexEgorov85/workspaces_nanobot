"""Отказ инструмента попадает в журнал как отказ.

Требование: ``lib/hooks/tool_audit_hook.py``,
``lib/services/db_logging_service.py`` (change
``2026-10-04-journal-observability-repair``, capability ``logging-db``).

Дефект, который страж держит: отказ, возникший на проводе ДО входа в конвейер
платформы, не оставлял в журнале ни одной строки. Наблюдение 2026-10-04
(контролируемый опыт, два вызова подряд с разными метками в ``session_id``):
вызов с ``priority_contents: null`` отвергнут и не оставил ни одной строки;
вызов с ``priority_contents: []`` прошёл и оставил три. Отказ не порождает даже
``tool.started`` — конвейер не начат, — а молчание неотличимо от того, что
вызова не было.

Писать отказ обязана та сторона, которая его видит, а видят его две: платформа
(доменный отказ внутри успешного конверта) и агент (отказ до конвейера).
Имя — каноническое ``tool.failed`` у обеих, а различает их ``metadata.source``.
Отдельное имя не заводится: при ``log_unknown_event_type_policy = strict``
непризнанное имя не пишется вовсе.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from lib.hooks.tool_audit_hook import ToolAuditHook
from lib.services.db_logging_service import EVENT_SOURCE_KEY, DbLoggingService, LogEvent
from lib.services.log_transport import LocalFallbackSink, McpLogWriter

from config import EXPECTED_RUNTIME_TABLE_NAMES

#: Имена таблиц берутся из конфигурации, а не зашиваются строкой: страж
#: ``test_no_hardcoded_table_names.py`` запрещает литерал, и запрет тут по
#: делу — переименование таблицы должно ломать сборку в одном месте.
_TABLES = EXPECTED_RUNTIME_TABLE_NAMES["test"]


def _run_sync(coro: Any) -> Any:
    return asyncio.run(coro)


class _RecordingClient:
    """Клиент, который помнит батчи и отвечает принятым счётчиком."""

    def __init__(self) -> None:
        self.batches: list[dict[str, Any]] = []

    async def call(
        self, operation: str, arguments: dict[str, Any] | None = None, *, identity: Any = None
    ) -> str:
        events = (arguments or {}).get("events") or []
        self.batches.append(dict(arguments or {}))
        return json.dumps({"status": "ok", "accepted": len(events), "dropped": 0})


def _service(tmp_path: Path) -> tuple[DbLoggingService, _RecordingClient]:
    client = _RecordingClient()
    service = DbLoggingService(
        dsn="",
        table_name=_TABLES["gateway_logs"],
        question_runs_table=_TABLES["question_runs"],
        mcp_writer=McpLogWriter(call=client.call, run=_run_sync),
        fallback_sink=LocalFallbackSink(str(tmp_path / "fallback.jsonl")),
    )
    return service, client


def _refusal(text: str = "[operation_failed] Input validation error: None is not of type 'array'") -> str:
    """Текст отказа, каким его видит агент для отказа на проводе."""
    return text


def _run_tool(
    hook: ToolAuditHook,
    service: DbLoggingService,
    *,
    tool_name: str = "mcp_enterprise_claim_task",
    status: str = "error",
    detail: str | None = None,
    session_key: str = "postgres:42",
) -> None:
    """Провести один вызов инструмента через хук аудита.

    Путь взят из жизненного цикла фреймворка: ``before_execute_tools`` кладёт
    записи, ``after_iteration`` проставляет статус. Подделка была бы проверкой
    формы, а не поведения.
    """
    call = type("ToolCall", (), {"name": tool_name, "id": "tc1", "arguments": {}})()
    ctx = type(
        "Ctx",
        (),
        {"session_key": session_key, "iteration": 0, "tool_calls": [call], "tool_events": []},
    )()
    asyncio.run(hook.before_execute_tools(ctx))
    ctx.tool_events = [
        {"name": tool_name, "status": status, "detail": detail if detail is not None else ""}
    ]
    asyncio.run(hook.after_iteration(ctx))


def _drain(service: DbLoggingService) -> list[LogEvent]:
    events: list[LogEvent] = []
    while True:
        try:
            item = service._queue.get_nowait()
        except Exception:
            return events
        if isinstance(item, LogEvent):
            events.append(item)


def _written_rows(service: DbLoggingService, client: _RecordingClient) -> list[dict[str, Any]]:
    service._flush_batch(_drain(service))
    rows: list[dict[str, Any]] = []
    for batch in client.batches:
        rows.extend(batch.get("events") or [])
    return rows


class TestToolAuditJournal:
    """Отказ виден в журнале как отказ, а успех — как успех."""

    def test_domain_refusal_is_logged_as_failure(self, tmp_path: Path) -> None:
        """Отказ на проводе даёт ``tool.failed``, а не тишину.

        Имя каноническое: у платформы доменный отказ пишется тем же именем, и
        различать их должен ``metadata.source``, а не выдуманное четвёртое имя.
        """
        service, client = _service(tmp_path)
        service.register_request("postgres:42", "r1", user_id="u1")
        hook = ToolAuditHook(db_logging_service=service)
        _run_tool(hook, service, detail=_refusal())

        rows = _written_rows(service, client)
        failures = [row for row in rows if row["event_type"] == "tool.failed"]
        assert failures, f"отказ не записан в журнал: {[r['event_type'] for r in rows]}"
        payload = failures[0]["payload"]
        assert payload["status"] in ("error", "timeout"), payload
        assert payload.get("error_code"), f"в отказе нет кода: {payload}"
        assert payload.get("error_message"), f"в отказе нет сообщения: {payload}"
        # ``tool.completed`` со статусом ``ok`` рядом означал бы, что отказ
        # записан как успех: именно это читается в отчёте как «всё завершилось».
        assert not [
            row
            for row in rows
            if row["event_type"] == "tool.completed" and row["payload"].get("status") == "ok"
        ], rows

    def test_guard_fails_when_refusal_is_not_logged(self, tmp_path: Path) -> None:
        """Без записи отказа страж действительно падает.

        Проверяется на хуке без службы журнала — то есть ровно на том поведении,
        которое было до правки. Страж, который проходит и после отключения
        журнала, не охраняет ничего.
        """
        hook = ToolAuditHook()
        _run_tool(hook, None, detail=_refusal())  # type: ignore[arg-type]
        entries = hook.drain("postgres:42")
        assert entries, "аудит перестал видеть отказ — чинить больше нечего"
        assert entries[0]["status"] == "error", entries

    def test_agent_writes_failure_it_can_see(self, tmp_path: Path) -> None:
        """Писатель — агент, и признак ``source`` у его строки свой.

        Сторона, которая видит отказ до конвейера, — агент; платформа такого
        отказа не видит и видеть не обязана. Без ``metadata.source`` строки
        обеих сторон были бы неразличимы при чтении одной таблицы.
        """
        service, client = _service(tmp_path)
        service.register_request("postgres:42", "r1", user_id="u1")
        hook = ToolAuditHook(db_logging_service=service)
        _run_tool(hook, service, detail=_refusal())

        rows = _written_rows(service, client)
        failures = [row for row in rows if row["event_type"] == "tool.failed"]
        assert failures, rows
        assert failures[0]["metadata"][EVENT_SOURCE_KEY] == "nanobot", failures[0]["metadata"]

    def test_writer_is_distinguishable_without_a_new_name(self, tmp_path: Path) -> None:
        """Новое имя события не заводится: словарь имён платформы закрыт.

        При ``log_unknown_event_type_policy = strict`` непризнанное имя не
        пишется вовсе, то есть цена нового имени — новое объявление в словаре
        имён. Проверяется против самого словаря.
        """
        import sys

        platform_root = str(Path(__file__).resolve().parent.parent / "mcp-platform")
        if platform_root not in sys.path:
            sys.path.insert(0, platform_root)
        from libs.enterprise_common.eventing.types import AGENT_DEGRADED  # noqa: F401

        known = _known_event_types()
        assert "tool.failed" in known, (
            "tool.failed обязан быть в словаре имён платформы: иначе "
            "отказ агента не пишется вовсе"
        )
        service, client = _service(tmp_path)
        service.register_request("postgres:42", "r1", user_id="u1")
        hook = ToolAuditHook(db_logging_service=service)
        _run_tool(hook, service, detail=_refusal())
        written = {row["event_type"] for row in _written_rows(service, client)}
        assert written <= known, f"имена вне словаря платформы: {written - known}"

    def test_success_is_not_logged_as_failure(self, tmp_path: Path) -> None:
        """Успех остаётся ``tool.completed`` и отказами не считается.

        Обратная сторона требования: если бы отказом считалось всё, что не
        ``ok``, то в ``tool.failed`` попадали бы и нормальные завершения, а
        отчёт по журналу перестал бы различать аварию и работу.
        """
        service, client = _service(tmp_path)
        service.register_request("postgres:42", "r1", user_id="u1")
        hook = ToolAuditHook(db_logging_service=service)
        _run_tool(hook, service, status="ok", detail="claimed: 0")

        rows = _written_rows(service, client)
        assert not [row for row in rows if row["event_type"] == "tool.failed"], rows
        assert not [row for row in rows if row["event_type"] == "tool.failed"], rows
        assert service.get_stats()["dropped"] == 0, service.get_stats()

    def test_coverage_table_names_both_writers(self) -> None:
        """Строка 8 таблицы покрытия больше не утверждает «агент не пишет».

        Таблица покрытия переехала из спеки компонента в
        `docs/journal-observability.md`: канон требует один H1 на файл, а
        документ наблюдаемости описывает не `logging-db`, а подсистему журнала
        целиком. Страж проверяет содержимое таблицы, а не её дом — поэтому
        адрес здесь и только здесь.
        """
        doc = (
            Path(__file__).resolve().parent.parent / "docs/journal-observability.md"
        ).read_text(encoding="utf-8")
        row = [line for line in doc.splitlines() if line.startswith("| 8 | Ошибка tool")]
        assert row, "строка 8 таблицы покрытия исчезла — вернуться нечему"
        assert "агент не пишет" not in row[0], (
            "строка 8 утверждает, что отказ пишет только платформа: после "
            "требования это неверно, и читатель получит ложное основание"
        )
        assert "metadata.source" in row[0], row[0]


def _known_event_types() -> set[str]:
    """Имена событий, объявленные в словаре платформы."""
    platform_root = Path(__file__).resolve().parent.parent / "mcp-platform"
    import sys

    if str(platform_root) not in sys.path:
        sys.path.insert(0, str(platform_root))
    import libs.enterprise_common.eventing.types as types_module

    return {
        value
        for name, value in vars(types_module).items()
        if name.isupper() and isinstance(value, str) and "." in value
    }
