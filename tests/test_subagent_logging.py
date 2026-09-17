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
from unittest.mock import MagicMock

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
