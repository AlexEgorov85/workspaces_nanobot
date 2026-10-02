"""Транспорт записи журнала: группировка по личности, отказы, fallback (фаза 7).

Тесты проверяют не «вызов произошёл», а решения, ради которых фаза и
затевалась:

* **смешанный батч разбивается по личности.** Операция ``log_events`` берёт
  ``session_id``/``user_id``/``request_id`` из контекста вызова, а не из тела
  батча. Один вызов на смешанный батч проставил бы всем событиям личность
  одного оборота — то есть записал бы чужое событие в чужую сессию.
* **событие без личности не подписывается догадкой.** Подставить
  ``user_id`` «на всякий случай» — значит приписать событие чужому
  пользователю. Оно уходит в fallback и в счётчик потерь.
* **недоступный сервер не блокирует ход и виден в счётчике** (приёмка 7.6).
* **тело батча не несёт личности.** Иначе у вызова появляется второй
  источник идентичности, и журнал может описать не тот вызов.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from lib.services.db_logging_service import DbLoggingService, LogEvent
from lib.services.enterprise_mcp_client import (
    CallIdentity,
    EnterpriseMcpUnavailable,
)
from lib.services.log_transport import (
    LocalFallbackSink,
    LogWriteUnavailable,
    LoopCallRunner,
    McpLogWriter,
    WriteResult,
    event_to_wire,
    group_by_identity,
)


class RecordingClient:
    """Клиент-заглушка: помнит вызовы и отвечает заданным текстом."""

    def __init__(self, response: str = '{"status": "ok", "accepted": 1, "dropped": 0}') -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, Any], Any]] = []
        self.fail_with: Exception | None = None

    async def call(
        self,
        operation: str,
        arguments: dict[str, Any] | None = None,
        *,
        identity: CallIdentity | None = None,
    ) -> str:
        self.calls.append((operation, dict(arguments or {}), identity))
        if self.fail_with is not None:
            raise self.fail_with
        if operation == "log_events":
            count = len((arguments or {}).get("events", []))
            return json.dumps(
                {"status": "ok", "accepted": count, "dropped": 0}
            )
        return self.response


class SyncRunner:
    """Запускает корутину до конца — для тестов без живого loop."""

    def __call__(self, coro: Any) -> Any:
        return asyncio.run(coro)


def _event(**kwargs: Any) -> LogEvent:
    base = {
        "event_type": "tool_call",
        "session_id": "s1",
        "user_id": "u1",
        "request_id": "r1",
    }
    base.update(kwargs)
    return LogEvent(**base)


class TestGrouping:
    """Батч делится на группы по личности вызова, порядок сохраняется."""

    def test_same_identity_shares_one_call(self) -> None:
        events = [
            _event(event_type="tool_call"),
            _event(event_type="tool_result"),
        ]
        client = RecordingClient()
        writer = McpLogWriter(call=client.call, run=SyncRunner())

        result = writer.write_events(events)

        assert result == WriteResult(accepted=2, dropped=0)
        assert len(client.calls) == 1, "одна личность — один вызов"

    def test_mixed_batch_is_split_per_identity(self) -> None:
        """Смешанный батч одним вызовом проставил бы всем событиям одну личность."""
        events = [
            _event(event_type="a", session_id="s1", user_id="u1", request_id="r1"),
            _event(event_type="b", session_id="s1", user_id="u1", request_id="r1"),
            _event(event_type="c", session_id="s2", user_id="u2", request_id="r2"),
        ]
        client = RecordingClient()
        writer = McpLogWriter(call=client.call, run=SyncRunner())

        result = writer.write_events(events)

        assert len(client.calls) == 2, "разные личности — разные вызовы"
        assert result.accepted == 3
        identities = [call[2] for call in client.calls]
        assert [(i.session_id, i.user_id) for i in identities] == [
            ("s1", "u1"),
            ("s2", "u2"),
        ]

    def test_group_preserves_order_of_first_appearance(self) -> None:
        """Перестановка батча переставила бы события оборота по времени."""
        events = [
            _event(event_type="first", session_id="s1", user_id="u1"),
            _event(event_type="second", session_id="s2", user_id="u2"),
            _event(event_type="third", session_id="s1", user_id="u1"),
        ]
        groups = group_by_identity(events)

        assert [[e.event_type for e in g.events] for g in groups] == [
            ["first", "third"],
            ["second"],
        ]

    def test_request_id_difference_splits_groups(self) -> None:
        """Один и тот же пользователь в двух оборотах — два вызова."""
        events = [
            _event(session_id="s1", user_id="u1", request_id="r1"),
            _event(session_id="s1", user_id="u1", request_id="r2"),
        ]
        groups = group_by_identity(events)
        assert len(groups) == 2

    def test_missing_request_id_still_signable(self) -> None:
        """``request_id`` клиент дополняет сам — его отсутствие не дефект."""
        events = [_event(request_id=None)]
        client = RecordingClient()
        writer = McpLogWriter(call=client.call, run=SyncRunner())

        result = writer.write_events(events)

        assert result.accepted == 1
        assert client.calls[0][2].request_id is None


class TestUnidentifiedEvents:
    """Без ``session_id``/``user_id`` подписанного вызова не существует."""

    def test_event_without_identity_is_not_signed(self) -> None:
        events = [_event(session_id=None, user_id=None, request_id=None)]
        client = RecordingClient()
        writer = McpLogWriter(call=client.call, run=SyncRunner())

        result = writer.write_events(events)

        assert result.accepted == 0
        assert result.dropped == 1, "потеря обязана быть посчитана"
        assert client.calls == [], "вызова без личности быть не может"

    def test_missing_user_id_is_enough_to_block_the_call(self) -> None:
        """Канальные события приходят с ``session_id`` и без ``user_id``."""
        events = [_event(session_id="s1", user_id=None)]
        client = RecordingClient()
        writer = McpLogWriter(call=client.call, run=SyncRunner())

        result = writer.write_events(events)

        assert result.dropped == 1
        assert client.calls == []

    def test_unidentified_goes_to_fallback_callback(self) -> None:
        captured: list[Any] = []
        client = RecordingClient()
        writer = McpLogWriter(
            call=client.call, run=SyncRunner(), on_fallback=captured.extend
        )

        writer.write_events([_event(session_id=None, user_id=None)])

        assert len(captured) == 1
        assert captured[0].event_type == "tool_call"

    def test_identified_goes_to_platform_not_to_fallback(self) -> None:
        captured: list[Any] = []
        client = RecordingClient()
        writer = McpLogWriter(
            call=client.call, run=SyncRunner(), on_fallback=captured.extend
        )

        writer.write_events([_event()])

        assert captured == []


class TestWireFormat:
    """Тело батча не несёт личности и не содержит лишних полей."""

    def test_identity_is_not_in_the_event_body(self) -> None:
        """Второй источник идентичности у вызова означал бы расхождение журнала."""
        wire = event_to_wire(_event())
        for key in ("session_id", "user_id", "request_id"):
            assert key not in wire, f"{key} в теле батча — второй источник личности"

    def test_documented_fields_present(self) -> None:
        wire = event_to_wire(_event())
        assert set(wire) == {
            "id",
            "event_type",
            "name",
            "level",
            "summary",
            "payload",
            "metadata",
            "channel",
            "actor",
        }
        assert wire["event_type"] == "tool_call"

    def test_absent_optional_fields_become_empty_not_none_for_text(self) -> None:
        wire = event_to_wire(_event(name=None, summary=None, channel=None))
        assert wire["name"] == ""
        assert wire["summary"] == ""
        assert wire["channel"] is None


class TestCounters:
    """Счётчики ответа платформы попадают в результат без искажений."""

    def test_server_reported_drops_are_visible(self) -> None:
        async def call(operation, arguments=None, *, identity=None):
            return json.dumps({"status": "ok", "accepted": 7, "dropped": 3})

        writer = McpLogWriter(call=call, run=SyncRunner())
        result = writer.write_events([_event()] * 10)

        assert result == WriteResult(accepted=7, dropped=3)

    def test_unparsable_answer_is_accepted_not_silently_zero(self) -> None:
        """Запись уже исполнилась: «не знаю» не значит «ничего не записано»."""

        async def call(operation, arguments=None, *, identity=None):
            return "не json"

        writer = McpLogWriter(call=call, run=SyncRunner())
        result = writer.write_events([_event(), _event()])

        assert result.accepted == 2
        assert result.dropped == 0


class TestUnavailablePlatform:
    """Приёмка 7.6: остановленный сервер не блокирует ход."""

    def test_client_unavailable_counts_as_dropped(self) -> None:
        client = RecordingClient()
        client.fail_with = EnterpriseMcpUnavailable("сервер не поднялся")
        writer = McpLogWriter(call=client.call, run=SyncRunner())

        result = writer.write_events([_event(), _event(), _event()])

        assert result.dropped == 3
        assert result.accepted == 0

    def test_one_failed_group_does_not_cancel_the_others(self) -> None:
        """Падение одного оборота не должно уносить журнал остальных."""

        async def call(operation, arguments=None, *, identity=None):
            if identity.session_id == "s_bad":
                raise EnterpriseMcpUnavailable("упал")
            return json.dumps({"status": "ok", "accepted": 1, "dropped": 0})

        writer = McpLogWriter(call=call, run=SyncRunner())
        result = writer.write_events(
            [
                _event(session_id="s_bad", user_id="u1", request_id="r1"),
                _event(session_id="s_ok", user_id="u1", request_id="r1"),
            ]
        )

        assert result.accepted == 1
        assert result.dropped == 1

    def test_domain_error_is_not_swallowed_silently(self) -> None:
        """Отказ операции — дефект на стороне агента, а не потеря батча."""

        async def call(operation, arguments=None, *, identity=None):
            raise ValueError("events[0].event_type не должен быть пустым")

        writer = McpLogWriter(call=call, run=SyncRunner())

        with pytest.raises(ValueError):
            writer.write_events([_event()])

    def test_unavailable_transport_reaches_fallback_too(self) -> None:
        """Приёмка 7.3: отказ транспорта обязан оставить локальный след.

        Счётчик потерь без файла умирает вместе с процессом: расследование
        отказа начинается ровно с того, чего не случилось.
        """
        client = RecordingClient()
        client.fail_with = EnterpriseMcpUnavailable("сервер не поднялся")
        seen: list[list[Any]] = []
        writer = McpLogWriter(
            call=client.call,
            run=SyncRunner(),
            on_fallback=lambda events: seen.append(list(events)),
        )

        result = writer.write_events([_event(), _event()])

        assert result.dropped == 2, "до журнала платформы не дошло — потеря считается"
        assert len(seen) == 1 and len(seen[0]) == 2, (
            "но след остаётся: файл должен пережить перезапуск процесса"
        )


class TestLoopCallRunner:
    """Мост из потока worker'а в loop агента."""

    def test_runs_coroutine_on_live_loop(self) -> None:
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        try:
            runner = LoopCallRunner(loop=loop, timeout_sec=5.0)

            async def work() -> str:
                return "готово"

            assert runner(work()) == "готово"
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)
            loop.close()

    def test_dead_loop_becomes_unavailable_not_a_hang(self) -> None:
        """Висящий worker остановил бы и ``stop()`` — потеря обязана быть счётчиком."""
        loop = asyncio.new_event_loop()
        loop.close()
        runner = LoopCallRunner(loop=loop, timeout_sec=1.0)

        async def work() -> str:
            return "готово"

        coro = work()
        with pytest.raises(LogWriteUnavailable):
            runner(coro)
        coro.close()

    def test_timeout_is_reported_as_unavailable(self) -> None:
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()

        async def slow() -> str:
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                # Отмена по таймауту runner'а — ожидаемый исход, а не ошибка
                # теста: без неё loop при остановке ругается «Task was
                # destroyed but it is pending», и за этим шумом не видно
                # результата проверки.
                raise
            return "не дождались"

        try:
            runner = LoopCallRunner(loop=loop, timeout_sec=0.05)
            with pytest.raises(LogWriteUnavailable):
                runner(slow())
            # Отмена доставляется в loop через call_soon_threadsafe, и
            # обрабатывается его потоком. Без паузы loop останавливается
            # раньше, и asyncio печатает «Task was destroyed but it is
            # pending» уже после зелёного результата — то есть шумом,
            # в котором не видно, упал ли тест на самом деле.
            time.sleep(0.2)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)
            loop.close()


