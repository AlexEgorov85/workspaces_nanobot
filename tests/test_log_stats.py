"""Статистика журнала видна оператору, а шина не молчит об отказах.

Две связанные проблемы одного корня — «потеря без следа»:

* ``DbLoggingService.get_stats()`` вне тестов никто не звал. Счётчики росли
  (и врали: запись, которой не было, попадала в ``question_runs``), но
  оператор узнавал о потере только по пустой таблице — то есть тогда, когда
  чинить уже поздно. Теперь итог печатается один раз за процесс, при остановке,
  а потери поднимаются до WARNING.
* ``db_logging_bus.make_inbound_logger`` глотал любое исключение через
  ``except Exception: pass``. Падение ``register_request`` убивало и связку
  вопроса с ``request_id``, и само событие входящего — без единой записи в
  логе. Теперь отказ назван и посчитан; публикацию сообщения он по-прежнему не
  роняет.

Там же — решение про ``actor`` входящего из очереди: у ``channels.postgres`` на
том конце producer, а не человек, и подставлять «user» без свидетельства
значит записать в журнал то, чего не было.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest

from lib.services.db_logging_bus import make_inbound_logger, make_outbound_logger
from lib.services.db_logging_service import DbLoggingService, LogEvent
from lib.services.enterprise_mcp_client import CallIdentity, EnterpriseMcpUnavailable
from lib.services.log_transport import McpLogWriter, group_by_identity

SERVICE_LOGGER = "lib.services.db_logging_service"
BUS_LOGGER = "lib.services.db_logging_bus"


class RecordingClient:
    def __init__(self) -> None:
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
            return json.dumps({"status": "ok", "accepted": count, "dropped": 0})
        return '{"status": "ok"}'


class SyncRunner:
    def __call__(self, coro: Any) -> Any:
        return asyncio.run(coro)


def _service(**kwargs: Any) -> DbLoggingService:
    client = kwargs.pop("client", None) or RecordingClient()
    writer = McpLogWriter(call=client.call, run=SyncRunner())
    # Имена таблиц — заглушки: при MCP-транспорте они не используются.
    return DbLoggingService(
        dsn="",
        table_name="journal_events",
        question_runs_table="journal_runs",
        mcp_writer=writer,
        **kwargs,
    )


class _FakeInbound:
    """Входящее сообщение очереди: те же поля, что и ``InboundMessage``."""

    def __init__(self, **kwargs: Any) -> None:
        self.channel = kwargs.get("channel", "postgres")
        self.session_key = kwargs.get(
            "session_key", f"{self.channel}:queue_probe"
        )
        self.sender_id = kwargs.get("sender_id", "queue-owner")
        self.chat_id = kwargs.get("chat_id", "queue_probe")
        self.content = kwargs.get("content", "посчитай строки")
        self.media = kwargs.get("media", [])
        self.metadata = kwargs.get("metadata", {"message_id": "msg-1"})


class _FailingInboundService(DbLoggingService):
    """Сервис, у которого ``register_request`` падает."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(
            dsn="",
            table_name="journal_events",
            question_runs_table="journal_runs",
            **kwargs,
        )
        self.register_calls = 0

    def register_request(self, *args: Any, **kwargs: Any) -> bool:
        self.register_calls += 1
        raise RuntimeError("очередь недоступна")


class TestStatsAreVisible:
    """Итог журнала должен доходить до оператора."""

    def test_stop_reports_stats_once(self, caplog: Any) -> None:
        service = _service()
        service.start()
        with caplog.at_level(logging.INFO, logger=SERVICE_LOGGER):
            service.stop()
            service.stop()

        reports = [r for r in caplog.records if "журнал агента:" in r.getMessage()]
        assert len(reports) == 1, "одна строка итога за жизнь процесса, не на каждый stop()"
        message = reports[0].getMessage()
        assert "контекстов вопроса записано" in message

    def test_losses_are_reported_as_warning(self, caplog: Any) -> None:
        """Потеря — повод поднять уровень, а не строка среди INFO."""
        client = RecordingClient()
        client.fail_with = EnterpriseMcpUnavailable("сервер не поднялся")
        service = _service(client=client)
        service.register_request("postgres:probe", "r1", user_id="u1")
        service._flush_batch([LogEvent(
            event_type="tool.started", session_id="postgres:probe", user_id="u1",
            request_id="r1",
        )])
        service.start()
        with caplog.at_level(logging.INFO, logger=SERVICE_LOGGER):
            service.stop()

        reports = [r for r in caplog.records if "журнал агента:" in r.getMessage()]
        assert reports, "итог не напечатан"
        assert reports[0].levelno == logging.WARNING

    def test_clean_run_stays_quiet(self, caplog: Any) -> None:
        service = _service()
        with caplog.at_level(logging.INFO, logger=SERVICE_LOGGER):
            service.report_stats()

        reports = [r for r in caplog.records if "журнал агента:" in r.getMessage()]
        assert reports and reports[0].levelno == logging.INFO

    def test_report_returns_the_snapshot_it_printed(self) -> None:
        service = _service()
        snapshot = service.report_stats()
        assert snapshot["written"] == service.get_stats()["written"]
        assert "question_runs_skipped" in snapshot

    def test_loss_counters_are_part_of_the_snapshot(self) -> None:
        """Счётчик, которого нет в отчёте, — потеря, о которой не узнают."""
        service = _service()
        snapshot = service.report_stats()
        for key in ("written", "dropped", "failed", "question_runs",
                    "question_runs_skipped", "question_runs_failed",
                    "registration_failures", "queue_full"):
            assert key in snapshot, f"в итоге нет {key!r}"


