from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from lib.services.context_compaction import ContextCompactionService


def _settings(**overrides):
    gw = {"compact": {}}
    gw["compact"].update(overrides)
    return SimpleNamespace(gateway=gw)


def _make_session(messages=None, last_consolidated=0):
    s = MagicMock()
    s.messages = list(messages or [])
    s.last_consolidated = last_consolidated
    return s


def _make_agent(
    *, before_messages=None, after_messages=None, after_cursor=None, summary=None,
):
    consolidator = MagicMock()
    consolidator.compact_idle_session = AsyncMock(return_value=summary)
    consolidator.maybe_consolidate_by_tokens = AsyncMock()
    consolidator.estimate_session_prompt_tokens = AsyncMock(
        side_effect=[
            (1000, "chain"),
            (300, "chain"),
        ]
    )

    session_after = _make_session(
        messages=after_messages if after_messages is not None else [],
        last_consolidated=after_cursor if after_cursor is not None else 0,
    )
    sessions = MagicMock()
    session_before = _make_session(
        messages=list(before_messages)
        if before_messages is not None
        else [{"role": "user", "content": f"m{i}"} for i in range(34)],
        last_consolidated=0,
    )
    sessions.get_or_create = MagicMock(side_effect=[session_before, session_after])

    runtime = MagicMock()
    runtime.context_window_tokens = 65536
    runtime.generation.max_tokens = 4096

    agent = MagicMock()
    agent.sessions = sessions
    agent.consolidator = consolidator
    agent.runtime_for_session = MagicMock(return_value=runtime)
    return agent, session_before, session_after, consolidator, sessions


class TestFormatReport:
    def test_failure_with_reason(self):
        r = {"ok": False, "reason": "boom"}
        assert "Сжатие не выполнено" in ContextCompactionService.format_report(r)
        assert "boom" in ContextCompactionService.format_report(r)

    def test_idle_no_archive(self):
        r = {
            "ok": True, "session_key": "cli:1", "mode": "token",
            "archived_msgs": 0, "kept_msgs": 5, "tokens_before": 100,
            "tokens_after": 100, "summary": None, "raw_dump": False,
        }
        text = ContextCompactionService.format_report(r)
        assert "не потребовалось" in text
        assert "100" in text

    def test_with_archive_includes_summary(self):
        r = {
            "ok": True, "session_key": "postgres:42", "mode": "token",
            "archived_msgs": 12, "kept_msgs": 22, "tokens_before": 34500,
            "tokens_after": 12300, "summary": "краткая сводка диалога", "raw_dump": False,
        }
        text = ContextCompactionService.format_report(r)
        assert "краткая сводка" in text
        assert "Итог:" in text
        assert "12" in text
        assert "22" in text
        assert "34500" in text
        assert "12300" in text
        assert "≈" in text
        idx_summary = text.index("краткая сводка")
        idx_total = text.index("Итог:")
        assert idx_summary < idx_total

    def test_summary_truncated_to_long_string_passes_through(self):
        r = {
            "ok": True, "session_key": "k", "mode": "token",
            "archived_msgs": 1, "kept_msgs": 1, "tokens_before": 200,
            "tokens_after": 100, "summary": "x" * 500, "raw_dump": False,
        }
        text = ContextCompactionService.format_report(r)
        assert "Итог:" in text
        assert "xxxx" in text

    def test_summary_nothing_marker_dropped(self):
        r = {
            "ok": True, "session_key": "k", "mode": "token",
            "archived_msgs": 5, "kept_msgs": 1, "tokens_before": 200,
            "tokens_after": 100, "summary": "(nothing)", "raw_dump": False,
        }
        text = ContextCompactionService.format_report(r)
        assert "(nothing)" not in text
        assert "Итог:" in text

    def test_archive_no_summary_still_has_total(self):
        r = {
            "ok": True, "session_key": "k", "mode": "token",
            "archived_msgs": 5, "kept_msgs": 1, "tokens_before": 200,
            "tokens_after": 100, "summary": None, "raw_dump": True,
        }
        text = ContextCompactionService.format_report(r)
        assert "Итог:" in text
        assert "200" in text and "100" in text