class TestLocalFallback:
    """Файл на случай недоступной платформы: петля не замыкается."""

    def test_writes_one_json_line_per_event(self, tmp_path: Path) -> None:
        sink = LocalFallbackSink(str(tmp_path / "fallback.jsonl"))
        sink.write([_event(event_type="a"), _event(event_type="b")])

        lines = (tmp_path / "fallback.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["event_type"] == "a"

    def test_identity_is_kept_in_the_fallback_record(self) -> None:
        """Событие без личности должно остаться опознаваемым при разборе."""
        sink_path = Path(str(Path.cwd() / "_fb_test.jsonl"))
        try:
            sink = LocalFallbackSink(str(sink_path))
            sink.write([_event(session_id="s1", user_id="u1", request_id="r1")])
            record = json.loads(sink_path.read_text(encoding="utf-8").splitlines()[0])
            assert record["session_id"] == "s1"
            assert record["user_id"] == "u1"
            assert record["request_id"] == "r1"
        finally:
            if sink_path.exists():
                sink_path.unlink()

    def test_size_limit_stops_writing_and_counts_drops(self, tmp_path: Path) -> None:
        target = tmp_path / "fallback.jsonl"
        target.write_text("x" * 100, encoding="utf-8")
        sink = LocalFallbackSink(str(target), max_bytes=10)

        sink.write([_event()])

        assert sink.stats()["dropped"] == 1
        assert target.read_text(encoding="utf-8") == "x" * 100, "файл не растёт"

    def test_unwritable_path_counts_drops_instead_of_raising(self, tmp_path: Path) -> None:
        """Отказ локального следа не должен ронять flush."""
        sink = LocalFallbackSink(str(tmp_path / "нет" / "нет" / "fallback.jsonl"))
        sink.write([_event()])
        assert sink.stats()["dropped"] == 1


class TestServiceWiring:
    """``DbLoggingService`` выбирает транспорт при сборке, а не по факту сбоя."""

    def _service(self, writer: Any, sink: Any = None) -> DbLoggingService:
        # Имена таблиц здесь — заглушки, а не реальные имена проекта: при
        # MCP-транспорте они не используются вовсе (операция ``log_events``
        # сама знает, куда писать), и страж
        # ``test_no_hardcoded_table_names`` справедливо запрещает зашивать их
        # в тест. Второе объявление имени здесь было бы ровно тем расхождением,
        # которое страж и ловит: переименование таблицы тихо оставило бы тест
        # проверять старое имя.
        return DbLoggingService(
            dsn="",
            table_name="journal_events",
            question_runs_table="journal_runs",
            mcp_writer=writer,
            fallback_sink=sink,
        )

    def test_stats_start_with_zero_loss_counters(self) -> None:
        service = self._service(McpLogWriter(call=RecordingClient().call, run=SyncRunner()))
        stats = service.get_stats()
        assert stats["dropped"] == 0
        assert stats["fallback_written"] == 0

    def test_mcp_path_does_not_touch_postgres(self) -> None:
        """При MCP-транспорте пул записи не используется вовсе."""
        client = RecordingClient()
        service = self._service(McpLogWriter(call=client.call, run=SyncRunner()))

        service._flush_batch([_event(), _event()])

        assert len(client.calls) == 1
        stats = service.get_stats()
        assert stats["written"] == 2
        assert stats["dropped"] == 0

    def test_unavailable_server_grows_loss_counter(self) -> None:
        client = RecordingClient()
        client.fail_with = EnterpriseMcpUnavailable("остановлен")
        service = self._service(McpLogWriter(call=client.call, run=SyncRunner()))

        service._flush_batch([_event(), _event(), _event()])

        stats = service.get_stats()
        assert stats["dropped"] == 3, "приёмка 7.6: счётчик потерь растёт"
        assert stats["written"] == 0
        assert stats["connected"] is False

    def test_unidentified_events_reach_fallback_and_counters(self) -> None:
        sink = LocalFallbackSink(str(Path.cwd() / "_fb_wiring.jsonl"))
        client = RecordingClient()
        writer = McpLogWriter(call=client.call, run=SyncRunner())
        service = self._service(writer, sink)
        try:
            service._flush_batch(
                [_event(session_id=None, user_id=None), _event(session_id="s", user_id="u")]
            )
            stats = service.get_stats()
            assert stats["fallback_written"] == 1
            assert stats["written"] == 1
        finally:
            path = Path(sink.path)
            if path.exists():
                path.unlink()

    def test_unavailable_server_still_leaves_a_local_trail(self, tmp_path: Path) -> None:
        """Приёмка 7.3: остановленный сервер пишет файл, а не только счётчик."""
        path = tmp_path / "fallback.jsonl"
        sink = LocalFallbackSink(str(path))
        client = RecordingClient()
        client.fail_with = EnterpriseMcpUnavailable("остановлен")
        writer = McpLogWriter(call=client.call, run=SyncRunner())
        service = self._service(writer, sink)

        service._flush_batch([_event(), _event(), _event()])

        stats = service.get_stats()
        assert stats["dropped"] == 3, "потери считаются независимо от следа"
        assert stats["fallback_written"] == 3, "и след остаётся независимо от потерь"
        assert stats["written"] == 0
        lines = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        assert len(lines) == 3, "файл непуст — отправная точка расследования есть"
        assert {line["event_type"] for line in lines} == {"tool_call"}

    def test_question_run_goes_through_its_own_operation(self) -> None:
        """Контекст вопроса пишется не событием журнала, а отдельной операцией."""
        client = RecordingClient()
        service = self._service(McpLogWriter(call=client.call, run=SyncRunner()))

        service.register_request("s1", "r1", user_id="u1", question="вопрос")

        # ``register_request`` кладёт запись в очередь; обрабатывает её worker.
        record = service._queue.get_nowait()
        service._handle_question_run(record)

        operations = [call[0] for call in client.calls]
        assert operations == ["upsert_question_run"]
        assert client.calls[0][2].request_id == "r1"
        assert "request_id" not in client.calls[0][1], (
            "request_id берётся из контекста вызова, а не из аргументов"
        )

    def test_question_run_without_identity_is_skipped_not_faked(self) -> None:
        client = RecordingClient()
        service = self._service(McpLogWriter(call=client.call, run=SyncRunner()))

        service.register_request("s1", "r1", user_id=None)
        record = service._queue.get_nowait()
        service._handle_question_run(record)

        assert client.calls == [], "подписать вызов нечем — выдумывать нельзя"


class SlowFailingClient:
    """Клиент, который и падает, и делает это не мгновенно.

    Нужен для проверки границы «источник не ждёт транспорт»: пока вызов висит,
    очередь наполняется и должна обрезаться дропом, а не блокировать продюсера.
    """

    def __init__(self, delay: float = 0.5) -> None:
        self.delay = delay
        self.calls = 0

    async def call(self, operation, arguments=None, *, identity=None):
        self.calls += 1
        time.sleep(self.delay)
        raise EnterpriseMcpUnavailable("остановлен")


class TestBatchedAsyncFlush:
    """Приёмка 7.2: батчевый асинхронный flush в ``log_events``."""

    def test_batch_leaves_in_one_call_and_producer_does_not_wait(self) -> None:
        client = RecordingClient()
        writer = McpLogWriter(call=client.call, run=SyncRunner())
        # ``flush_interval_sec`` намеренно велик: флаш может случиться только
        # на стопе, поэтому утверждения ниже не зависят от гонки с таймером.
        service = DbLoggingService(
            dsn="",
            table_name="journal_events",
            question_runs_table="journal_runs",
            mcp_writer=writer,
            flush_interval_sec=30.0,
            batch_size=100,
        )
        service.start()
        try:
            for _ in range(5):
                assert service.log_event(_event()) is True
            assert client.calls == [], (
                "log_event кладёт событие в очередь и возвращает: ход агента "
                "не ждёт сети"
            )
        finally:
            service.stop(timeout_sec=5)

        assert len(client.calls) == 1, "батч уходит одним вызовом, а не пятью"
        assert len(client.calls[0][1]["events"]) == 5
        assert service.get_stats()["written"] == 5

    def test_event_survives_a_restart_of_the_transport(self) -> None:
        """Первый вызов падает, второй проходит: событие не теряется молча."""
        state = {"fail": True}

        async def call(operation, arguments=None, *, identity=None):
            if state["fail"]:
                raise EnterpriseMcpUnavailable("остановлен")
            count = len((arguments or {}).get("events", []))
            return json.dumps({"status": "ok", "accepted": count, "dropped": 0})

        writer = McpLogWriter(call=call, run=SyncRunner())

        lost = writer.write_events([_event()])
        state["fail"] = False
        ok = writer.write_events([_event()])

        assert lost.dropped == 1 and lost.accepted == 0
        assert ok.accepted == 1 and ok.dropped == 0


class TestPlatformOutage:
    """Приёмки 7.1 и 7.6: буфер ограничен, а ход агтора не блокируется."""

    def _service(self, client: Any, sink: Any = None, **kwargs: Any) -> DbLoggingService:
        return DbLoggingService(
            dsn="",
            table_name="journal_events",
            question_runs_table="journal_runs",
            mcp_writer=McpLogWriter(call=client.call, run=SyncRunner()),
            fallback_sink=sink,
            purge_interval_sec=0.0,
            **kwargs,
        )

    def test_buffer_drops_instead_of_blocking_the_turn(self) -> None:
        """7.1: очередь ограничена, переполнение считается, продюсер жив."""
        service = self._service(
            SlowFailingClient(delay=0.5), batch_size=1, queue_maxsize=2
        )
        service.start()
        try:
            accepted = [service.log_event(_event()) for _ in range(10)]
        finally:
            service.stop(timeout_sec=10)

        assert accepted.count(False) > 0, "переполненная очередь обязана отказывать"
        stats = service.get_stats()
        assert stats["queue_full"] == accepted.count(False), "дроп измерим"
        assert stats["running"] is False, "остановка не ждёт сети"

    def test_outage_leaves_a_trail_that_outlives_the_process(self, tmp_path: Path) -> None:
        """7.3 + 7.6: недоступность платформы не блокирует ход и не стирает след."""
        path = tmp_path / "trail.jsonl"
        client = RecordingClient()
        client.fail_with = EnterpriseMcpUnavailable("остановлен")
        service = self._service(
            client, LocalFallbackSink(str(path)), flush_interval_sec=0.05
        )
        service.start()
        try:
            for _ in range(5):
                assert service.log_event(_event()) is True
        finally:
            service.stop(timeout_sec=10)

        stats = service.get_stats()
        assert stats["written"] == 0
        assert stats["dropped"] == 5
        assert stats["fallback_written"] == 5

        # След лежит в файле, а не в памяти процесса: его читает кто угодно
        # после перезапуска, и новый sink на том же пути дописывает, а не
        # затирает.
        restarted = LocalFallbackSink(str(path))
        restarted.write([_event()])
        lines = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        assert len(lines) == 6, "новый процесс дописывает след, а не затирает его"
        assert [line["request_id"] for line in lines] == ["r1"] * 6
