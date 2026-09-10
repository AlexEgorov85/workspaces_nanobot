"""Тесты CLI навыка ``audit_analyzer`` (scripts/cli.py).

CLI — целевая точка вызова навыка из shell/runtime. Покрывает:

* argparse + dispatch по 3 режимам;
* вывод плоского JSON через ``output.prepare_output``;
* маршрутизацию в правильный режим;
* обработку ошибок (HTTPError, FileNotFoundError, неизвестный режим);
* контракт (для runtime — JSON в stdout, exit code).
"""

from __future__ import annotations

import argparse
import inspect
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
    "scripts/output.py",
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

        # Structural checks: each entry point is callable AND has a sensible signature,
        # so a future rename or stub-replace will be caught.
        for entry in (_build_parser, _parse_params, main, prepare_output):
            assert callable(entry), f"{entry!r} must be callable"
            sig = inspect.signature(entry)
            assert sig.parameters or sig.return_annotation is not inspect.Signature.empty, (
                f"{entry!r} must be a real function, not a stub"
            )
        # _build_parser() must actually return an argparse.ArgumentParser.
        parser = _build_parser()
        assert isinstance(parser, argparse.ArgumentParser), (
            "_build_parser() must return argparse.ArgumentParser"
        )


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
        """``--mode generated_sql`` — LLM-генерация SQL по NL-запросу."""
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


class TestResolveKnownIndex:
    """Публичный registry lookup для ``_run_vector``.

    Skill валидирует ``index_name`` ДО вызова ``CacheProvider.search_vector``,
    чтобы избежать silent fail (раньше неизвестный индекс → ``[]`` →
    «Документы не найдены» как success). Тесты мокают публичный
    ``lib.services.cache_provider_impl.read_vector_index_config``.
    """

    def test_known_index(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from workspace.skills.audit_analyzer.scripts import cli

        monkeypatch.setattr(
            "lib.services.cache_provider_impl.read_vector_index_config",
            lambda cfg: {"audits_index": object(), "violations_index": object()},
        )
        known, msg = cli._resolve_known_index("audits_index")
        assert known is True
        assert msg == ""

    def test_unknown_index(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from workspace.skills.audit_analyzer.scripts import cli

        monkeypatch.setattr(
            "lib.services.cache_provider_impl.read_vector_index_config",
            lambda cfg: {"audits_index": object(), "violations_index": object()},
        )
        known, msg = cli._resolve_known_index("bogus_index")
        assert known is False
        assert "bogus_index" in msg
        # Сообщение содержит список доступных имён.
        assert "audits_index" in msg
        assert "violations_index" in msg

    def test_empty_registry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from workspace.skills.audit_analyzer.scripts import cli

        monkeypatch.setattr(
            "lib.services.cache_provider_impl.read_vector_index_config",
            lambda cfg: {},
        )
        known, msg = cli._resolve_known_index("anything")
        assert known is False
        assert "пуст" in msg or "available" in msg.lower()

    def test_registry_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """PG offline → registry lookup падает → (None, error_msg).

        CLI в этом случае **отдаёт error**, не пропускает дальше
        (иначе снова рискуем silent fail).
        """
        from workspace.skills.audit_analyzer.scripts import cli

        def _explode(cfg):
            raise RuntimeError("PG unavailable")

        monkeypatch.setattr(
            "lib.services.cache_provider_impl.read_vector_index_config",
            _explode,
        )
        known, msg = cli._resolve_known_index("audits_index")
        assert known is None
        assert "PG unavailable" in msg
        assert "gateway" in msg.lower()


class TestRunVectorValidation:
    """``_run_vector`` отвергает неизвестный индекс БЕЗ обращения к provider'у."""

    def _stub_db(self) -> object:
        class _Stub:
            def __getattr__(self, name):
                raise AssertionError(
                    f"db.{name} не должен вызываться при unknown_index"
                )

        return _Stub()

    def test_unknown_index_returns_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from workspace.skills.audit_analyzer.scripts import cli

        monkeypatch.setattr(
            "lib.services.cache_provider_impl.read_vector_index_config",
            lambda cfg: {"audits_index": object()},
        )
        result = cli._run_vector(
            query="пожарная безопасность",
            db=self._stub_db(),
            index_name="bogus_index",
            top_k=3,
            threshold=None,
        )
        assert result["status"] == "error"
        assert result["data"]["error_type"] == "unknown_index"
        assert "bogus_index" in result["data"]["message"]

    def test_registry_unavailable_returns_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from workspace.skills.audit_analyzer.scripts import cli

        def _explode(cfg):
            raise RuntimeError("PG offline")

        monkeypatch.setattr(
            "lib.services.cache_provider_impl.read_vector_index_config",
            _explode,
        )
        result = cli._run_vector(
            query="q",
            db=self._stub_db(),
            index_name="audits_index",
            top_k=5,
            threshold=None,
        )
        assert result["status"] == "error"
        assert result["data"]["error_type"] == "registry_unavailable"

    def test_known_index_proceeds_to_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Happy path: индекс известен → db.search_vector вызывается."""
        from dataclasses import dataclass

        from workspace.skills.audit_analyzer.scripts import cli

        monkeypatch.setattr(
            "lib.services.cache_provider_impl.read_vector_index_config",
            lambda cfg: {"audits_index": object()},
        )

        @dataclass
        class _FakeResult:
            content: str = "doc"
            score: float = 0.9
            source: str = "oarb.audits"
            table: str = "audits"
            pk_value: int = 1
            chunk: str = ""
            matched_chunks: int = 1
            row: dict = None  # type: ignore[assignment]
            signature_status: str = "CURRENT"
            signature_reason: str = ""

            def __post_init__(self):
                if self.row is None:
                    self.row = {}

        class _FakeDB:
            def search_vector(self, query, index_name, top_k, threshold):
                assert index_name == "audits_index"
                return [_FakeResult()]

        out = cli._run_vector(
            query="q",
            db=_FakeDB(),
            index_name="audits_index",
            top_k=5,
            threshold=None,
        )
        assert out["status"] == "success"
        assert out["data"]["count"] == 1


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
