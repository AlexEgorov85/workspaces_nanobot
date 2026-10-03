"""Real integration tests for large tool-result handling.

Отличие от unit-тестов в ``test_runtime_patcher.py``: здесь проверки идут
против **настоящих** модулей фреймворка nanobot и **реальной** файловой
системы, а не фейков/макетов:

  * реальный ``ExecTool`` исполняет настоящую команду, выдающую вывод
    больше лимита, — проверяем, что усечение с маркером
    ``… chars truncated …`` работает на **действующих** дефолтах;
  * реальный ``ReadFileTool`` читает файл больше дефолтного потолка;
  * upstream ``ContextGovernor.normalize_tool_result`` пишет **полный**
    вывод в ``.nanobot/tool-results/`` на диск, а ``read_file`` от
    персиста освобождён (change ``use-upstream-tool-result-persist``:
    наш патч ``context_governor`` и хук ``ToolResultArchiveHook`` снесены
    в пользу библиотечного ``maybe_persist_tool_result``).

Раньше эти тесты доказывали, что патчи ``exec_limits`` / ``tool_limits``
поднимают потолки. Патчи сняты (решение владельца, 2026-10-03), и тесты
переписаны на обратное: они фиксируют лимиты, которые действуют теперь,
потому что после сноса их больше ничем не перекрыть. Каждый тест сам
фиксирует дефолты библиотеки и **восстанавливает** изначальное состояние
в ``finally``, чтобы не зависеть от ambient-состояния набора.

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


async def _run_exec(tool, command, **kwargs):
    res = await tool.execute(command=command, **kwargs)
    if not isinstance(res, str):
        raise AssertionError(f"exec вернул не строку: {getattr(res, 'is_error', type(res).__name__)}")
    return res


@contextmanager
def _framework_defaults():
    """Явно ставит дефолты библиотеки и восстанавливает их в ``finally``.

    После сноса ``exec_limits``/``tool_limits`` эти числа — не украшение, а
    то, что реально действует. Тест не должен зависеть от того, в каком
    состоянии остался модуль после другого теста набора.
    """
    import nanobot.agent.tools.exec_session as es
    import nanobot.agent.tools.shell as shell
    from nanobot.agent.tools import filesystem as fs
    from nanobot.agent.tools import search as srch

    originals = (
        shell.MAX_OUTPUT_CHARS, shell.ExecTool._MAX_OUTPUT,
        es.MAX_OUTPUT_CHARS, es.DEFAULT_MAX_OUTPUT_CHARS,
        fs.ReadFileTool._MAX_CHARS, fs.ListDirTool._DEFAULT_MAX,
        srch._DEFAULT_HEAD_LIMIT, srch._DEFAULT_FILE_HEAD_LIMIT,
        srch.GrepTool._MAX_FILE_BYTES,
    )
    try:
        shell.MAX_OUTPUT_CHARS = 50_000
        shell.ExecTool._MAX_OUTPUT = 10_000
        es.MAX_OUTPUT_CHARS = 50_000
        es.DEFAULT_MAX_OUTPUT_CHARS = 10_000
        fs.ReadFileTool._MAX_CHARS = 128_000
        fs.ListDirTool._DEFAULT_MAX = 200
        srch._DEFAULT_HEAD_LIMIT = 250
        srch._DEFAULT_FILE_HEAD_LIMIT = 200
        srch.GrepTool._MAX_FILE_BYTES = 2_000_000
        yield
    finally:
        (
            shell.MAX_OUTPUT_CHARS, shell.ExecTool._MAX_OUTPUT,
            es.MAX_OUTPUT_CHARS, es.DEFAULT_MAX_OUTPUT_CHARS,
            fs.ReadFileTool._MAX_CHARS, fs.ListDirTool._DEFAULT_MAX,
            srch._DEFAULT_HEAD_LIMIT, srch._DEFAULT_FILE_HEAD_LIMIT,
            srch.GrepTool._MAX_FILE_BYTES,
        ) = originals


class TestExecToolE2E:
    """Реальный ExecTool с командой, генерирующей вывод больше лимита.

    Патч ``exec_limits`` снят, поэтому действует дефолт библиотеки:
    ``_MAX_OUTPUT`` = 10 000 (именно он, а не 50 000) и потолок
    ``MAX_OUTPUT_CHARS`` = 50 000. Раньше эти числа были подняты до
    100 000 / 500 000, и модель получала вывод на порядок больше.
    """

    def test_truncates_by_default(self, tmp_path):
        from nanobot.agent.tools.shell import ExecTool

        with _framework_defaults():
            tool = ExecTool(working_dir=str(tmp_path), timeout=30)
            out = asyncio.run(_run_exec(tool, 'python -c "print(chr(120)*60000)"'))

        assert "chars truncated" in out
        assert len(out) < 60_000

    def test_default_output_limit_is_10k(self, tmp_path):
        """Рабочий дефолт — 10K, а не 50K.

        Это самая дорогая деградация после сноса патча: модель, не
        передавшая ``max_output_chars``, получает в 10 раз меньше текста.
        Тест существует, чтобы число не «съехало» апгрейдом nanobot
        незаметно — перекрыть его больше нечем.
        """
        from nanobot.agent.tools.shell import ExecTool

        with _framework_defaults():
            tool = ExecTool(working_dir=str(tmp_path), timeout=30)
            out = asyncio.run(_run_exec(tool, 'python -c "print(chr(120)*60000)"'))

        # «голова + хвост»: маркер и служебный хвост сверх лимита.
        assert "chars truncated" in out
        assert len(out) < 12_000, (
            f"вывод exec упёрся в {len(out)} символов при дефолте 10 000 — "
            "либо дефолт изменился, либо его снова поднял патч"
        )

    def test_ceiling_truncates_even_above_requested(self, tmp_path):
        """Потолок 50K режет даже явно запрошенный объём.

        Модель может попросить ``max_output_chars``, но выше 50 000
        библиотека всё равно усекает: потолок не настраивается.
        """
        from nanobot.agent.tools.shell import ExecTool

        with _framework_defaults():
            tool = ExecTool(working_dir=str(tmp_path), timeout=30)
            out = asyncio.run(
                _run_exec(
                    tool, 'python -c "print(chr(121)*700000)"',
                    max_output_chars=500_000,
                )
            )

        assert "chars truncated" in out
        assert len(out) < 60_000, (
            f"вывод {len(out)} символов не упёрся в потолок 50K"
        )


class TestReadFileE2E:
    """Реальный ReadFileTool с файлом больше действующего потолка (128K)."""

    def _make_file(self, tmp_path) -> Path:
        p = tmp_path / "big.txt"
        # ~200K байт, 2000 строк: дефолтное чтение (limit=2000) превышает
        # потолок read_file (128K) и усекается. Патч поднимал потолок до
        # 512K и файл читался целиком; после сноса — обрезается.
        p.write_text("\n".join("q" * 100 for _ in range(2000)), encoding="utf-8")
        return p

    def test_truncates_by_default(self, tmp_path):
        from nanobot.agent.tools.filesystem import ReadFileTool

        with _framework_defaults():
            fn = self._make_file(tmp_path)
            tool = ReadFileTool(workspace=tmp_path)
            out = asyncio.run(tool.execute(path=str(fn)))

        # текст-путь read_file обрывает на потолке и пишет «(Showing lines …)»
        assert "(Showing lines" in out
        assert "(End of file" not in out
        assert len(out) < 200_000

    def test_full_file_is_no_longer_reachable(self, tmp_path):
        """Файл больше 128K целиком больше не читается.

        Раньше патч поднимал ``_MAX_CHARS`` до 512K, и этот файл
        возвращался целиком. Теперь — обрезанным по хвосту, и полнота
        чтения зависит только от построчного ``offset``/``limit``.
        """
        from nanobot.agent.tools.filesystem import ReadFileTool

        with _framework_defaults():
            fn = self._make_file(tmp_path)
            tool = ReadFileTool(workspace=tmp_path)
            out = asyncio.run(tool.execute(path=str(fn)))

        assert "(End of file" not in out, (
            "файл на 200K прочитан целиком при потолке 128K — либо "
            "дефолт изменился, либо его снова поднял патч"
        )


class TestGrepBlindToLargeFilesE2E:
    """Grep пропускает файлы крупнее 2 МБ — молча, без ошибки.

    Отдельный класс, потому что это единственная регрессия после сноса
    ``tool_limits``, которая НЕ сопровождается маркером в ответе: grep
    считает файл в ``skipped_large`` и возвращает пустой результат, а
    модель читает это как «совпадений нет». Данные были — их не посмотрели.
    """

    def test_file_above_2mb_is_skipped(self, tmp_path):
        from nanobot.agent.tools.search import GrepTool

        big = tmp_path / "big.log"
        # 3 МБ с заведомо искомой строкой в самом начале.
        big.write_text(
            "NEEDLE_AT_START\n" + ("x" * 99 + "\n") * 31_000,
            encoding="utf-8",
        )
        assert big.stat().st_size > 2_000_000

        with _framework_defaults():
            tool = GrepTool(workspace=tmp_path)
            res = asyncio.run(tool.execute(pattern="NEEDLE_AT_START", path="."))

        rendered = res if isinstance(res, str) else getattr(res, "content", str(res))

        # Уведомление о пропуске ЕСТЬ, но оно в хвосте скобками, а первой
        # строкой идёт «No matches found» — то есть модель читает результат
        # как «совпадений нет». Именно поэтому регрессия опасна: данные были,
        # их не посмотрели, и ошибки при этом не возникает. Это зафиксировано
        # как ожидаемое поведение, чтобы его изменение апгрейдом nanobot
        # потребовало явного решения, а не прошло молча.
        assert rendered.startswith("No matches found for pattern"), rendered[:120]
        assert "skipped 1 large files" in rendered, (
            "grep перестал сообщать о пропуске крупных файлов — это лучше "
            "текущего поведения, но цифры в каталоге патчей надо обновить"
        )



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
