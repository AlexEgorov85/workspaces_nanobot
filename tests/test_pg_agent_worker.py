from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import pg_agent_worker as pw


@pytest.fixture(autouse=True)
def patch_db_and_nanobot():
    """Patch pg_agent_worker's module-level imports."""
    with (
        patch("pg_agent_worker.fetch", new_callable=AsyncMock) as mock_fetch,
        patch("pg_agent_worker.execute", new_callable=AsyncMock) as mock_exec,
        patch("pg_agent_worker.transaction") as mock_tx,
        patch("pg_agent_worker.configure"),
        patch("pg_agent_worker.Nanobot") as mock_nb,
    ):
        mock_conn = AsyncMock()
        mock_tx.return_value.__aenter__.return_value = mock_conn
        mock_fetch.return_value = []

        yield {
            "fetch": mock_fetch,
            "execute": mock_exec,
            "transaction": mock_tx,
            "conn": mock_conn,
            "Nanobot": mock_nb,
        }


def _make_worker(**overrides):
    args = {
        "input_table": "agent_questions",
        "output_table": "agent_responses",
        "batch_size": 10,
        "session_prefix": "pg_worker",
    }
    args.update(overrides)
    worker = pw.PostgresAgentWorker(**args)
    worker._bot = MagicMock()
    worker._bot.run = AsyncMock(return_value=MagicMock(content="ok"))
    return worker


class TestWorkerInit:
    def test_defaults(self):
        worker = pw.PostgresAgentWorker()
        assert worker.input_table == "agent_questions"
        assert worker.output_table == "agent_responses"
        assert worker.batch_size == 10
        assert worker.session_prefix == "pg_worker"
        assert worker._bot is None
        assert worker._session_cache == {}


class TestWorkerGetSessionKey:
    @pytest.mark.asyncio
    async def test_new_session(self):
        worker = _make_worker()
        assert await worker._get_session_key("sess-1") == "pg_worker:sess-1"
        assert "sess-1" in worker._session_cache

    @pytest.mark.asyncio
    async def test_cached_session(self):
        worker = _make_worker()
        worker._session_cache["sess-1"] = "custom:key"
        assert await worker._get_session_key("sess-1") == "custom:key"

    @pytest.mark.asyncio
    async def test_custom_prefix(self):
        worker = _make_worker(session_prefix="my_prefix")
        assert await worker._get_session_key("s-1") == "my_prefix:s-1"


class TestWorkerGetPendingQuestions:
    @pytest.mark.asyncio
    async def test_returns_empty(self, patch_db_and_nanobot):
        patch_db_and_nanobot["fetch"].return_value = []
        worker = _make_worker()
        worker._bot = None
        questions = await worker._get_pending_questions(10)
        assert questions == []

    @pytest.mark.asyncio
    async def test_returns_rows(self, patch_db_and_nanobot):
        patch_db_and_nanobot["fetch"].return_value = [
            {"id": 1, "session_id": "s1", "question": "What?", "priority": 1, "created_at": "2024-01-01"},
        ]
        worker = _make_worker()
        worker._bot = None
        questions = await worker._get_pending_questions(10)
        assert len(questions) == 1
        assert questions[0]["id"] == 1


class TestWorkerMarkProcessing:
    @pytest.mark.asyncio
    async def test_marks_processing(self, patch_db_and_nanobot):
        worker = _make_worker()
        worker._bot = None
        await worker._mark_processing(42)
        patch_db_and_nanobot["execute"].assert_called_once()


class TestWorkerSaveResponse:
    @pytest.mark.asyncio
    async def test_saves_in_transaction(self, patch_db_and_nanobot):
        worker = _make_worker()
        worker._bot = None
        await worker._save_response(1, "Answer", status="completed", metadata={"key": "val"})
        assert patch_db_and_nanobot["conn"].execute.call_count == 2

    @pytest.mark.asyncio
    async def test_saves_with_error(self, patch_db_and_nanobot):
        worker = _make_worker()
        worker._bot = None
        await worker._save_response(1, "Error msg", status="failed", error_message="Broke")
        assert patch_db_and_nanobot["conn"].execute.call_count == 2


