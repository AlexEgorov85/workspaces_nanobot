"""Real integration tests for large tool-result handling.

Отличие от unit-тестов в ``test_runtime_patcher.py``: здесь проверки идут
против **настоящих** модулей фреймворка nanobot и **реальной** файловой
системы, а не фейков/макетов:

  * реальный ``ExecTool`` исполняет настоящую команду, выдающую вывод
    больше лимитов, — проверяем, что без патча маркер ``… chars
    truncated …`` есть, а после патча его нет (данные целые);
  * реальный ``ReadFileTool`` читает файл больше дефолтного потолка;
  * ``_save_turn``-обёртка и ``ContextGovernor.normalize_tool_result``
    пишут **полные** файлы в ``data_store/`` на диск.

Каждый тест сам ставит патч и **восстанавливает** изначальное состояние в
``finally``, чтобы не влиять на остальной набор.

Эти тесты требуют запуска настоящих subprocess и потому несколько медленнее
unit-тестов, но не требуют внешних сервисов (БД/сеть).
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

_workspace_path = str(Path(__file__).resolve().parent.parent / "workspace")
if _workspace_path not in sys.path:
    sys.path.insert(0, _workspace_path)
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
_user_site = r"C:\Users\Алексей\AppData\Roaming\Python\Python314\site-packages"
if _user_site not in sys.path:
    sys.path.insert(0, _user_site)

from lib.services.runtime_patcher import RuntimePatcher  # noqa: E402


class _Settings:
    def __init__(self, **overrides):
        gw = {
            "persist_threshold": 0,
            "persist_max_files": 100,
            "persist_max_age_hours": 0,
            "tool_result_limits": {},
        }
        gw.update(overrides)
        self.gateway = gw


@contextmanager
def _patched_exec(settings=None):
    import nanobot.agent.tools.shell as shell
    import nanobot.agent.tools.exec_session as es

    orig = (
        shell.MAX_OUTPUT_CHARS,
        shell.ExecTool._MAX_OUTPUT,
        shell.ExecTool.parameters,
        es.MAX_OUTPUT_CHARS,
        es.DEFAULT_MAX_OUTPUT_CHARS,
        es.WriteStdinTool.parameters,
    )
    try:
        RuntimePatcher().patch_exec_limits(settings or _Settings(
            persist_threshold=5000,
            tool_result_limits={
                "exec_max_output_chars": 500_000,
                "exec_default_output_chars": 100_000,
            },
        ))
        yield
    finally:
        (
            shell.MAX_OUTPUT_CHARS,
            shell.ExecTool._MAX_OUTPUT,
            shell.ExecTool.parameters,
            es.MAX_OUTPUT_CHARS,
            es.DEFAULT_MAX_OUTPUT_CHARS,
            es.WriteStdinTool.parameters,
        ) = orig


@contextmanager
def _patched_tool_limits(settings=None):
    from nanobot.agent.tools import filesystem as fs
    from nanobot.agent.tools import search as srch

    orig = (
        fs.ReadFileTool._MAX_CHARS,
        fs.ListDirTool._DEFAULT_MAX,
        srch._DEFAULT_HEAD_LIMIT,
        srch._DEFAULT_FILE_HEAD_LIMIT,
        srch.GrepTool._MAX_FILE_BYTES,
    )
    try:
        RuntimePatcher().patch_tool_limits(settings or _Settings(
            persist_threshold=5000,
            tool_result_limits={
                "read_file_max_chars": 512_000,
                "grep_head_limit": 500,
                "grep_file_head_limit": 400,
                "grep_max_file_bytes": 20_000_000,
                "list_dir_max_entries": 500,
            },
        ))
        yield
    finally:
        (
            fs.ReadFileTool._MAX_CHARS,
            fs.ListDirTool._DEFAULT_MAX,
            srch._DEFAULT_HEAD_LIMIT,
            srch._DEFAULT_FILE_HEAD_LIMIT,
            srch.GrepTool._MAX_FILE_BYTES,
        ) = orig


async def _run_exec(tool, command, **kwargs):
    res = await tool.execute(command=command, **kwargs)
    if not isinstance(res, str):
        raise AssertionError(f"exec вернул не строку: {getattr(res, 'is_error', type(res).__name__)}")
    return res


class TestExecToolE2E:
    """Реальный ExecTool с командой, генерирующей вывод больше лимита."""

    def test_truncates_by_default(self, tmp_path):
        from nanobot.agent.tools.shell import ExecTool
        import nanobot.agent.tools.shell as shell
        import nanobot.agent.tools.exec_session as es

        # Фиксируем ДЕФОЛТНЫЕ рамки фреймворка явно (не полагаясь на ambient-состояние,
        # которое может быть уже пропатчено другими тестами набора).
        orig = (
            shell.MAX_OUTPUT_CHARS, shell.ExecTool._MAX_OUTPUT,
            es.MAX_OUTPUT_CHARS, es.DEFAULT_MAX_OUTPUT_CHARS,
        )
        try:
            shell.MAX_OUTPUT_CHARS = 50_000
            shell.ExecTool._MAX_OUTPUT = 10_000
            es.MAX_OUTPUT_CHARS = 50_000
            es.DEFAULT_MAX_OUTPUT_CHARS = 10_000

            tool = ExecTool(working_dir=str(tmp_path), timeout=30)
            out = asyncio.run(_run_exec(tool, 'python -c "print(chr(120)*60000)"'))
        finally:
            (
                shell.MAX_OUTPUT_CHARS, shell.ExecTool._MAX_OUTPUT,
                es.MAX_OUTPUT_CHARS, es.DEFAULT_MAX_OUTPUT_CHARS,
            ) = orig

        assert len(out) < 60_000
        assert "chars truncated" in out

    def test_patch_preserves_full_output(self, tmp_path):
        from nanobot.agent.tools.shell import ExecTool

        tool = ExecTool(working_dir=str(tmp_path), timeout=30)
        with _patched_exec():
            out = asyncio.run(_run_exec(tool, 'python -c "print(chr(120)*60000)"'))
        # 60_000 'x' + перевод строки + служебный хвост '\nExit code: 0'
        assert len(out) >= 60_000
        assert "chars truncated" not in out

    def test_truncation_still_works_above_new_ceiling(self, tmp_path):
        from nanobot.agent.tools.shell import ExecTool

        tool = ExecTool(working_dir=str(tmp_path), timeout=30)
        with _patched_exec():
            # 700K символов > нового потолка 500K — механизм усечения жив,
            # просто срабатывает на большем пороге.
            out = asyncio.run(_run_exec(tool, 'python -c "print(chr(121)*700000)"'))
        assert "chars truncated" in out
        assert len(out) < 700_000


class TestReadFileE2E:
    """Реальный ReadFileTool с файлом больше дефолтного потолка (128K)."""

    def _make_file(self, tmp_path) -> Path:
        p = tmp_path / "big.txt"
        # ~200K байт, 2000 строк: дефолтное чтение (limit=2000) превышает
        # потолок read_file (128K) и усекается; после патча (512K) — полный вывод.
        p.write_text("\n".join("q" * 100 for _ in range(2000)), encoding="utf-8")
        return p

    def test_truncates_by_default(self, tmp_path):
        from nanobot.agent.tools.filesystem import ReadFileTool
        from nanobot.agent.tools import filesystem as fs

        orig = fs.ReadFileTool._MAX_CHARS
        try:
            fs.ReadFileTool._MAX_CHARS = 128_000  # дефолт фреймворка (против ambient-патча)
            fn = self._make_file(tmp_path)
            tool = ReadFileTool(workspace=tmp_path)
            out = asyncio.run(tool.execute(path=str(fn)))
        finally:
            fs.ReadFileTool._MAX_CHARS = orig

        # текст-путь read_file обрывает на потолке и пишет «(Showing lines …)»
        assert "(Showing lines" in out
        assert "(End of file" not in out
        assert len(out) < 200_000

    def test_patch_reads_full_file(self, tmp_path):
        from nanobot.agent.tools.filesystem import ReadFileTool

        fn = self._make_file(tmp_path)
        tool = ReadFileTool(workspace=tmp_path)
        with _patched_tool_limits():
            out = asyncio.run(tool.execute(path=str(fn)))
        # после поднятия потолка файл прочитан целиком
        assert "(Showing lines" not in out
        assert "(End of file" in out
        assert len(out) >= 200_000


class TestSaveTurnE2E:
    """_save_turn-обёртка пишет ПОЛНЫЙ файл в data_store и подменяет историю."""

    def _apply(self, workspace_dir):
        captured = {}

        def _original(session, messages, skip, *, turn_latency_ms=None):
            captured["messages"] = list(messages)
            captured["turn_latency_ms"] = turn_latency_ms
            return None

        class FakeAgent:
            max_tool_result_chars = 16_000

            def __init__(self):
                self._save_turn = _original

        agent = FakeAgent()
        settings = _Settings(
            persist_threshold=5000,
            persist_max_files=100,
            persist_max_age_hours=0,
        )
        ok, detail = RuntimePatcher().patch_save_turn(settings, workspace_dir, agent)
        assert ok, detail
        return agent, captured

    def test_archives_full_content_to_disk(self, tmp_path):
        class _Session:
            key = "e2e"

        big = "x" * 100_000
        msg = {"role": "tool", "content": big, "tool_call_id": "t1", "name": "exec"}
        agent, captured = self._apply(tmp_path)

        agent._save_turn(_Session(), [msg], 0)

        # история подменена ссылкой
        assert captured["messages"][0]["content"].startswith("[Result saved to data_store/")
        # на диске лежит ПОЛНЫЙ результат
        results = list((tmp_path / "data_store" / "cache" / "sessions" / "e2e" / "results").iterdir())
        assert len(results) == 1
        assert results[0].read_text(encoding="utf-8") == big

    def test_small_result_not_archived(self, tmp_path):
        class _Session:
            key = "e2e2"

        msg = {"role": "tool", "content": "small", "tool_call_id": "t1", "name": "read"}
        agent, captured = self._apply(tmp_path)

        agent._save_turn(_Session(), [msg], 0)

        assert captured["messages"][0]["content"] == "small"
        results_dir = tmp_path / "data_store" / "cache" / "sessions" / "e2e2" / "results"
        assert not results_dir.exists() or not list(results_dir.iterdir())


class TestContextGovernorE2E:
    """ContextGovernor.normalize_tool_result persist-путь с реальным файлом."""

    def test_persists_full_content_to_disk(self, tmp_path):
        from nanobot.agent.context_governance import ContextGovernor

        original = ContextGovernor.normalize_tool_result
        try:
            settings = _Settings(persist_threshold=5000)
            ok, detail = RuntimePatcher().patch_context_governor(
                MagicMock(), settings, tmp_path
            )
            assert ok, detail

            cfg = SimpleNamespace(session_key="cg-e2e", workspace=str(tmp_path))
            big = "y" * 50_000
            res = ContextGovernor.normalize_tool_result(cfg, "tid1", "exec", big)

            assert isinstance(res, str)
            assert res.startswith("[Result saved to data_store/")
            results = list((tmp_path / "data_store" / "cache" / "sessions" / "cg-e2e" / "results").iterdir())
            assert len(results) == 1
            assert results[0].read_text(encoding="utf-8") == big
        finally:
            ContextGovernor.normalize_tool_result = original

    def test_read_file_is_exempt(self, tmp_path):
        from nanobot.agent.context_governance import ContextGovernor

        original = ContextGovernor.normalize_tool_result
        try:
            settings = _Settings(persist_threshold=5)
            ok, detail = RuntimePatcher().patch_context_governor(
                MagicMock(), settings, tmp_path
            )
            assert ok, detail

            cfg = SimpleNamespace(session_key="cg-exempt", workspace=str(tmp_path))
            big = "z" * 50_000
            res = ContextGovernor.normalize_tool_result(cfg, "tid2", "read_file", big)
            # read_file exempt: возврат как есть, файл не пишется
            assert res == big
            results_dir = tmp_path / "data_store" / "cache" / "sessions" / "cg-exempt" / "results"
            assert not results_dir.exists() or not list(results_dir.iterdir())
        finally:
            ContextGovernor.normalize_tool_result = original