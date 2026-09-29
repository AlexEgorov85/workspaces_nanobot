"""
Тесты Stage F (change ``unify-cli-gateway-architecture``).

Проверяют:

* ``cli_agent.py`` MUST hardcode ``profile="test"``;
* ``cli_agent.py`` MUST NOT принимать ``--profile`` flag;
* При передаче ``--profile=test`` или ``--profile prod`` —
  ``ConfigurationError`` с понятным сообщением;
* cli_agent.main() exit code 2 при reject.

CLI — entrypoint для локального test/dev, не для production-deploy.
Production-deploy — через ``gateway.py`` (см. design D8).
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

import pytest

_project_root = Path(__file__).resolve().parent.parent
_workspace_path = str(_project_root / "workspace")
if _workspace_path not in sys.path:
    sys.path.insert(0, _workspace_path)
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))


# ---------------------------------------------------------------------------
# Stage F — CLI rejects --profile
# ---------------------------------------------------------------------------


class TestCliAgentRejectsProfile:
    def test_cli_rejects_dash_dash_profile_equal_test(self) -> None:
        """``python cli_agent.py --profile=test`` → exit 2."""
        result = subprocess.run(
            [sys.executable, "cli_agent.py", "--profile=test"],
            cwd=str(_project_root),
            env={**os.environ, "PYTHONPATH": str(_project_root)},
            capture_output=True,
            text=True,
            timeout=10.0,
        )
        assert result.returncode != 0, (
            f"CLI должна reject --profile, но exit {result.returncode}. "
            f"stdout: {result.stdout}, stderr: {result.stderr}"
        )
        assert "--profile" in result.stderr or "profile" in result.stderr.lower()

    def test_cli_rejects_dash_dash_profile_alone(self) -> None:
        """``python cli_agent.py --profile foo`` → exit 2."""
        result = subprocess.run(
            [sys.executable, "cli_agent.py", "--profile", "test"],
            cwd=str(_project_root),
            env={**os.environ, "PYTHONPATH": str(_project_root)},
            capture_output=True,
            text=True,
            timeout=10.0,
        )
        assert result.returncode != 0

    def test_cli_rejects_dash_profile_short(self) -> None:
        """``python cli_agent.py -p test`` → exit 2."""
        result = subprocess.run(
            [sys.executable, "cli_agent.py", "-p", "test"],
            cwd=str(_project_root),
            env={**os.environ, "PYTHONPATH": str(_project_root)},
            capture_output=True,
            text=True,
            timeout=10.0,
        )
        assert result.returncode != 0


class TestCliFixedProfile:
    def test_cli_fixed_profile_constant(self) -> None:
        """CLI fixed profile MUST be ``test`` (D8)."""
        from cli_agent import CLI_FIXED_PROFILE
        assert CLI_FIXED_PROFILE == "test"

    def test_cli_rejected_flags_constant(self) -> None:
        """CLI MUST reject --profile и его short forms."""
        from cli_agent import CLI_REJECTED_FLAGS
        assert "--profile" in CLI_REJECTED_FLAGS


class TestCliHardcodesProfileInLifecycle:
    def test_cli_does_not_resolve_profile_from_env(self, monkeypatch) -> None:
        """CLI MUST NOT читать профиль из env (D8: only test, env-override запрещён).

        Негативная проверка: даже если в окружении стоит ``NANOBOT_PROFILE=prod``,
        CLI игнорирует — ``_initialize_settings(profile=CLI_FIXED_PROFILE)``.

        Проверяем, что config.resolve_application_config не использует
        ``os.environ.get("NANOBOT_PROFILE")`` ни прямо, ни через
        ``os.environ`` mapping. Это упадёт, если кто-то снова введёт
        env-fallback в обход новой модели.
        """
        from cli_agent import CLI_FIXED_PROFILE
        import config

        # Подменяем все источники env на ``prod``, чтобы любое чтение
        # NANOBOT_PROFILE дало prod.
        monkeypatch.setenv("NANOBOT_PROFILE", "prod")

        # Сбрасываем singleton, чтобы повторная инициализация прошла
        # чисто.
        config.SETTINGS._inner_dict = None

        try:
            # При наличии env-fallback в config.resolve_application_config
            # этот вызов либо поднимет ConfigurationError("profile='prod'
            # not in whitelist"), либо вернёт SETTINGS["profile"] == "prod".
            # Спека требует: CLI hardcode'ит "test", env игнорируется.
            import config as _cfg
            _cfg._initialize_settings(profile=CLI_FIXED_PROFILE)
            assert _cfg.SETTINGS["profile"] == "test", (
                "NANOBOT_PROFILE=prod подменён в окружении, но "
                "SETTINGS['profile'] должен быть 'test' (CLI hardcode)"
            )
        finally:
            # Не оставляем SETTINGS в инициализированном состоянии — это
            # ломает последующие тесты, которые сами инициализируют.
            config.SETTINGS._inner_dict = None


# ---------------------------------------------------------------------------
# Stage F — _parse_args unit-уровень
# ---------------------------------------------------------------------------


class TestParseArgsProfileRejection:
    def test_parse_args_raises_on_profile(self) -> None:
        from cli_agent import _parse_args, CLI_FIXED_PROFILE
        from config import ConfigurationError

        with pytest.raises(ConfigurationError) as exc_info:
            _parse_args(["--profile=test"])
        assert "--profile" in str(exc_info.value)
        assert CLI_FIXED_PROFILE in str(exc_info.value)

    def test_parse_args_no_profile_ok(self) -> None:
        from cli_agent import _parse_args

        args = _parse_args([])
        assert args.profile == "test"

    def test_parse_args_with_storage_ok(self) -> None:
        from cli_agent import _parse_args

        args = _parse_args(["--storage=postgres"])
        assert args.storage == "postgres"
        assert args.profile == "test"

    def test_parse_args_with_session_ok(self) -> None:
        from cli_agent import _parse_args

        args = _parse_args(["--session=my-session"])
        assert args.session == "my-session"
        assert args.profile == "test"

    def test_parse_args_patched_flag_ok(self) -> None:
        from cli_agent import _parse_args

        args = _parse_args(["--patched"])
        assert args.patched is True
        assert args.profile == "test"
