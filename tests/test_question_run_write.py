"""Контекст вопроса (``agent_question_runs``): запись не теряется молча.

Симптом, который закрывают эти тесты: обороты, пришедшие из очереди
(``channels.postgres``), имели полный след событий в журнале и НОЛЬ строк в
таблице прогонов вопросов, при этом ничего не падало и нигде не сообщалось.
Причина была не в канале и не в очереди, а в том, что запись контекста
уходила в транспорт неподписанной и отбрасывалась на раннем ``return`` —
а счётчик ``question_runs`` рос на этом пути так же, как на успешной записи.

Каждый тест проверяет решение, а не «строку кода»:

* ``finish_request`` подписывается личностью, зарегистрированной для этого же
  ``request_id`` (регистрация и завершение — два вызова одной записи);
* прогресс, который не состоялся, не считается состоявшимся: у него свой
  счётчик, и потеря видна в статистике;
* отказ платформы поднимается наверх и попадает в лог с текстом отказа, а не
  растворяется в «сервер недоступен».

Имена таблиц в тестах — заглушки: при MCP-транспорте они не используются, а
страж ``test_no_hardcoded_table_names`` запрещает зашивать реальные.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from lib.services.db_logging_service import DbLoggingService, _QuestionRunRecord
from lib.services.enterprise_mcp_client import (
    CallIdentity,
    EnterpriseMcpUnavailable,
    EnterpriseOperationError,
)
from lib.services.log_transport import (
    LocalFallbackSink,
    LogWriteUnavailable,
    LoopCallRunner,
    McpLogWriter,
)

SESSION = "postgres:queue_probe"
REQUEST_ID = "11111111-2222-3333-4444-555555555555"
USER_ID = "queue-owner"


class RecordingClient:
    """Клиент-заглушка: помнит вызовы, отвечает или падает заданным."""

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
        if operation == "data.log_events":
            count = len((arguments or {}).get("events", []))
            return json.dumps({"status": "ok", "accepted": count, "dropped": 0})
        return '{"status": "ok"}'


class SyncRunner:
    """Гоняет корутину до конца — для тестов без живого loop."""

    def __call__(self, coro: Any) -> Any:
        return asyncio.run(coro)


@contextmanager
def _live_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """Живой loop в отдельном потоке — как в рантайме агента.

    Мост ждёт результат корутины синхронно, поэтому звать его из потока самого
    loop нельзя: loop занят ожиданием собственного future. Агент тоже моет
    журнал из отдельного worker-потока, и здесь нужна та же форма.
    """
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        # Дать отменённой корутине добежать до конца: иначе закрытие loop'а
        # печатает в лог asyncio «Task was destroyed but it is pending».
        time.sleep(0.05)
        loop.close()


def _service(
    writer: Any, sink: Any = None, **kwargs: Any
) -> DbLoggingService:
    # Имена таблиц — заглушки (см. докстринг модуля).
    return DbLoggingService(
        dsn="",
        table_name="journal_events",
        question_runs_table="journal_runs",
        mcp_writer=writer,
        fallback_sink=sink,
        **kwargs,
    )


def _writer(client: RecordingClient) -> McpLogWriter:
    return McpLogWriter(call=client.call, run=SyncRunner())


def _drain(service: DbLoggingService) -> list[Any]:
    """Вынуть и обработать накопленное очередью без запуска потока."""
    records = []
    while not service._queue.empty():
        item = service._queue.get_nowait()
        if isinstance(item, _QuestionRunRecord):
            service._handle_question_run(item)
        records.append(item)
    return records


class TestSignedWrite:
    """Регистрация и завершение оборота — одна запись, и обе части подписаны."""

    def test_registration_and_finish_both_reach_the_platform(self) -> None:
        client = RecordingClient()
        service = _service(_writer(client))

        assert service.register_request(
            SESSION, REQUEST_ID, user_id=USER_ID, chat_id="queue_probe",
            channel="postgres", question="сколько строк?",
        ) is True
        assert service.finish_request(
            REQUEST_ID, status="finished", summary="ок", response="много"
        ) is True
        _drain(service)

        writes = [c for c in client.calls if c[0] == "data.upsert_question_run"]
        assert len(writes) == 2, "регистрация и завершение — две записи одного оборота"
        assert writes[1][1]["update_only"] is True
        for _, _, identity in writes:
            assert identity.request_id == REQUEST_ID
            assert identity.session_id == SESSION
            assert identity.user_id == USER_ID

    def test_finish_without_registration_is_not_signed_by_a_guess(self) -> None:
        """Нет регистрации — нет и личности; выдумывать её нельзя."""
        client = RecordingClient()
        service = _service(_writer(client))

        service.finish_request("неизвестный-прогон", status="finished")
        _drain(service)

        assert client.calls == [], "подписывать нечем — значит и не звать"

    def test_finish_reports_another_questions_identity(self) -> None:
        """Личность берётся у СВОЕГО request_id, а не у последнего попавшего."""
        client = RecordingClient()
        service = _service(_writer(client))
        service.register_request(SESSION, REQUEST_ID, user_id=USER_ID)

        service.finish_request("чужой-прогон", status="finished")
        _drain(service)

        assert [c[0] for c in client.calls] == ["data.upsert_question_run"], (
            "регистрация прошла, незнакомый прогон — нет"
        )
        assert client.calls[0][2].request_id == REQUEST_ID


class TestHonestStatistics:
    """Статистика обязана отличать написанное от не-написанного."""

    def test_early_return_is_not_counted_as_a_write(self) -> None:
        client = RecordingClient()
        service = _service(_writer(client))

        service.finish_request("неизвестный-прогон", status="finished")
        _drain(service)

        stats = service.get_stats()
        assert stats["question_runs"] == 0, (
            "вызова платформы не было — записей тоже не было"
        )
        assert stats["question_runs_skipped"] == 1, (
            "потеря обязана быть видна своим счётчиком, иначе её не видно"
        )

    def test_performed_write_is_counted_as_written(self) -> None:
        client = RecordingClient()
        service = _service(_writer(client))
        service.register_request(SESSION, REQUEST_ID, user_id=USER_ID)
        service.finish_request(REQUEST_ID, status="finished")

        _drain(service)

        stats = service.get_stats()
        assert stats["question_runs"] == 2
        assert stats["question_runs_skipped"] == 0
        assert stats["question_runs_failed"] == 0

    def test_counter_keys_exist_before_anything_happened(self) -> None:
        stats = _service(_writer(RecordingClient())).get_stats()
        for key in ("question_runs", "question_runs_skipped", "question_runs_failed"):
            assert key in stats, f"счётчика {key!r} нет — потеря некому считать"

    def test_skipped_write_leaves_a_local_trail(self, tmp_path: Any) -> None:
        """До платформы запись не дошла — след попытки должен остаться."""
        sink = LocalFallbackSink(tmp_path / "fallback.jsonl")
        client = RecordingClient()
        service = _service(_writer(client), sink=sink)

        service.finish_request("неизвестный-прогон", status="finished")
        _drain(service)

        assert sink.stats()["written"] == 1, (
            "счётчик потерь живёт только в памяти процесса — нужен и след в файле"
        )


class TestLoudFailures:
    """Отказ обязан быть виден: и в статистике, и в логе с текстом отказа."""

    def test_platform_refusal_is_counted_as_a_failure(self) -> None:
        client = RecordingClient()
        client.fail_with = EnterpriseOperationError("permission_denied", "нет прав")
        service = _service(_writer(client))
        service.register_request(SESSION, REQUEST_ID, user_id=USER_ID)

        _drain(service)

        stats = service.get_stats()
        assert stats["question_runs"] == 0
        assert stats["question_runs_failed"] == 1
        assert "permission_denied" in str(stats["last_error"])

    def test_platform_refusal_is_logged_with_its_own_text(self, caplog: Any) -> None:
        client = RecordingClient()
        client.fail_with = EnterpriseOperationError("permission_denied", "нет прав")
        service = _service(_writer(client))
        service.register_request(SESSION, REQUEST_ID, user_id=USER_ID)

        with caplog.at_level("ERROR", logger="lib.services.db_logging_service"):
            _drain(service)

        text = "\n".join(record.getMessage() for record in caplog.records)
        assert "permission_denied" in text, (
            "отказ платформы обязан быть назван своим текстом, иначе след пуст"
        )

    def test_unavailable_transport_is_a_skip_not_a_write(self) -> None:
        client = RecordingClient()
        client.fail_with = EnterpriseMcpUnavailable("сервер не поднялся")
        service = _service(_writer(client))
        service.register_request(SESSION, REQUEST_ID, user_id=USER_ID)

        _drain(service)

        stats = service.get_stats()
        assert stats["question_runs"] == 0
        assert stats["question_runs_skipped"] == 1

    def test_domain_error_is_not_misreported_as_a_dead_loop(self) -> None:
        """``EnterpriseOperationError`` — это ответ сервера, а не отказ loop'а.

        Оба класса — потомки ``RuntimeError``, и мост ``LoopCallRunner`` раньше
        переписывал доменный отказ в «event loop отказал», то есть подменял
        «сервер отказал» на «сервера нет» и прятал причину в сообщении о loop'е.
        """
        with _live_loop() as loop:
            runner = LoopCallRunner(loop=loop)

            async def _refuse() -> str:
                raise EnterpriseOperationError("identity_missing", "нет личности")

            with pytest.raises(EnterpriseOperationError) as caught:
                runner(_refuse())
            assert "identity_missing" in str(caught.value)

    def test_dead_loop_is_still_reported_as_unavailable(self) -> None:
        """Обратная сторона: настоящий отказ loop'а остаётся LogWriteUnavailable."""
        with _live_loop() as loop:
            runner = LoopCallRunner(loop=loop, timeout_sec=0.05)

            async def _sleep() -> str:
                await asyncio.sleep(5)
                return "{}"

            with pytest.raises(LogWriteUnavailable):
                runner(_sleep())