class TestCompact:
    def test_disabled_returns_failure(self):
        agent = MagicMock()
        svc = ContextCompactionService(agent, settings=_settings(enabled=False))
        report = asyncio.run(svc.compact(session_key="cli:1"))
        assert report["ok"] is False
        assert "enabled=false" in report["reason"]
        agent.sessions.get_or_create.assert_not_called()

    def test_missing_session_key_no_request_context(self, monkeypatch):
        agent = MagicMock()
        agent.sessions = MagicMock()
        agent.consolidator = MagicMock()
        agent.runtime_for_session = MagicMock()
        svc = ContextCompactionService(agent, settings=_settings())
        monkeypatch.setattr(
            "nanobot.agent.tools.context.current_request_session_key",
            lambda: None,
        )
        report = asyncio.run(svc.compact())
        assert report["ok"] is False
        assert "session_key" in report["reason"]

    def test_token_compaction_archives_and_reports(self):
        agent, before, after, consolidator, sessions = _make_agent(
            after_messages=[{"role": "user", "content": f"m{i}"} for i in range(22)],
            after_cursor=12,
        )
        svc = ContextCompactionService(agent, settings=_settings())
        report = asyncio.run(svc.compact(session_key="cli:1"))
        assert report["ok"] is True
        assert report["mode"] == "token"
        assert report["archived_msgs"] == 12
        assert report["kept_msgs"] == 22
        assert report["tokens_before"] == 1000
        assert report["tokens_after"] == 300
        consolidator.maybe_consolidate_by_tokens.assert_awaited_once()
        consolidator.compact_idle_session.assert_not_called()

    def test_idle_compaction_uses_compact_idle(self):
        agent, _before, _after, consolidator, _sessions = _make_agent(
            after_messages=[{"role": "user", "content": "tail"}],
            after_cursor=0,
            summary="краткая сводка",
        )
        svc = ContextCompactionService(agent, settings=_settings())
        report = asyncio.run(svc.compact(session_key="cli:1", idle=True, max_suffix=4))
        assert report["ok"] is True
        assert report["mode"] == "idle"
        assert report["summary"] == "краткая сводка"
        consolidator.compact_idle_session.assert_awaited_once_with(
            "cli:1", runtime=agent.runtime_for_session.return_value, max_suffix=4,
        )

    def test_session_state_relies_on_nanobot_consolidator(self):
        """После ``compact()`` обновление ``_last_summary`` и ``last_consolidated``
        делает штатный ``Consolidator`` (``memory.py::_persist_last_summary``),
        а не наш сервис. Здесь имитируем нативное поведение консолидатора
        и проверяем, что результат попадает в сессию и переживёт ``compact()``.
        """
        real_session = SimpleNamespace(
            messages=[{"role": "user", "content": "tail"}],
            last_consolidated=0,
            metadata={},
            updated_at="2026-01-01T00:00:00",
        )
        sessions = MagicMock()
        sessions.get_or_create = MagicMock(return_value=real_session)
        runtime = MagicMock()
        runtime.context_window_tokens = 65536
        runtime.generation.max_tokens = 4096

        agent = MagicMock()
        agent.sessions = sessions
        agent.consolidator = MagicMock()
        agent.runtime_for_session = MagicMock(return_value=runtime)
        agent.consolidator.estimate_session_prompt_tokens = AsyncMock(
            side_effect=[(1000, "chain"), (300, "chain")]
        )

        async def fake_idle(key, *, runtime, max_suffix):
            real_session.metadata["_last_summary"] = {
                "text": "нативная сводка от Consolidator",
                "last_active": "2026-01-01T00:00:00",
            }
            agent.sessions.save(real_session)
            return "нативная сводка от Consolidator"

        agent.consolidator.compact_idle_session = AsyncMock(side_effect=fake_idle)

        svc = ContextCompactionService(agent, settings=_settings())
        asyncio.run(svc.compact(session_key="cli:1", idle=True))

        assert (
            real_session.metadata["_last_summary"]["text"]
            == "нативная сводка от Consolidator"
        )
        agent.sessions.save.assert_called_with(real_session)

    def test_no_extra_message_added_to_session(self):
        """Сервис НЕ добавляет служебных сообщений в ``session.messages`` —
        иначе они бы съедали только что освобождённые токены. Состояние
        сессии правит только нативный ``Consolidator``.
        """
        msgs_before = [{"role": "user", "content": f"m{i}"} for i in range(5)]
        agent, _before, session_after, _consolidator, _sessions = _make_agent(
            before_messages=msgs_before,
            after_messages=msgs_before,
            after_cursor=0,
            summary="",
        )
        svc = ContextCompactionService(agent, settings=_settings())
        asyncio.run(svc.compact(session_key="cli:1", idle=True))
        assert len(session_after.messages) == len(msgs_before)

    def test_idle_no_archive_returns_idle_report(self):
        full = [{"role": "user", "content": f"m{i}"} for i in range(5)]
        agent, _before, _after, consolidator, _sessions = _make_agent(
            before_messages=full,
            after_messages=full,
            after_cursor=0,
            summary="",
        )
        svc = ContextCompactionService(agent, settings=_settings())
        report = asyncio.run(svc.compact(session_key="cli:1", idle=True))
        assert report["ok"] is True
        assert report["archived_msgs"] == 0
        assert report["summary"] is None

    def test_compactor_failure_is_caught(self):
        agent, _before, _after, consolidator, _sessions = _make_agent()
        consolidator.maybe_consolidate_by_tokens.side_effect = RuntimeError("LLM down")
        svc = ContextCompactionService(agent, settings=_settings())
        report = asyncio.run(svc.compact(session_key="cli:1"))
        assert report["ok"] is False
        assert "LLM down" in report["reason"]


