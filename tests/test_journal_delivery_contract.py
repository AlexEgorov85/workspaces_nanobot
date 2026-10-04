"""Событие агента не теряется молча.

Требование: ``lib/services/db_logging_service.py``,
``lib/services/log_transport.py``, ``lib/core/application_context.py``
(change ``2026-10-04-journal-observability-repair``, capability ``logging-db``).

Дефект, который страж держит: за прогон в таблице не было ни одной строки,
написанной агентом, при 169 отказах в консоли — и ничто об этом не говорило.
Баннер утверждал, что журнал пишется через ``log_events`` (не измеряя этого),
потери считались одним счётчиком без причины, а батч, отложенный до решения о
транспорте, обещал дождаться решения кодом, которого не было.

Ключевое различие требования: потеря не запрещена, запрещено её **молчание**.
Событие без полной личности подписать нельзя, и выдумывать личность — значит
записать в чужую сессию. Но такая потеря обязана быть **посчитана и названа**,
иначе пустая таблица рядом с растущим счётчиком выглядит как тишина.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from config import EXPECTED_RUNTIME_TABLE_NAMES

from lib.services.db_logging_service import DbLoggingService, LogEvent
from lib.services.enterprise_mcp_client import EnterpriseMcpUnavailable
from lib.services.log_transport import (
    LocalFallbackSink,
    McpLogWriter,
    event_to_wire,
    group_by_identity,
)

#: Имена таблиц берутся из конфигурации, а не зашиваются строкой: страж
#: ``test_no_hardcoded_table_names.py`` запрещает литерал, и запрет тут по
#: делу — переименование таблицы должно ломать сборку в одном месте.
_TABLES = EXPECTED_RUNTIME_TABLE_NAMES["test"]


def _run_sync(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)


class _AcceptingClient:
    """Клиент, у которого платформа принимает всё.

    Ответ повторяет форму реального ответа операции ``log_events``, иначе страж
    проверял бы структуру подмены, а не договорённость.
    """

    def __init__(self) -> None:
        self.batches: list[dict[str, Any]] = []
        self.identities: list[Any] = []

    async def call(
        self, operation: str, arguments: dict[str, Any] | None = None, *, identity: Any = None
    ) -> str:
        self.batches.append(dict(arguments or {}))
        self.identities.append(identity)
        events = (arguments or {}).get("events") or []
        return json.dumps({"status": "ok", "accepted": len(events), "dropped": 0})


class _RefusingClient:
    """Клиент, у которого платформа не отвечает вовсе."""

    def __init__(self) -> None:
        self.calls = 0

    async def call(self, *args: Any, **kwargs: Any) -> str:
        self.calls += 1
        raise EnterpriseMcpUnavailable("платформа недоступна")


def _service(
    tmp_path: Path,
    *,
    client: Any | None = None,
    trail: Path | None = None,
    transport_pending: bool = False,
) -> tuple[DbLoggingService, Any]:
    """Служба журнала с транспортом платформы и локальным следом.

    При ``transport_pending=True`` writer'а в службе НЕТ — ровно как в
    боевом старте (``ApplicationContext.start()`` подключает транспорт с
    ``mcp_writer=None``, пока живого loop ещё нет). Writer, подставленный
    «на всякий случай», снял бы ветку откладывания целиком, и страж проверил
    бы не то состояние, в котором батч откладывается.
    """
    sink_trail = trail if trail is not None else tmp_path / "fallback.jsonl"
    resolved = client if client is not None else _AcceptingClient()
    writer = McpLogWriter(call=resolved.call, run=_run_sync)
    sink = LocalFallbackSink(str(sink_trail))
    service = DbLoggingService(
        dsn="",
        table_name=_TABLES["gateway_logs"],
        question_runs_table=_TABLES["question_runs"],
        # Мелкий интервал сброса: страж ждёт доставки по событию, а не по
        # расписанию пятисекундного флаша.
        flush_interval_sec=0.05,
        mcp_writer=None if transport_pending else writer,
        fallback_sink=sink,
    )
    service.attach_transport(
        mcp_writer=None if transport_pending else writer,
        fallback_sink=sink,
        transport_pending=transport_pending,
    )
    return service, resolved


def _event(**overrides: Any) -> LogEvent:
    base = {
        "event_type": "agent.received",
        "session_id": "cli:1",
        "user_id": "u1",
        "request_id": "r1",
        "payload": {"phase": "turn"},
    }
    base.update(overrides)
    return LogEvent(**base)


class TestJournalDeliveryContract:
    """Принятое событие доходит до таблицы; непринятое — названо."""

    def test_accepted_event_reaches_the_table(self, tmp_path: Path) -> None:
        """Событие, принятое ``log_events``, дожидается строки в таблице.

        Проверяется сквозной путь: ``log_event`` → батч → ``log_events`` →
        счётчик ``written``. Разрывы между этими точками и были дефектом, и по
        одному только факту вызова их не увидеть.
        """
        service, _client = _service(tmp_path)
        service.register_request("cli:1", "r1", user_id="u1")
        service.start()
        try:
            service.log_event(_event())
            _wait_for(lambda: service.get_stats()["written"] == 1)
        finally:
            service.stop()

        stats = service.get_stats()
        assert stats["written"] == 1, (
            f"событие, принятое платформой, не дошло до таблицы: {stats}"
        )
        assert stats["batch_count"] == 1, stats
        assert stats["dropped"] == 0, stats

    def test_agent_side_events_are_not_silently_lost(self, tmp_path: Path) -> None:
        """События, которые пишет только агент, учтены — принятые или названные.

        Именно на этом наблюдении стоял дефект: 169 ``agent.degraded`` в консоли
        и ноль строк от агента в таблице. Сами эти события личности вызова не
        имеют (у ошибки опроса канала нет ни сессии, ни пользователя), и
        подписать их нечем. Требование поэтому не «доставьте», а «считайте и
        назовите»: расхождение допустимо ровно настолько, насколько потеря
        объяснена.
        """
        service, _client = _service(tmp_path)
        service.log_event(
            _event(
                event_type="agent.degraded",
                session_id=None,
                user_id=None,
                request_id=None,
                summary="поллинг не удался",
            )
        )
        service._flush_batch(_drain(service))

        stats = service.get_stats()
        assert stats["written"] == 0, (
            f"событие без личности подписано и ушло в таблицу: {stats}"
        )
        assert stats["dropped"] == 1, stats
        assert stats["fallback_written"] == 1, (
            f"событие не сохранено нигде — даже локально: {stats}"
        )
        reasons = stats["loss_reasons"]
        assert reasons, f"потеря посчитана, но не названа: {stats}"
        assert any("неполная личность" in name for name in reasons), reasons

    def test_unaccounted_event_is_reported(self, tmp_path: Path) -> None:
        """Каждое поставленное в очередь событие — принято или названо.

        Считается оно по-разному: ``queued`` минус доставленные минус отказ —
        и разница обязана совпасть с потерями по причинам. Счётчик без такой
        сверки растёт рядом с пустой таблицой и ничего не сообщает.
        """
        service, _client = _service(tmp_path)
        service.log_event(_event())
        service._flush_batch(_drain(service))
        stats = service.get_stats()
        lost = sum(int(v) for v in stats["loss_reasons"].values())
        assert stats["queued"] == 1, stats
        assert stats["written"] + lost == stats["queued"], (
            f"событие не принято и не названо потерянным: {stats}"
        )

    def test_failure_carries_a_reason(self, tmp_path: Path) -> None:
        """Потеря названа, а не посчитана: причина есть в статистике.

        Счётчик ``dropped`` без причины отвечает на «сколько» и не отвечает на
        «почему»; между «платформа отказала» и «нечем подписать вызов» разница
        принципиальна, и обе выглядят в журнале одинаково.
        """
        service, _client = _service(tmp_path, client=_RefusingClient())
        service.log_event(_event())
        service._flush_batch(_drain(service))

        stats = service.get_stats()
        assert stats["dropped"] == 1, stats
        reasons = stats["loss_reasons"]
        assert reasons, f"потеря не названа: {stats}"
        assert any("log_events" in name for name in reasons), reasons
        # Причина обязана быть видна и в итоге, который печатается один раз за
        # процесс: иначе её увидят только те, кто снимет get_stats().
        reported = _reported_line(service)
        assert "потери по причинам" in reported, reported

    def test_transport_line_is_measured_or_hedged(self) -> None:
        """Строка о транспорте не обещает то, чего не измеряла.

        Прежняя строка утверждала, что журнал пишется через ``log_events``, и её
        верили при нуле строк от агента. Допустимо только одно из двух: измерить
        доставку или сказать, что она измеряется счётчиками.
        """
        source = (Path(__file__).resolve().parent.parent / "lib/core/application_context.py")
        text = source.read_text(encoding="utf-8")
        assert "журнал агента пишется через enterprise-mcp" not in text, (
            "баннер обещает запись, не измеряя её — именно на него поверили "
            "при нуле строк в таблице"
        )
        assert "транспорт журнала агента подключён" in text, (
            "подключённый транспорт должен быть назван, иначе его ищут в логах"
        )

    def test_is_running_reflects_the_thread(self, tmp_path: Path) -> None:
        """``is_running()`` не остаётся правдой после смерти потока.

        Признак ``_running`` переживает падение worker'а, и тогда «журнал
        работает» означало бы, что батч уходит, хотя он копится в очереди.
        """
        service, _client = _service(tmp_path)
        assert service.is_running() is False, "до старта журнал не работает"
        service.start()
        try:
            assert service.is_running() is True
            # Поток убит извне — ровно то, что происходит при необработанном
            # исключении в цикле flush.
            if service._thread is not None:
                service._thread.join(timeout=0.0)
            service._thread = _DeadThread()
            assert service.is_running() is False, (
                "мёртвый worker-поток объявлен рабочим: потери будут копиться "
                "незамеченными"
            )
        finally:
            service._thread = None
            service._running = False

    def test_deferred_batch_is_delivered_or_declared(self, tmp_path: Path) -> None:
        """Отложенный батч либо доставляется, либо объявляется брошенным.

        Обещание «дожидается решения» было невыполнимым: кода, читающего
        отложенное обратно, не существовало, и батч ждал ровно до конца
        процесса. Теперь решение есть — ``attach_transport`` отдаёт батч.
        """
        service, _client = _service(tmp_path, transport_pending=True)
        service.log_event(_event())
        service._flush_batch(_drain(service))
        assert service.get_stats()["written"] == 0, "до решения о транспорте доставки нет"

        client = _AcceptingClient()
        service.attach_transport(
            mcp_writer=McpLogWriter(call=client.call, run=_run_sync),
            fallback_sink=service._fallback_sink,
            transport_pending=False,
        )
        assert service.get_stats()["written"] == 1, (
            "отложенный батч не доставлен после решения о транспорте: "
            f"{service.get_stats()}"
        )

    def test_defer_docstring_matches_behavior(self, tmp_path: Path) -> None:
        """Оставшийся отложенный батч объявляется брошенным — и это видно.

        Сценарий «транспорт не выбрали до остановки» обязан быть назван: иначе
        после ``stop()`` отчёт показывает потерю без причины, и это тот же
        дефект, только на выходе.
        """
        service, _client = _service(tmp_path, transport_pending=True)
        service.log_event(_event())
        service._flush_batch(_drain(service))
        service.start()
        service.stop()

        stats = service.get_stats()
        reasons = stats["loss_reasons"]
        assert any("не выбран" in name for name in reasons), (
            f"брошенный батч не объявлен: {reasons}"
        )
        assert stats["written"] == 0
        assert stats["dropped"] == 1, stats

    def test_fallback_path_is_absolute(self, tmp_path: Path) -> None:
        """Путь следа абсолютен: иначе его не найти при другом каталоге."""
        service, _client = _service(tmp_path)
        path = service.get_stats()["fallback_path"]
        assert path, "служба не сообщает, куда пишет локальный след"
        assert Path(path).is_absolute(), path

    def test_reported_path_is_the_written_path(self, tmp_path: Path) -> None:
        """Сообщаемый путь — тот же файл, куда пишет след.

        Смена текущего каталога не должна менять смысл уже написанного: путь,
        который «относительно того, откуда запустили», после смены каталога
        указывает на другой файл или ни на какой.
        """
        trail = tmp_path / "fallback.jsonl"
        service, _client = _service(tmp_path, client=_RefusingClient(), trail=trail)
        service.log_event(_event())
        service._flush_batch(_drain(service))

        import os

        original = os.getcwd()
        os.chdir(tmp_path.parent)
        try:
            reported = Path(service.get_stats()["fallback_path"])
            assert reported == trail.resolve(), (
                f"служба сообщает {reported}, а пишет {trail.resolve()}"
            )
        finally:
            os.chdir(original)


class _DeadThread:
    """Поток, который не жив: подмена ради проверки честности ``is_running``."""

    def is_alive(self) -> bool:
        return False

    def join(self, timeout: float | None = None) -> None:
        return None


def _drain(service: DbLoggingService) -> list[LogEvent]:
    """Снять события с очереди журнала, минуя worker-поток.

    Worker поднимается отдельным потоком, а стражу нужен один батч, отправленный
    в известный момент: иначе проверка ждала бы таймера и зависела бы от
    ``flush_interval``, то есть проверяла бы расписание, а не доставку.
    """
    events: list[LogEvent] = []
    while True:
        try:
            item = service._queue.get_nowait()
        except Exception:
            return events
        if isinstance(item, LogEvent):
            events.append(item)


def _wait_for(predicate: Any, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("условие не наступило за отведённое время")


def _reported_line(service: DbLoggingService) -> str:
    """Строка итога, которую печатает ``report_stats``."""
    from loguru import logger

    sink: list[str] = []
    handler_id = logger.add(lambda message: sink.append(str(message)), level="DEBUG")
    try:
        service.report_stats()
    finally:
        logger.remove(handler_id)
    return "\n".join(sink)


def test_agent_event_wire_shape_matches_the_platform_contract() -> None:
    """Кадр агента имеет ту форму, которую ``log_events`` объявила.

    Объявленная схема операции стала исполняемой, поэтому кадр, который агент
    реально кладёт в батч, обязан проходить её проверку. Проверка на стороне
    агента: платформа не импортирует агентский код, и граница запрещена.
    """
    import jsonschema

    schema_path = (
        Path(__file__).resolve().parent.parent
        / "mcp-platform/servers/enterprise/capabilities/data/tools/log_events.py"
    )
    namespace: dict[str, Any] = {}
    source = schema_path.read_text(encoding="utf-8")
    exec(  # noqa: S102 - файл операции платформы, читаем его объявление
        compile(source[source.index("_EVENT_ITEM") :], str(schema_path), "exec"),
        namespace,
    )
    batch = [event_to_wire(_event())]
    jsonschema.validate(
        instance={"events": batch, "session_id": "cli:1", "user_id": "u1", "request_id": "r1"},
        schema={
            "type": "object",
            "properties": {"events": namespace["_EVENT_ITEM"] and {"type": "array", "items": namespace["_EVENT_ITEM"]}},
        },
    )


def test_grouping_requires_identity_the_agent_cannot_invent() -> None:
    """Неполная личность образует отдельную группу — и это видно.

    Событие без ``session_id``/``user_id`` нельзя подписать вызовом, а выдумать
    личность значит записать в чужую сессию. Развилка «принято или названо»
    обязана быть явной, иначе потеря растворяется в группе с чужой личностью.
    """
    groups = group_by_identity([_event(), _event(session_id=None, user_id=None)])
    complete = [group for group in groups if group.key.complete()]
    incomplete = [group for group in groups if not group.key.complete()]
    assert len(complete) == 1 and len(complete[0].events) == 1, groups
    assert len(incomplete) == 1 and len(incomplete[0].events) == 1, groups
