"""Тесты CLI: end-to-end через ``main(argv)``."""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout

import pytest

from workspace.skills.audit_formulation_strengthener.scripts import cli
from workspace.skills.audit_formulation_strengthener.scripts.report import markdown


def _run(argv: list[str]) -> tuple[int, str, str]:
    """Запустить ``cli.main(argv)`` с перехватом stdout/stderr.

    Returns:
        ``(exit_code, stdout, stderr)``.
    """
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with redirect_stdout(out_buf):
        old_stderr = sys.stderr
        sys.stderr = err_buf
        try:
            exit_code = cli.main(argv)
        finally:
            sys.stderr = old_stderr
    return exit_code, out_buf.getvalue(), err_buf.getvalue()


# -----------------------------------------------------------------------------
# estimate-only
# -----------------------------------------------------------------------------


def test_cli_all_estimate_only_zero_calls(
    mock_llm_all, sample_vnd_files
) -> None:
    exit_code, stdout, _ = _run(
        [
            "--mode", "all",
            "--estimate-only",
            "--violation", "Срок хранения ПДн 1 год",
            "--vnd", str(sample_vnd_files["file1"]),
        ]
    )
    assert exit_code == 0
    payload = json.loads(stdout)
    assert payload["status"] == "success"
    assert payload["data"]["estimate"] is True
    assert payload["data"]["llm_calls_planned"] >= 3
    assert mock_llm_all.counter.chat_json_calls == 0


def test_cli_analyze_estimate_only_without_vnd(mock_llm_all) -> None:
    exit_code, stdout, _ = _run(
        [
            "--mode", "analyze",
            "--estimate-only",
            "--violation", "Test",
        ]
    )
    assert exit_code == 0
    payload = json.loads(stdout)
    assert payload["data"]["estimate"] is True
    assert payload["data"]["vnd_count"] == 0
    assert payload["data"]["llm_calls_planned"] == 1


def test_cli_search_estimate_only(mock_llm_all, sample_vnd_files) -> None:
    exit_code, stdout, _ = _run(
        [
            "--mode", "search",
            "--estimate-only",
            "--violation", "x",
            "--vnd", str(sample_vnd_files["file1"]),
        ]
    )
    assert exit_code == 0
    payload = json.loads(stdout)
    assert payload["data"]["llm_calls_planned"] >= 1


# -----------------------------------------------------------------------------
# Exit-коды
# -----------------------------------------------------------------------------


def test_cli_no_vnd_exits_2_with_no_vnd_json(mock_llm_all) -> None:
    exit_code, stdout, _ = _run(
        ["--mode", "all", "--violation", "x"]
    )
    assert exit_code == 2
    payload = json.loads(stdout)
    assert payload["data"]["error_type"] == "no_vnd"


def test_cli_empty_violation_exits_2(mock_llm_all, sample_vnd_files) -> None:
    exit_code, stdout, _ = _run(
        [
            "--mode", "analyze",
            "--violation", "   ",
        ]
    )
    assert exit_code == 2
    payload = json.loads(stdout)
    assert payload["data"]["error_type"] == "empty_violation"


def test_cli_missing_file_exits_2(mock_llm_all) -> None:
    exit_code, stdout, _ = _run(
        [
            "--mode", "search",
            "--violation", "x",
            "--vnd", "/nonexistent/file.pdf",
        ]
    )
    assert exit_code == 2
    payload = json.loads(stdout)
    assert payload["data"]["error_type"] == "vnd_not_found"


def test_cli_missing_file_in_all_mode_fails_before_llm(mock_llm_all) -> None:
    """Регрессия: в --mode all несуществующий ВНД ловится ДО LLM-вызова analyze.

    Раньше валидация происходила только внутри search/prepare_vnd — после
    analyze, из-за чего тратился LLM-вызов, а при недоступном LLM ошибка
    маскировалась под llm_error (exit 1) вместо vnd_not_found (exit 2).
    """
    exit_code, stdout, _ = _run(
        [
            "--mode", "all",
            "--violation", "x",
            "--vnd", "/nonexistent/file.pdf",
        ]
    )
    assert exit_code == 2
    payload = json.loads(stdout)
    assert payload["data"]["error_type"] == "vnd_not_found"
    assert mock_llm_all.counter.chat_json_calls == 0



