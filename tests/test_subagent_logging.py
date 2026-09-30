"""Регрессионные тесты для subagent-логирования.

Закрывает баг №3 из proposal: «пишется ли ``subagent_run_finished``
вообще». Тесты проверяют, что при finalize'е subagent-цикла
``_SubagentLoggingHook._finalize`` эмиттирует ``LogEvent`` с
``event_type="subagent_run_finished"`` и характерной структурой payload.

Поскольку ``_SubagentLoggingHook`` — приватный класс, определённый
внутри ``RuntimePatcher.patch_subagent_logging`` (``lib/services/runtime_patcher.py``),
мы воспроизводим минимум логики финализации и проверяем публичный
контракт сервиса: ``db_logging_service.get_stats()["written_by_type"]``
должен расти после успешного flush'а subagent-итога.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_WORKSPACE = str(Path(__file__).resolve().parent.parent / "workspace")
if _WORKSPACE not in sys.path:
    sys.path.insert(0, _WORKSPACE)


class TestSubagentRunFinishedEventShape:
    """``subagent_run_finished`` пишется как ``LogEvent`` с правильным
    типом и payload."""

    def test_subagent_run_finished_event_type(self):
        """Имитируем emit ``subagent_run_finished`` через реальный
        ``DbLoggingService`` (без БД — событие в очереди).
        """
        from lib.services.db_logging_service import (
            DbLoggingService,
            LogEvent,
        )

        svc = DbLoggingService(
            dsn="", table_name="x", question_runs_table="y",
        )

        # Эмулируем ровно тот путь, что использует ``_SubagentLoggingHook.
        # ``_finalize`` (см. lib/services/runtime_patcher.py:1512-1555):
        svc.log_event(LogEvent(
            event_type="subagent_run_finished",
            level="INFO",
            session_id="subagent:task-1",
            channel="subagent",
            actor="agent",
            name="task-1",
            request_id="subagent:task-1",
            summary="ответ подагента",
            payload={
                "final_content": "ответ подагента",
                "tools_used": ["compact_context"],
                "stop_reason": "stop",
                "task_id": "task-1",
                "task": "краткое описание задачи",
                "request_id": "subagent:task-1",
                "parent_request_id": "req-parent-1",
            },
            metadata={
                "tokens_used": 42,
                "had_error": False,
            },
        ))

        events = [e for e in svc._queue.queue if isinstance(e, LogEvent)]
        sub = next(
            (e for e in events if e.event_type == "subagent_run_finished"),
            None,
        )
        assert sub is not None
        assert sub.session_id == "subagent:task-1"
        assert sub.channel == "subagent"
        assert sub.payload["task_id"] == "task-1"
        assert sub.payload["parent_request_id"] == "req-parent-1"
        assert sub.payload["tools_used"] == ["compact_context"]
        assert sub.request_id == "subagent:task-1"

    def test_subagent_run_finished_written_by_type(self):
        """После успешного flush'а событие инкрементирует
        ``written_by_type["subagent_run_finished"]``.

        Мокаем ``DbLoggingService._db_run``, чтобы ``_flush_batch``
        прошёл без реальной БД.
        """
        from lib.services.db_logging_service import (
            DbLoggingService,
            LogEvent,
        )
        from unittest.mock import MagicMock, patch

        svc = DbLoggingService(
            dsn="postgresql://x",
            table_name="x",
            question_runs_table="y",
            flush_interval_sec=0.05,
            batch_size=4,
        )

        # ``_flush_batch`` вызывает ``self._db_run(_work)`` —
        # мокаем, чтобы INSERT прошёл без реальной БД.
        with patch.object(svc, "_db_run", return_value=None):
            svc.start()
            try:
                for i in range(2):
                    svc.log_event(LogEvent(
                        event_type="subagent_run_finished",
                        session_id=f"subagent:task-{i}",
                        channel="subagent",
                        actor="agent",
                        name=f"task-{i}",
                        summary=f"answer {i}",
                        payload={
                            "final_content": f"ответ {i}",
                            "tools_used": [],
                            "stop_reason": "stop",
                            "task_id": f"task-{i}",
                            "task": "task desc",
                            "request_id": f"subagent:task-{i}",
                            "parent_request_id": "rid",
                        },
                        metadata={},
                    ))
                time.sleep(0.3)
            finally:
                svc.stop(timeout_sec=2.0)

        counter = svc.get_stats()["written_by_type"]
        assert counter.get("subagent_run_finished") == 2

    def test_subagent_logging_hook_finalize_writes_event(self):
        """Wiring-тест через реальный ``_SubagentLoggingHook``:
        патчер ``RuntimePatcher.patch_subagent_logging`` подменяет
        ``nanobot.agent.subagent._SubagentHook`` на подкласс, который
        эмиттирует ``subagent_run_finished`` через ``_finalize``.
        Мы напрямую вызываем ``_finalize`` через подменённый класс и
        проверяем, что ``db_logging_service.log_event`` получил
        ``LogEvent`` с правильным ``event_type`` и характерным payload.
        """
        from unittest.mock import MagicMock, patch

        from lib.services.db_logging_service import (
            DbLoggingService,
            LogEvent,
        )
        from lib.hooks.database_logging_hook import DatabaseLoggingHook

        svc = DbLoggingService(
            dsn="", table_name="x", question_runs_table="y",
        )
        # ``_SubagentLoggingHook`` создаёт свой ``DatabaseLoggingHook``
        # инстанс per-subagent (см. комментарий в runtime_patcher.py).
        # Подменяем его на ``svc``, чтобы не плодить лишний.
        with patch(
            "lib.hooks.database_logging_hook.DatabaseLoggingHook",
            lambda *_a, **_kw: DatabaseLoggingHook(svc),
        ):
            from lib.services.runtime_patcher import RuntimePatcher

            patcher = RuntimePatcher()
            ok, reason = patcher.patch_subagent_logging(
                db_logging_service=svc, session_manager=None,
            )
            assert ok, reason

            # Импортируем подменённый класс (патчер заменяет атрибут
            # ``_SubagentHook`` модуля ``nanobot.agent.subagent``).
            from nanobot.agent.subagent import _SubagentHook  # noqa: WPS433

            # Конструируем хук с явным task_id и имитируем finalize
            # через прямой вызов ``after_run`` / ``on_error``.
            # Используем ``SimpleNamespace`` для ``context`` —
            # ``_SubagentLoggingHook`` ожидает ``session_key``,
            # ``final_content``, ``tools_used``, ``stop_reason``,
            # ``messages``, ``usage``, ``error``.
            ctx = SimpleNamespace(
                session_key="postgres:42",
                final_content="ответ подагента",
                tools_used=["compact_context"],
                stop_reason="stop",
                messages=[
                    {"role": "user", "content": "сводка договора"},
                ],
                usage={"total_tokens": 42},
                error=None,
            )
            hook = _SubagentHook("task-1")
            import asyncio
            asyncio.run(hook.after_run(ctx))

        # ``_finalize`` через ``after_run`` эмиттировал LogEvent в
        # очередь ``svc``. Проверяем форму события.
        events = [e for e in svc._queue.queue if isinstance(e, LogEvent)]
        sub = next(
            (e for e in events if e.event_type == "subagent_run_finished"),
            None,
        )
        assert sub is not None, (
            "после _SubagentLoggingHook._finalize LogEvent "
            "subagent_run_finished должен быть в очереди"
        )
        # Форма события — точно как требует спекa § tools-history-search
        # / subagent_run_finished payload:
        assert sub.session_id == "subagent:task-1"
        assert sub.channel == "subagent"
        assert sub.payload["task_id"] == "task-1"
        assert sub.payload["task"] == "сводка договора"
        assert sub.payload["final_content"] == "ответ подагента"
        assert sub.payload["tools_used"] == ["compact_context"]
        assert sub.payload["stop_reason"] == "stop"
        assert "parent_request_id" in sub.payload
        assert "request_id" in sub.payload
        # Канал и сессия соответствуют конвенции под-агента.
        assert sub.request_id == "subagent:task-1"


class TestSubagentUserIdPropagation:
    """``_SubagentLoggingHook`` явно прокидывает ``user_id`` родителя
    в ``LogEvent.user_id`` для ``subagent_run_finished`` (security
    boundary для ``history_search(session_scope="all")``)."""

    def test_subagent_inherits_parent_user_id(self):
        """parent alice → subagent → INSERT содержит user_id='alice'."""
        from lib.services.db_logging_service import (
            DbLoggingService,
            LogEvent,
        )
        from lib.hooks.database_logging_hook import DatabaseLoggingHook
        from unittest.mock import MagicMock, patch

        svc = DbLoggingService(
            dsn="", table_name="x", question_runs_table="y",
        )
        with patch(
            "lib.hooks.database_logging_hook.DatabaseLoggingHook",
            lambda *_a, **_kw: DatabaseLoggingHook(svc),
        ):
            from lib.services.runtime_patcher import RuntimePatcher

            patcher = RuntimePatcher()
            ok, reason = patcher.patch_subagent_logging(
                db_logging_service=svc, session_manager=None,
            )
            assert ok, reason

            from nanobot.agent.subagent import _SubagentHook  # noqa: WPS433

            ctx = SimpleNamespace(
                session_key="postgres:42",
                final_content="ответ подагента",
                tools_used=[],
                stop_reason="stop",
                messages=[{"role": "user", "content": "сводка"}],
                usage={"total_tokens": 1},
                error=None,
            )
            with patch(
                "nanobot.agent.tools.context.current_request_context",
                return_value=SimpleNamespace(sender_id="alice"),
            ):
                hook = _SubagentHook("task-1")
                import asyncio
                asyncio.run(hook.after_run(ctx))

        events = [e for e in svc._queue.queue if isinstance(e, LogEvent)]
        sub = next(
            (e for e in events if e.event_type == "subagent_run_finished"),
            None,
        )
        assert sub is not None
        assert sub.user_id == "alice"

    def test_previous_request_user_does_not_leak_to_subagent(self):
        """previous request alice, current request bob → subagent текущего
        request пишется с user_id='bob' (НЕ 'alice'). Закрывает security
        окно вида «subagent подхватывает user_id предыдущего request»."""
        from lib.services.db_logging_service import (
            DbLoggingService,
            LogEvent,
        )
        from lib.hooks.database_logging_hook import DatabaseLoggingHook
        from unittest.mock import MagicMock, patch

        svc = DbLoggingService(
            dsn="", table_name="x", question_runs_table="y",
        )
        with patch(
            "lib.hooks.database_logging_hook.DatabaseLoggingHook",
            lambda *_a, **_kw: DatabaseLoggingHook(svc),
        ):
            from lib.services.runtime_patcher import RuntimePatcher

            patcher = RuntimePatcher()
            ok, reason = patcher.patch_subagent_logging(
                db_logging_service=svc, session_manager=None,
            )
            assert ok, reason

            from nanobot.agent.subagent import _SubagentHook  # noqa: WPS433

            ctx = SimpleNamespace(
                session_key="postgres:42",
                final_content="ответ подагента",
                tools_used=[],
                stop_reason="stop",
                messages=[{"role": "user", "content": "task"}],
                usage={"total_tokens": 1},
                error=None,
            )
            # Контекст текущего request: bob.
            with patch(
                "nanobot.agent.tools.context.current_request_context",
                return_value=SimpleNamespace(sender_id="bob"),
            ):
                hook = _SubagentHook("task-2")
                import asyncio
                asyncio.run(hook.after_run(ctx))

        events = [e for e in svc._queue.queue if isinstance(e, LogEvent)]
        sub = next(
            (e for e in events if e.event_type == "subagent_run_finished"),
            None,
        )
        assert sub is not None
        assert sub.user_id == "bob"
        assert sub.user_id != "alice"


# ---------------------------------------------------------------------------
# post-0.3.5-patches-cleanup / группа 2:
# _SubagentLoggingHook публикует SubagentTurnCompleted через bus.publish.
# ---------------------------------------------------------------------------


class FakeBus:
    """Имитация ``MessageBus`` для тестов публикации событий."""

    def __init__(self) -> None:
        self.published: list = []

    async def publish(self, event: Any) -> None:
        self.published.append(event)


@pytest.mark.asyncio
async def test_subagent_publishes_subagent_turn_completed_on_after_run() -> None:
    from lib.hooks.database_logging_hook import DatabaseLoggingHook
    from lib.services.db_logging_service import DbLoggingService, LogEvent
    from lib.services.runtime_patcher import RuntimePatcher

    svc = DbLoggingService(dsn="", table_name="x", question_runs_table="y")
    bus = FakeBus()

    with patch(
        "lib.hooks.database_logging_hook.DatabaseLoggingHook",
        lambda *_a, **_kw: DatabaseLoggingHook(svc),
    ):
        patcher = RuntimePatcher()
        ok, _ = patcher.patch_subagent_logging(
            db_logging_service=svc, session_manager=None, bus=bus,
        )
        assert ok

        from nanobot.agent.subagent import _SubagentHook  # noqa: WPS433

        ctx = SimpleNamespace(
            session_key="telegram:1",
            final_content="done",
            tools_used=["read_file"],
            stop_reason="end_turn",
            messages=[{"role": "user", "content": "task description"}],
            usage=SimpleNamespace(total_tokens=128),
            error=None,
            exception=None,
        )
        hook = _SubagentHook("task-pub", bus=bus)

    # after_run публикует SubagentTurnCompleted через bus.publish.
    with patch(
        "lib.hooks.database_logging_hook._current_request_sender_id",
        return_value="alice",
    ):
        await hook.after_run(ctx)

    assert len(bus.published) == 1
    from lib.events.subagent import SubagentTurnCompleted

    ev = bus.published[0]
    assert isinstance(ev, SubagentTurnCompleted)
    assert ev.task_id == "task-pub"
    assert ev.final_content == "done"
    assert ev.tools_used == ["read_file"]
    assert ev.had_error is False


@pytest.mark.asyncio
async def test_subagent_no_bus_means_no_publish() -> None:
    """Без bus — публикация пропускается, _finalize продолжает работать."""
    from lib.hooks.database_logging_hook import DatabaseLoggingHook
    from lib.services.db_logging_service import DbLoggingService
    from lib.services.runtime_patcher import RuntimePatcher

    svc = DbLoggingService(dsn="", table_name="x", question_runs_table="y")

    with patch(
        "lib.hooks.database_logging_hook.DatabaseLoggingHook",
        lambda *_a, **_kw: DatabaseLoggingHook(svc),
    ):
        patcher = RuntimePatcher()
        ok, _ = patcher.patch_subagent_logging(
            db_logging_service=svc, session_manager=None,
            # bus не передаётся
        )
        assert ok

        from nanobot.agent.subagent import _SubagentHook  # noqa: WPS433

        ctx = SimpleNamespace(
            session_key="telegram:1",
            final_content="done",
            tools_used=[],
            stop_reason="end_turn",
            messages=[{"role": "user", "content": "task"}],
            usage=None,
            error=None,
            exception=None,
        )
        hook = _SubagentHook("task-nobus")  # bus=None по умолчанию

    # Не должно быть исключения.
    await hook.after_run(ctx)

    # _finalize всё равно должен был записать subagent_run_finished.
    from lib.services.db_logging_service import LogEvent as _LogEvent

    events = [e for e in svc._queue.queue if isinstance(e, _LogEvent)]
    sub = next(
        (e for e in events if e.event_type == "subagent_run_finished"),
        None,
    )
    assert sub is not None


@pytest.mark.asyncio
async def test_subagent_finalize_skips_log_event_when_subscriber_active() -> None:
    """Когда ``_subscriber_registered=True``, ``_finalize`` НЕ пишет
    ``subagent_run_finished`` напрямую в БД (запись сделает
    ``_handle_subagent_turn_completed`` через pub-sub).

    Также проверяет, что finish_request всё равно вызывается (для
    question_runs). Открытие/закрытие request остаётся в _finalize.
    """
    from lib.hooks.database_logging_hook import DatabaseLoggingHook
    from lib.services.db_logging_service import DbLoggingService
    from lib.services.runtime_patcher import RuntimePatcher

    svc = DbLoggingService(dsn="", table_name="x", question_runs_table="y")

    # Патчим DatabaseLoggingHook внутри _SubagentLoggingHook,
    # чтобы он использовал наш svc.
    with patch(
        "lib.hooks.database_logging_hook.DatabaseLoggingHook",
        lambda *_a, **_kw: DatabaseLoggingHook(svc),
    ):
        patcher = RuntimePatcher()
        ok, _ = patcher.patch_subagent_logging(
            db_logging_service=svc, session_manager=None,
        )
        assert ok

        from nanobot.agent.subagent import _SubagentHook  # noqa: WPS433

        # Активируем флаг subagent-subscriber.
        _SubagentHook.set_subscriber_registered(True)
        try:
            ctx = SimpleNamespace(
                session_key="telegram:1",
                final_content="done",
                tools_used=["read_file"],
                stop_reason="end_turn",
                messages=[{"role": "user", "content": "task"}],
                usage=None,
                error=None,
                exception=None,
            )
            hook = _SubagentHook("task-flag")
            await hook.after_run(ctx)

            # _finalize НЕ должен был записать subagent_run_finished.
            from lib.services.db_logging_service import LogEvent as _LogEvent

            events = [e for e in svc._queue.queue if isinstance(e, _LogEvent)]
            sub = next(
                (e for e in events if e.event_type == "subagent_run_finished"),
                None,
            )
            assert sub is None, (
                "subagent_run_finished должен быть записан через pub-sub, "
                "а не через _finalize напрямую"
            )
        finally:
            _SubagentHook.set_subscriber_registered(False)