class TestBusDoesNotSwallow:
    """Шина перехватывает отказ, но не прячет его."""

    def test_failed_registration_is_named_and_counted(self, caplog: Any) -> None:
        service = _FailingInboundService()
        with caplog.at_level(logging.WARNING, logger=BUS_LOGGER):
            asyncio.run(make_inbound_logger(service)(_FakeInbound()))

        assert service.register_calls == 1
        messages = "\n".join(r.getMessage() for r in caplog.records)
        assert "не зарегистрирован" in messages
        assert "очередь недоступна" in messages, "причина обязана быть названа"
        stats = service.get_stats()
        assert stats["registration_failures"] == 1
        assert stats["last_error"] is not None

    def test_failed_registration_does_not_kill_the_inbound(self, caplog: Any) -> None:
        """Событие входящего важнее привязки к прогону — оно пишется в любом случае."""
        service = _FailingInboundService()
        with caplog.at_level(logging.WARNING, logger=BUS_LOGGER):
            asyncio.run(make_inbound_logger(service)(_FakeInbound()))

        inbound = [e for e in service._queue.queue if isinstance(e, LogEvent)]
        assert [e.event_type for e in inbound] == ["agent.received"], (
            "падение регистрации не должно уносить с собой событие входящего"
        )

    def test_registration_failure_never_breaks_publication(self, caplog: Any) -> None:
        """Ни один отказ логгера не имеет права уронить публикацию сообщения."""

        class _Exploding:
            def __getattr__(self, name: str) -> Any:
                raise RuntimeError(f"не читается: {name}")

        with caplog.at_level(logging.WARNING, logger=BUS_LOGGER):
            asyncio.run(make_inbound_logger(_Exploding())(_FakeInbound()))

    def test_broken_inbound_logging_is_not_silent(self, caplog: Any) -> None:
        class _NoLogger:
            def register_request(self, *a: Any, **k: Any) -> bool:
                return True

            def log_inbound(self, *a: Any, **k: Any) -> bool:
                raise RuntimeError("таблица журнала недоступна")

        with caplog.at_level(logging.WARNING, logger=BUS_LOGGER):
            asyncio.run(make_inbound_logger(_NoLogger())(_FakeInbound()))

        messages = "\n".join(r.getMessage() for r in caplog.records)
        assert "не залогировано" in messages
        assert "таблица журнала недоступна" in messages


class TestInboundActorIsHonest:
    """``actor`` входящего — это источник, а не константа."""

    def test_queue_message_without_sender_is_not_called_a_user(self, caplog: Any) -> None:
        service = _service()
        message = _FakeInbound(sender_id=None)

        asyncio.run(make_inbound_logger(service)(message))

        inbound = next(e for e in service._queue.queue if isinstance(e, LogEvent))
        assert inbound.actor != "user", (
            "у сообщения из очереди нет человека на том конце — «user» был бы выдумкой"
        )
        assert inbound.actor == "queue:postgres"

    def test_named_requester_stays_the_actor(self) -> None:
        service = _service()

        asyncio.run(make_inbound_logger(service)(_FakeInbound(sender_id="queue-owner")))

        inbound = next(e for e in service._queue.queue if isinstance(e, LogEvent))
        assert inbound.actor == "queue-owner"

    def test_other_channels_keep_the_human_actor(self) -> None:
        service = _service()
        message = _FakeInbound(channel="cli", session_key="cli:direct", sender_id=None)

        asyncio.run(make_inbound_logger(service)(message))

        inbound = next(e for e in service._queue.queue if isinstance(e, LogEvent))
        assert inbound.actor == "user"


