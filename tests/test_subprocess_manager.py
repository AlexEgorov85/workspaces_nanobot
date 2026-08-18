from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from lib.services.subprocess_manager import SubprocessManager


class TestSpawnStreamlit:
    def test_script_missing_returns_false(self, tmp_path):
        mgr = SubprocessManager(log_dir=tmp_path)
        assert mgr.spawn_streamlit(tmp_path / "missing.py") is False

    def test_successful_spawn(self, tmp_path):
        script = tmp_path / "app.py"
        script.write_text("", encoding="utf-8")
        proc = MagicMock()
        proc.terminate = MagicMock()
        proc.wait = MagicMock()
        proc.kill = MagicMock()

        mgr = SubprocessManager(log_dir=tmp_path)
        with patch("lib.services.subprocess_manager.subprocess.Popen", return_value=proc):
            assert mgr.spawn_streamlit(script, port=8501) is True

        mgr.terminate_all()  # закрыть log_handle
        # Лог-файл создан
        assert (tmp_path / "streamlit.log").exists()

    def test_spawn_failure_returns_false(self, tmp_path):
        script = tmp_path / "app.py"
        script.write_text("", encoding="utf-8")
        from subprocess import TimeoutExpired

        def _boom(*args, **kwargs):
            raise TimeoutExpired(cmd="streamlit", timeout=1)

        mgr = SubprocessManager(log_dir=tmp_path)
        with patch("lib.services.subprocess_manager.subprocess.Popen", side_effect=_boom):
            assert mgr.spawn_streamlit(script) is False


class TestTerminateAll:
    def test_terminate_then_wait(self, tmp_path):
        script = tmp_path / "app.py"
        script.write_text("", encoding="utf-8")
        proc = MagicMock()
        proc.terminate = MagicMock()
        proc.wait = MagicMock()
        proc.kill = MagicMock()

        with SubprocessManager(log_dir=tmp_path) as mgr:
            with patch("lib.services.subprocess_manager.subprocess.Popen", return_value=proc):
                mgr.spawn_streamlit(script)

        proc.terminate.assert_called_once()
        proc.wait.assert_called_once()

    def test_kill_on_wait_timeout(self, tmp_path):
        script = tmp_path / "app.py"
        script.write_text("", encoding="utf-8")
        proc = MagicMock()
        proc.terminate = MagicMock()
        proc.wait.side_effect = TimeoutError("wait timeout")
        proc.kill = MagicMock()

        with SubprocessManager(log_dir=tmp_path) as mgr:
            with patch("lib.services.subprocess_manager.subprocess.Popen", return_value=proc):
                mgr.spawn_streamlit(script)

        proc.kill.assert_called_once()
        # очередь процессов очищена
        assert mgr._processes == []