class TestCompactToolPatch:
    def test_registers_tool_when_enabled(self, monkeypatch):
        agent = MagicMock()
        agent.tools.register = MagicMock()

        class FakeTool:
            def __init__(self, svc):
                self.svc = svc
                self.name = "compact_context"

        monkeypatch.setattr(
            "lib.services.context_compaction.ContextCompactionService",
            lambda *_a, **_k: SimpleNamespace(enabled=True),
        )
        monkeypatch.setattr(
            "lib.tools.compact_context_tool.CompactContextTool", FakeTool,
        )
        from lib.services.runtime_patcher import RuntimePatcher

        ok, _detail = RuntimePatcher().patch_compact_tool(
            agent, settings=_settings(enabled=True),
        )
        assert ok is True
        agent.tools.register.assert_called_once()

    def test_skips_when_disabled(self, monkeypatch):
        agent = MagicMock()
        agent.tools.register = MagicMock()
        monkeypatch.setattr(
            "lib.services.context_compaction.ContextCompactionService",
            lambda *_a, **_k: SimpleNamespace(enabled=False),
        )
        from lib.services.runtime_patcher import RuntimePatcher

        ok, detail = RuntimePatcher().patch_compact_tool(
            agent, settings=_settings(enabled=False),
        )
        assert ok is False
        assert "enabled=false" in detail
        agent.tools.register.assert_not_called()


class TestRecordExternalCompaction:
    """record_external_compaction — единый путь записи для ручного и авто-сжатия."""

    @pytest.mark.asyncio
    async def test_calls_write_history_notice(self):
        agent = MagicMock()
        agent.sessions = MagicMock()
        svc = ContextCompactionService(agent, settings=_settings())
        svc._write_history_notice = AsyncMock()
        await svc.record_external_compaction(
            session_key="postgres:1", mode="idle", summary="svodka",
            archived_msgs=10, kept_msgs=20,
            tokens_before=2000, tokens_after=800,
        )
        svc._write_history_notice.assert_awaited_once()
        call = svc._write_history_notice.await_args
        key, report = call.args
        assert key == "postgres:1"
        assert report["mode"] == "idle"
        assert report["archived_msgs"] == 10
        assert report["summary"] == "svodka"

    @pytest.mark.asyncio
    async def test_skips_zero_archived(self):
        agent = MagicMock()
        agent.sessions = MagicMock()
        svc = ContextCompactionService(agent, settings=_settings())
        svc._write_history_notice = AsyncMock()
        await svc.record_external_compaction(
            session_key="postgres:1", mode="token", summary="x",
            archived_msgs=0, kept_msgs=5,
            tokens_before=100, tokens_after=100,
        )
        svc._write_history_notice.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_notify_disabled(self):
        agent = MagicMock()
        agent.sessions = MagicMock()
        svc = ContextCompactionService(agent, settings=_settings(
            enabled=True, notify_in_history=False,
        ))
        svc._write_history_notice = AsyncMock()
        await svc.record_external_compaction(
            session_key="postgres:1", mode="idle", summary="x",
            archived_msgs=10, kept_msgs=20,
            tokens_before=100, tokens_after=50,
        )
        svc._write_history_notice.assert_not_called()