class TestFinalAnswerIsSigned:
    """Финальный ответ оборота уходит после конца оборота — но подписать его можно.

    ``outbound_final`` публикуется уже после ``DatabaseLoggingHook.after_run``,
    который снял привязку вопроса (``clear_request``), поэтому индекс вопросов
    пуст и подпись события неоткуда взять. Личность берётся из одноразового
    снимка ВХОДА: подставлять выдуманную нельзя, а взять свою — можно.
    """

    SESSION = "postgres:42"
    SENDER = "user-77"
    REQUEST_ID = "msg-9001"

    def _service_after_turn(self) -> DbLoggingService:
        service = _service()
        service.register_request(
            self.SESSION, self.REQUEST_ID, user_id=self.SENDER,
            chat_id="42", channel="postgres",
        )
        service.clear_request(self.SESSION)  # что делает after_run в конце оборота
        return service

    def _outbound(self, final: bool = True, message_id: str | None = None) -> Any:
        class _Msg:
            channel = "postgres"
            chat_id = "42"
            content = "готовый ответ"
            media: list = []
            metadata = {"_final_turn": True} if final else {"_turn": 1}
            event = None

            def __init__(self) -> None:
                if message_id:
                    self.metadata = dict(self.metadata, message_id=message_id)

        return _Msg()

    def _final(self, service: DbLoggingService, **kwargs: Any) -> LogEvent:
        asyncio.run(make_outbound_logger(service)(self._outbound(**kwargs)))
        finals = [
            e for e in service._queue.queue
            if isinstance(e, LogEvent) and e.event_type == "agent.delivered"
        ]
        assert len(finals) == 1
        return finals[0]

    def test_final_answer_carries_the_identity_of_its_own_turn(self) -> None:
        service = self._service_after_turn()

        event = self._final(service)

        assert event.user_id == self.SENDER
        assert event.request_id == self.REQUEST_ID

    def test_final_answer_is_sendable_by_the_transport(self) -> None:
        """Без полной личности транспорт отбрасывает группу — и ответ теряется."""
        service = self._service_after_turn()

        event = self._final(service)

        assert group_by_identity([event])[0].key.complete()

    def test_identity_snapshot_is_consumed_once(self) -> None:
        """Второе событие конца оборота личности первого не получает.

        Иначе события РАЗНЫХ оборотов одного чата подписывались бы чужим
        ``user_id``.
        """
        service = self._service_after_turn()
        asyncio.run(make_outbound_logger(service)(self._outbound()))
        asyncio.run(make_outbound_logger(service)(self._outbound()))

        finals = [
            e for e in service._queue.queue
            if isinstance(e, LogEvent) and e.event_type == "agent.delivered"
        ]
        assert [e.user_id for e in finals] == [self.SENDER, None]

    def test_new_turn_rearms_the_snapshot(self) -> None:
        service = _service()
        service.register_request(self.SESSION, "msg-1", user_id="user-1")
        service.clear_request(self.SESSION)
        service.register_request(self.SESSION, "msg-2", user_id="user-2")
        service.clear_request(self.SESSION)

        event = self._final(service)

        assert event.user_id == "user-2", "снимок принадлежит последнему входу"
        assert event.request_id == "msg-2"

    def test_final_without_inbound_is_not_signed_by_a_guess(self) -> None:
        """Нет входящего — нет и личности. Плейсхолдер записал бы событие
        в чужую учётную запись."""
        service = _service()

        event = self._final(service)

        assert event.user_id is None
        assert event.request_id is None

    def test_inbound_sender_is_not_fabricated(self) -> None:
        """Очередь без ``sender_id`` — тоже не повод выдумать человека."""
        service = _service()
        service.register_request(self.SESSION, self.REQUEST_ID, user_id=None)
        service.clear_request(self.SESSION)

        event = self._final(service)

        assert event.user_id is None

    def test_intermediate_message_does_not_consume_the_snapshot(self) -> None:
        """Промежуточные сообщения оборота снимок не забирают: конец у них один."""
        service = self._service_after_turn()

        asyncio.run(make_outbound_logger(service)(self._outbound(final=False)))
        event = self._final(service)

        assert event.user_id == self.SENDER

    def test_explicit_message_id_is_kept(self) -> None:
        """Свою привязку сообщения не затираем снимком оборота."""
        service = self._service_after_turn()

        event = self._final(service, message_id="outbound-55")

        assert event.request_id == "outbound-55"
        assert event.user_id == self.SENDER

    def test_snapshot_does_not_outlive_the_process(self) -> None:
        service = self._service_after_turn()
        service.stop()

        event = self._final(service)

        assert event.user_id is None
