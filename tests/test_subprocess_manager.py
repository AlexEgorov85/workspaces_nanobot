from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import config
from lib.services.subprocess_manager import SubprocessManager


@pytest.fixture
def _init_test_settings():
    """Init SETTINGS через ``_initialize_settings(profile='test')`` перед тестом
    (после теста оставляем proxy инициализированной — идемпотентно для
    следующих тестов в той же фазе pytest, ленивая proxy это позволяет)."""
    if not config.is_settings_initialized():
        config._initialize_settings(profile="test")


class TestSpawnStreamlit:
    def test_script_missing_returns_false(self, tmp_path, _init_test_settings):
        mgr = SubprocessManager(log_dir=tmp_path)
        assert mgr.spawn_streamlit(tmp_path / "missing.py") is False

    def test_successful_spawn(self, tmp_path, _init_test_settings):
        script = tmp_path / "app.py"
        script.write_text("", encoding="utf-8")
        proc = MagicMock()
        proc.terminate = MagicMock()
        proc.wait = MagicMock()
        proc.kill = MagicMock()

        mgr = SubprocessManager(log_dir=tmp_path)
        with patch("lib.services.subprocess_manager.subprocess.Popen", return_value=proc) as popen_call:
            assert mgr.spawn_streamlit(script, port=8501) is True
            # Проверить, что ``--profile=test`` проброшен в argv child
            args = popen_call.call_args.args[0]
            assert "--profile=test" in args, (
                f"argv child должен содержать --profile=<SETTINGS[profile]>={config.SETTINGS['profile']!r}; "
                f"получено: {args}"
            )

        mgr.terminate_all()  # закрыть log_handle
        # Лог-файл создан
        assert (tmp_path / "streamlit.log").exists()

    def test_spawn_failure_returns_false(self, tmp_path, _init_test_settings):
        script = tmp_path / "app.py"
        script.write_text("", encoding="utf-8")
        from subprocess import TimeoutExpired

        def _boom(*args, **kwargs):
            raise TimeoutExpired(cmd="streamlit", timeout=1)

        mgr = SubprocessManager(log_dir=tmp_path)
        with patch("lib.services.subprocess_manager.subprocess.Popen", side_effect=_boom):
            assert mgr.spawn_streamlit(script) is False


class TestTerminateAll:
    def test_terminate_then_wait(self, tmp_path, _init_test_settings):
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

    def test_kill_on_wait_timeout(self, tmp_path, _init_test_settings):
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
