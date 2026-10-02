"""Real integration tests for large tool-result handling.

Отличие от unit-тестов в ``test_runtime_patcher.py``: здесь проверки идут
против **настоящих** модулей фреймворка nanobot и **реальной** файловой
системы, а не фейков/макетов:

  * реальный ``ExecTool`` исполняет настоящую команду, выдающую вывод
    больше лимитов, — проверяем, что без патча маркер ``… chars
    truncated …`` есть, а после патча его нет (данные целые);
  * реальный ``ReadFileTool`` читает файл больше дефолтного потолка;
  * upstream ``ContextGovernor.normalize_tool_result`` пишет **полный**
    вывод в ``.nanobot/tool-results/`` на диск, а ``read_file`` от
    персиста освобождён (change ``use-upstream-tool-result-persist``:
    наш патч ``context_governor`` и хук ``ToolResultArchiveHook`` снесены
    в пользу библиотечного ``maybe_persist_tool_result``).

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

import pytest

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


@pytest.fixture(autouse=True)
def _auto_seed_context_bridge(monkeypatch):
    """Засеять bridge и подменить _session_key_of, чтобы
    ``_attach_context_window`` не поднимал ``ContextWindowNotSeededError``
    (контракт после opencode change post-0.3.5-patches-cleanup).
    """
    from lib.hooks.database_logging_hook import (
        _CONTEXT_BRIDGE,
        _CONTEXT_BRIDGE_LOCK,
        seed_context_window,
    )

    session_key = "test:e2e:save_turn"
    seed_context_window(session_key, limit=40000, model="test-model")
    monkeypatch.setattr(
        "lib.services.runtime_patcher._session_key_of",
        lambda msg: session_key,
    )
    yield
    with _CONTEXT_BRIDGE_LOCK:
        _CONTEXT_BRIDGE.pop(session_key, None)


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

    orig = [
        shell.MAX_OUTPUT_CHARS,
        shell.ExecTool._MAX_OUTPUT,
        shell.ExecTool.parameters,
        es.MAX_OUTPUT_CHARS,
        es.DEFAULT_MAX_OUTPUT_CHARS,
    ]
    has_ws = hasattr(es, "WriteStdinTool")
    if has_ws:
        orig.append(es.WriteStdinTool.parameters)
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
        restore = orig[:5]
        shell.MAX_OUTPUT_CHARS = restore[0]
        shell.ExecTool._MAX_OUTPUT = restore[1]
        shell.ExecTool.parameters = restore[2]
        es.MAX_OUTPUT_CHARS = restore[3]
        es.DEFAULT_MAX_OUTPUT_CHARS = restore[4]
        if has_ws:
            es.WriteStdinTool.parameters = restore[5]


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



class TestUpstreamToolResultPersistE2E:
    """Персист больших результатов tool'ов — против настоящего nanobot 0.3.5.

    Раньше здесь проверялся наш патч ``context_governor`` (write в
    ``data_store/``). Он снесён: то же делает
    ``maybe_persist_tool_result`` внутри
    ``ContextGovernor.normalize_tool_result``. Тесты переписаны на
    библиотечный путь — иначе после сноса патча механизм остался бы
    без e2e-покрытия, а именно он (а не патч) теперь отвечает за то,
    что большой вывод не «съедается».
    """

    @staticmethod
    def _cfg(tmp_path: Path, session_key: str, max_chars: int):
        """Три поля, которые ``normalize_tool_result`` реально читает.

        Берётся ``SimpleNamespace``, а не настоящий
        ``ContextGovernanceConfig``: конструктор требует живой
        ``LLMProvider`` и ``ToolRegistry``, а проверять тут нужно только
        персист-ветку.
        """
        return SimpleNamespace(
            session_key=session_key,
            workspace=tmp_path,
            max_tool_result_chars=max_chars,
        )

    def test_persists_full_content_to_disk(self, tmp_path: Path):
        from nanobot.agent.context_governance import ContextGovernor

        big = "y" * 50_000
        cfg = self._cfg(tmp_path, "cg-e2e", max_chars=5_000)

        res = ContextGovernor.normalize_tool_result(cfg, "tid1", "exec", big)

        assert isinstance(res, str)
        # В историю уходит ссылка, а не сам вывод.
        assert res != big
        assert "[tool output persisted]" in res
        assert "Full output saved to workspace path:" in res

        bucket = tmp_path / ".nanobot" / "tool-results" / "cg-e2e"
        written = list(bucket.glob("*.txt"))
        assert len(written) == 1, f"ожидался один файл результата в {bucket}"
        # Полный, не обрезанный и не превью.
        assert written[0].read_text(encoding="utf-8") == big

    def test_read_file_is_exempt(self, tmp_path: Path):
        """``read_file`` не персистится: иначе persist -> read -> persist."""
        from nanobot.agent.context_governance import ContextGovernor

        big = "z" * 50_000
        cfg = self._cfg(tmp_path, "cg-exempt", max_chars=5_000)

        res = ContextGovernor.normalize_tool_result(cfg, "tid2", "read_file", big)

        assert res == big
        bucket = tmp_path / ".nanobot" / "tool-results" / "cg-exempt"
        assert not bucket.exists() or not list(bucket.iterdir())

    def test_small_result_is_untouched(self, tmp_path: Path):
        """Короткий вывод не персистится и не искажается."""
        from nanobot.agent.context_governance import ContextGovernor

        small = "короткий вывод"
        cfg = self._cfg(tmp_path, "cg-small", max_chars=5_000)

        assert ContextGovernor.normalize_tool_result(
            cfg, "tid3", "exec", small
        ) == small

    def test_empty_result_is_replaced(self, tmp_path: Path):
        """``ensure_nonempty_tool_result`` — тоже наша была забота."""
        from nanobot.agent.context_governance import ContextGovernor

        cfg = self._cfg(tmp_path, "cg-empty", max_chars=5_000)

        res = ContextGovernor.normalize_tool_result(cfg, "tid4", "exec", "")

        assert res != ""
        assert isinstance(res, str) and res.strip()