class TestWorkerProcessQuestion:
    @pytest.mark.asyncio
    async def test_success(self):
        worker = _make_worker()
        worker._bot.run = AsyncMock(
            return_value=MagicMock(
                content="The answer is 42",
                usage={"prompt_tokens": 10, "completion_tokens": 20},
            )
        )
        success, response, metadata = await worker._process_question({
            "id": 1, "session_id": "s1", "question": "What is the answer?",
        })
        assert success is True
        assert response == "The answer is 42"
        assert metadata["latency_sec"] >= 0
        assert metadata["tokens"]["prompt_tokens"] == 10

    @pytest.mark.asyncio
    async def test_failure(self):
        worker = _make_worker()
        worker._bot.run = AsyncMock(side_effect=Exception("LLM API error"))
        success, response, metadata = await worker._process_question({
            "id": 1, "session_id": "s1", "question": "What?",
        })
        assert success is False
        assert "Error:" in response
        assert "error" in metadata

    @pytest.mark.asyncio
    async def test_init_bot_if_needed(self, patch_db_and_nanobot):
        worker = pw.PostgresAgentWorker()
        mock_bot = MagicMock()
        mock_bot.run = AsyncMock(return_value=MagicMock(content="ok"))
        patch_db_and_nanobot["Nanobot"].from_config.return_value = mock_bot

        success, response, _ = await worker._process_question({
            "id": 1, "session_id": "s1", "question": "Hi",
        })
        assert success is True
        assert worker._bot is not None
        patch_db_and_nanobot["Nanobot"].from_config.assert_called_once()


class TestWorkerProcessQuestionMetadata:
    @pytest.mark.asyncio
    async def test_metadata_contains_lengths(self):
        worker = _make_worker()
        worker._bot.run = AsyncMock(return_value=MagicMock(content="Answer", usage=None))
        _, _, metadata = await worker._process_question({
            "id": 1, "session_id": "s1", "question": "Hi there!",
        })
        assert metadata["prompt_length"] == 9
        assert metadata["response_length"] == 6
        assert metadata["session_id"] == "s1"
        assert "latency_sec" in metadata


class TestWorkerRunBatch:
    @pytest.mark.asyncio
    async def test_no_pending_questions(self):
        worker = _make_worker()
        worker._get_pending_questions = AsyncMock(return_value=[])
        stats = await worker.run_batch(limit=10)
        assert stats == {"processed": 0, "success": 0, "failed": 0}

    @pytest.mark.asyncio
    async def test_processes_questions(self):
        worker = _make_worker()
        worker._get_pending_questions = AsyncMock(return_value=[
            {"id": 1, "session_id": "s1", "question": "Q1?", "created_at": "2024-01-01"},
            {"id": 2, "session_id": "s1", "question": "Q2?", "created_at": "2024-01-01"},
        ])
        worker._mark_processing = AsyncMock()
        worker._process_question = AsyncMock(return_value=(True, "Ans", {"key": "val"}))
        worker._save_response = AsyncMock()
        stats = await worker.run_batch()
        assert stats["processed"] == 2
        assert stats["success"] == 2
        assert stats["failed"] == 0

    @pytest.mark.asyncio
    async def test_mixed_success_failure(self):
        worker = _make_worker()
        worker._get_pending_questions = AsyncMock(return_value=[
            {"id": 1, "session_id": "s1", "question": "Q1?", "created_at": "2024-01-01"},
            {"id": 2, "session_id": "s2", "question": "Q2?", "created_at": "2024-01-01"},
        ])
        worker._mark_processing = AsyncMock()
        results = iter([(True, "Ok", {}), (False, "Err", {"error": "fail"})])
        worker._process_question = AsyncMock(side_effect=lambda q: next(results))
        worker._save_response = AsyncMock()
        stats = await worker.run_batch()
        assert stats["processed"] == 2
        assert stats["success"] == 1
        assert stats["failed"] == 1


class TestWorkerClose:
    @pytest.mark.asyncio
    async def test_close_without_bot(self):
        worker = pw.PostgresAgentWorker()
        await worker.close()

    @pytest.mark.asyncio
    async def test_close_with_bot(self):
        worker = pw.PostgresAgentWorker()
        worker._bot = AsyncMock()
        await worker.close()
        worker._bot.aclose.assert_awaited_once()