# -----------------------------------------------------------------------------
# estimate-only + --output → --output игнорируется (П6)
# -----------------------------------------------------------------------------


def test_cli_estimate_only_ignores_output(
    mock_llm_all, sample_vnd_files, tmp_path
) -> None:
    target = tmp_path / "should_not_exist.json"
    exit_code, stdout, _ = _run(
        [
            "--mode", "all",
            "--estimate-only",
            "--violation", "x",
            "--vnd", str(sample_vnd_files["file1"]),
            "--output", str(target),
        ]
    )
    assert exit_code == 0
    # Файл НЕ должен быть создан.
    assert not target.exists()
    # JSON ушёл в stdout.
    payload = json.loads(stdout)
    assert payload["data"]["estimate"] is True


# -----------------------------------------------------------------------------
# --output → файл + brief JSON в stdout (D10)
# ---------------------------------------------------------------------`--------


def test_cli_synthesize_with_output_creates_file_and_brief(
    mock_llm_all, sample_vnd_files, tmp_path
) -> None:
    target = tmp_path / "report.md"
    mock_llm_all.set_response(
        "synthesize",
        {
            "title": "Тестовый отчёт",
            "violation_summary": ["s"],
            "established_facts": ["f"],
            "deviation_analysis": ["a"],
            "vnd_citations": [],
            "verdict": {"category": "средняя", "verdict_text": ["v"]},
            "recommended_formulation": ["r"],
        },
    )

    exit_code, stdout, _ = _run(
        [
            "--mode", "synthesize",
            "--violation", "x",
            "--vnd", str(sample_vnd_files["file1"]),
            "--output", str(target),
        ]
    )
    assert exit_code == 0
    assert target.exists()
    content = target.read_text(encoding="utf-8")
    assert "Тестовый отчёт" in content
    assert "## 1. Краткое изложение отклонения" in content
    # В stdout — краткий JSON (D10 fix).
    brief = json.loads(stdout.strip())
    assert brief["mode"] == "synthesize"
    assert brief["status"] == "success"
    assert brief["saved_to"].endswith("report.md")


# -----------------------------------------------------------------------------
# --internal-format json
# -----------------------------------------------------------------------------


def test_cli_synthesize_internal_format_json(
    mock_llm_all, sample_vnd_files
) -> None:
    mock_llm_all.set_response(
        "synthesize",
        {
            "title": "Test",
            "violation_summary": ["s"],
            "established_facts": ["f"],
            "deviation_analysis": ["a"],
            "vnd_citations": [],
            "verdict": {"category": "средняя", "verdict_text": ["v"]},
            "recommended_formulation": ["r"],
        },
    )
    exit_code, stdout, _ = _run(
        [
            "--mode", "synthesize",
            "--internal-format", "json",
            "--violation", "x",
            "--vnd", str(sample_vnd_files["file1"]),
        ]
    )
    assert exit_code == 0
    payload = json.loads(stdout)
    assert payload["status"] == "success"
    assert payload["data"]["title"] == "Test"


# -----------------------------------------------------------------------------
# Parametrize по всем error_type → exit code
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error_type,expected_exit",
    [
        ("no_vnd", 2),
        ("vnd_not_found", 2),
        ("vnd_unreadable", 2),
        ("vnd_empty", 2),
        ("too_many_chunks", 2),
        ("empty_violation", 2),
        ("analyze_result_unreadable", 2),
        ("search_result_unreadable", 2),
        ("unknown_mode", 2),
        ("prompt_missing", 1),
        ("prompt_unresolved_var", 1),
        ("json_parse_failed", 1),
        ("schema_mismatch", 1),
        ("llm_error", 1),
        ("io_error", 1),
        ("internal_error", 1),
    ],
)
def test_cli_exit_code_mapping(error_type: str, expected_exit: int) -> None:
    """Все error_type → правильный exit code."""
    assert cli._exit_code_for(
        {"status": "error", "data": {"error_type": error_type}}
    ) == expected_exit


def test_cli_exit_code_zero_for_success() -> None:
    assert cli._exit_code_for({"status": "success", "data": {}}) == 0