class TestPatchCompactionTracking:
    def test_skips_when_disabled(self, monkeypatch):
        monkeypatch.setattr(
            "lib.services.context_compaction.ContextCompactionService",
            lambda *_a, **_k: SimpleNamespace(
                enabled=False, notify_in_history=True,
            ),
        )
        from lib.services.runtime_patcher import RuntimePatcher
        ok, detail = RuntimePatcher().patch_compaction_tracking(
            MagicMock(), settings=_settings(enabled=False),
        )
        assert ok is False and "enabled=false" in detail

    def test_skips_when_notify_disabled(self, monkeypatch):
        monkeypatch.setattr(
            "lib.services.context_compaction.ContextCompactionService",
            lambda *_a, **_k: SimpleNamespace(
                enabled=True, notify_in_history=False,
            ),
        )
        from lib.services.runtime_patcher import RuntimePatcher
        ok, detail = RuntimePatcher().patch_compaction_tracking(
            MagicMock(), settings=_settings(enabled=True, notify_in_history=False),
        )
        assert ok is False and "notify_in_history=false" in detail

    def test_archive_wrapper_calls_record_on_real_archive(self, monkeypatch):
        record_calls: list = []
        estimate_returns = iter([(1000, "c"), (300, "c")])

        class FakeSvc:
            enabled = True
            notify_in_history = True

            async def record_external_compaction(self, **kw):
                record_calls.append(kw)

            async def _estimate(self, *a, **kw):
                return next(estimate_returns)

        monkeypatch.setattr(
            "lib.services.context_compaction.ContextCompactionService",
            lambda *_a, **_k: FakeSvc(),
        )
        from lib.services.runtime_patcher import RuntimePatcher

        before = SimpleNamespace(
            messages=[1] * 20, last_consolidated=0, metadata={},
        )
        after = SimpleNamespace(
            messages=[1] * 8, last_consolidated=12,
            metadata={"_last_summary": {"text": "svodka"}},
        )
        sessions = MagicMock()
        sessions.get_or_create = MagicMock(side_effect=[before, after])
        runtime = MagicMock()

        async def fake_archive(key, *, runtime):
            return "svodka"

        auto = MagicMock()
        auto._archive = fake_archive
        agent = MagicMock()
        agent.sessions = sessions
        agent.consolidator = MagicMock()
        agent.auto_compact = auto

        ok, _ = RuntimePatcher().patch_compaction_tracking(agent, settings=_settings())
        assert ok is True
        asyncio.run(agent.auto_compact._archive("postgres:c1", runtime=runtime))
        assert len(record_calls) == 1
        assert record_calls[0]["mode"] == "idle"
        assert record_calls[0]["archived_msgs"] == 12
        assert record_calls[0]["summary"] == "svodka"

    def test_archive_wrapper_skips_when_no_archive(self, monkeypatch):
        record_calls: list = []

        class FakeSvc:
            enabled = True
            notify_in_history = True

            async def record_external_compaction(self, **kw):
                record_calls.append(kw)

            async def _estimate(self, *a, **kw):
                return (100, "c")

        monkeypatch.setattr(
            "lib.services.context_compaction.ContextCompactionService",
            lambda *_a, **_k: FakeSvc(),
        )
        from lib.services.runtime_patcher import RuntimePatcher

        same = SimpleNamespace(messages=[1] * 5, last_consolidated=0, metadata={})
        sessions = MagicMock()
        sessions.get_or_create = MagicMock(return_value=same)
        runtime = MagicMock()

        async def fake_archive(key, *, runtime):
            return ""  # нечего архивировать

        auto = MagicMock()
        auto._archive = fake_archive
        agent = MagicMock()
        agent.sessions = sessions
        agent.consolidator = MagicMock()
        agent.auto_compact = auto

        RuntimePatcher().patch_compaction_tracking(agent, settings=_settings())
        asyncio.run(agent.auto_compact._archive("postgres:c1", runtime=runtime))
        assert record_calls == []

    def test_maybe_consolidate_wrapper_calls_record_on_real_archive(self, monkeypatch):
        record_calls: list = []

        class FakeSvc:
            enabled = True
            notify_in_history = True

            async def record_external_compaction(self, **kw):
                record_calls.append(kw)

            async def _estimate(self, *a, **kw):
                return (100, "c")

        monkeypatch.setattr(
            "lib.services.context_compaction.ContextCompactionService",
            lambda *_a, **_k: FakeSvc(),
        )
        from lib.services.runtime_patcher import RuntimePatcher

        before = SimpleNamespace(
            key="postgres:c1", messages=[1] * 15,
            last_consolidated=0, metadata={},
        )
        after = SimpleNamespace(
            key="postgres:c1", messages=[1] * 10,
            last_consolidated=5,
            metadata={"_last_summary": {"text": "ns"}},
        )
        sessions = MagicMock()
        # _wrapped зовёт get_or_create ровно один раз (для after);
        # замер before идёт через аргумент.
        sessions.get_or_create = MagicMock(return_value=after)
        runtime = MagicMock()
        consolidator = MagicMock()
        consolidator.maybe_consolidate_by_tokens = AsyncMock()
        agent = MagicMock()
        agent.sessions = sessions
        agent.consolidator = consolidator
        agent.auto_compact = MagicMock()

        ok, _ = RuntimePatcher().patch_compaction_tracking(agent, settings=_settings())
        assert ok is True
        asyncio.run(
            agent.consolidator.maybe_consolidate_by_tokens(before, runtime=runtime)
        )
        assert len(record_calls) == 1
        assert record_calls[0]["mode"] == "token"
        assert record_calls[0]["archived_msgs"] == 5
        assert record_calls[0]["summary"] == "ns"
