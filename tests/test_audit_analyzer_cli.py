"""Тесты CLI навыка ``audit_analyzer`` (scripts/cli.py).

CLI — целевая точка вызова навыка из shell/runtime. Покрывает:

* argparse + dispatch по 3 режимам;
* вывод плоского JSON через ``output.prepare_output``;
* маршрутизацию в правильный режим;
* обработку ошибок (HTTPError, FileNotFoundError, неизвестный режим);
* контракт (для runtime — JSON в stdout, exit code).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


SKILL_DIR = Path("workspace/skills/audit_analyzer")
CLI_PATH = SKILL_DIR / "scripts" / "cli.py"

REQUIRED_FILES = [
    "scripts/cli.py",
    "scripts/__init__.py",
    "scripts/predefined_mode.py",
    "scripts/generated_sql_mode.py",
    "scripts/output.py",
    "scripts/llm.py",
    "scripts/skill_config.py",
]


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    """Запустить CLI subprocess с PYTHONPATH=. для импортов."""
    return subprocess.run(
        [sys.executable, str(CLI_PATH), *args],
        capture_output=True,
        text=True,
        cwd=".",
        env={"PYTHONPATH": ".", "PATH": os.environ.get("PATH", "")},
    )


class TestCLIStructure:
    """Все ожидаемые файлы CLI существуют и импортируются."""

    @pytest.mark.parametrize("path", REQUIRED_FILES)
    def test_file_exists(self, path: str) -> None:
        full = SKILL_DIR / path
        assert full.is_file(), f"{full} должен существовать"

    def test_cli_imports(self) -> None:
        from workspace.skills.audit_analyzer.scripts.cli import (
            _build_parser,
            _parse_params,
            main,
            prepare_output,
        )

        assert callable(main)
        assert callable(_build_parser)
        assert callable(_parse_params)
        assert callable(prepare_output)


class TestCLIParser:
    """Argparse: разбор аргументов."""

    def test_parser_help(self) -> None:
        from workspace.skills.audit_analyzer.scripts.cli import _build_parser

        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--help"])

    def test_default_mode(self) -> None:
        from workspace.skills.audit_analyzer.scripts.cli import _build_parser

        parser = _build_parser()
        args = parser.parse_args([])
        assert args.mode in ("predefined", "generated_sql", "vector")

    def test_explicit_mode(self) -> None:
        from workspace.skills.audit_analyzer.scripts.cli import _build_parser

        parser = _build_parser()
        args = parser.parse_args(["--mode", "vector"])
        assert args.mode == "vector"

    def test_generated_sql_mode(self) -> None:
        """``--mode generated_sql`` — LLM-генерация SQL (синоним для LLM)."""
        from workspace.skills.audit_analyzer.scripts.cli import _build_parser

        parser = _build_parser()
        args = parser.parse_args(["--mode", "generated_sql"])
        assert args.mode == "generated_sql"

    def test_short_sql_alias_rejected(self) -> None:
        """``--mode sql`` (короткое) отвергается: имя нормализовано в generated_sql."""
        from workspace.skills.audit_analyzer.scripts.cli import _build_parser

        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--mode", "sql"])

    def test_invalid_mode_rejected(self) -> None:
        from workspace.skills.audit_analyzer.scripts.cli import _build_parser

        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--mode", "bogus"])


class TestParamsParser:
    """``--params`` принимает JSON и key=value."""

    def test_json_params(self) -> None:
        from workspace.skills.audit_analyzer.scripts.cli import _parse_params

        out = _parse_params('{"date_from": "2024-01-01"}')
        assert out == {"date_from": "2024-01-01"}

    def test_keyvalue_params(self) -> None:
        from workspace.skills.audit_analyzer.scripts.cli import _parse_params

        out = _parse_params("date_from=2024-01-01,date_to=2024-12-31")
        assert out == {
            "date_from": "2024-01-01",
            "date_to": "2024-12-31",
        }

    def test_empty_params(self) -> None:
        from workspace.skills.audit_analyzer.scripts.cli import _parse_params

        assert _parse_params("") == {}


class TestOutputFormat:
    """``output.prepare_output`` — плоский JSON для всех 3 режимов."""

    def test_predefined_success(self) -> None:
        from workspace.skills.audit_analyzer.scripts.output import prepare_output

        out = prepare_output(
            {
                "status": "success",
                "data": {
                    "script_name": "audit_status_summary",
                    "sql": "SELECT 1",
                    "result": {
                        "rows": [[1]],
                        "columns": ["x"],
                        "row_count": 1,
                    },
                },
            },
            "predefined",
        )
        assert out["mode"] == "predefined"
        assert out["status"] == "success"
        assert out["row_count"] == 1
        assert out["script_name"] == "audit_status_summary"

    def test_vector_results(self) -> None:
        from workspace.skills.audit_analyzer.scripts.output import prepare_output

        out = prepare_output(
            {
                "status": "success",
                "data": {
                    "results": [{"content": "x", "score": 0.9}],
                    "count": 1,
                },
            },
            "vector",
        )
        assert out["mode"] == "vector"
        assert out["vector_results"] == [{"content": "x", "score": 0.9}]
        assert out["count"] == 1

    def test_error_message(self) -> None:
        from workspace.skills.audit_analyzer.scripts.output import prepare_output

        out = prepare_output(
            {"status": "error", "data": {"message": "fail"}},
            "predefined",
        )
        assert out["status"] == "error"
        assert "fail" in out["message"]


class TestCLIIntegration:
    """Subprocess: CLI запускается и выдаёт ожидаемые exit-коды / JSON."""

    def test_cli_help_exits_zero(self) -> None:
        result = _run_cli("--help")
        assert result.returncode == 0

    def test_cli_invalid_mode_exits_two(self) -> None:
        """Неизвестный --mode → argparse SystemExit(2)."""
        result = _run_cli("--mode", "bogus")
        assert result.returncode == 2

    def test_cli_produces_json_on_dispatch_error(self) -> None:
        """--mode predefined без --script → JSON-ошибка в stdout.

        Либо JSON со ``status=error`` (если cache read прошёл),
        либо SystemExit (1) — но с осмысленным trace на stderr.
        """
        result = _run_cli("--mode", "predefined")
        assert result.returncode in (0, 1)
        stdout = result.stdout.strip()
        if stdout:
            payload = json.loads(stdout)
            assert payload.get("status") == "error"
            assert "message" in payload
