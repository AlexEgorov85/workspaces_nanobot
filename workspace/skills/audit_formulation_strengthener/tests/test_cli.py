"""Тесты CLI-обёртки ``scripts/cli.py``.

Тестируем через прямой вызов ``main(argv)`` — это позволяет
использовать моки для LLM и не дёргать subprocess.
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import pytest


# _SKILL_SCRIPTS вычисляется в _run_cli, чтобы conftest.py успел
# настроить sys.path.
def _get_skill_scripts() -> Path:
    return Path(__file__).resolve().parents[1] / "scripts"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_cli(
    *argv: str,
    monkeypatch: pytest.MonkeyPatch,
    llm_mock: Any,
    prepare_vnd_mock: Any | None = None,
) -> tuple[int, str]:
    """Запустить CLI с моками, вернуть (returncode, stdout)."""
    import importlib.util

    _SKILL_SCRIPTS = _get_skill_scripts()
    spec = importlib.util.spec_from_file_location(
        "afs_cli", _SKILL_SCRIPTS / "cli.py"
    )
    cli_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli_mod)  # type: ignore[union-attr]

    # Принудительно импортируем modes, чтобы они появились в sys.modules.
    # Иначе патч не сработает, т.к. modes импортируется через
    # 'from modes import analyze' внутри функций cli.py.
    for mode_name in ("analyze", "search", "synthesize"):
        mod_key = f"modes.{mode_name}"
        if mod_key not in sys.modules:
            import importlib
            importlib.import_module(mod_key)

    # Подменяем LLM через sys.modules.
    for mode_name in ("analyze", "search", "synthesize"):
        mod_key = f"modes.{mode_name}"
        if mod_key in sys.modules:
            monkeypatch.setattr(sys.modules[mod_key], "call_llm_json", llm_mock)

    # Подменяем prepare_vnd в vnd_io.
    if prepare_vnd_mock is not None:
        if "vnd_io" not in sys.modules:
            import importlib
            importlib.import_module("vnd_io")
        monkeypatch.setattr(sys.modules["vnd_io"], "prepare_vnd", prepare_vnd_mock)

    # Перехватываем stdout.
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            rc = cli_mod.main(list(argv))
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 1
    return rc, buf.getvalue()


def _all_mock(system, user, operation):
    """Единый мок для всех трёх операций."""
    if operation == "analyze":
        return {
            "normalized": "В организации установлен срок хранения ПДн — 1 год.",
            "key_concepts": ["срок хранения", "ПДн"],
            "severity": "высокая",
            "suggested_vnd_sections": ["Сроки хранения ПДн"],
        }
    if operation == "search_map":
        return {
            "relation_type": "прямое_противоречие",
            "relevance_score": 0.9,
            "why_matches": "Прямое противоречие.",
        }
    if operation == "synthesize":
        return {
            "title": "Анализ отклонения",
            "violation_summary": ["Нормализованный текст."],
            "established_facts": ["Установлено."],
            "deviation_analysis": ["Анализ."],
            "vnd_citations": [],
            "verdict": {"category": "высокая", "verdict_text": ["Итог."]},
            "recommended_formulation": ["Рекомендация."],
        }
    raise RuntimeError(f"unexpected op: {operation}")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_cli_analyze_mode(
    monkeypatch: pytest.MonkeyPatch, mock_prepare_vnd: Any
) -> None:
    """``--mode analyze`` → JSON-вывод с data.normalized."""
    # Подготовить моки: search_map НЕ должен вызываться.
    called = {"analyze": 0, "search": 0, "synth": 0}

    def counting(system, user, operation):
        if operation == "analyze":
            called["analyze"] += 1
        elif operation == "search_map":
            called["search"] += 1
        elif operation == "synthesize":
            called["synth"] += 1
        return _all_mock(system, user, operation)

    rc, out = _run_cli(
        "--mode", "analyze",
        "--violation", "Срок хранения ПДн — 1 год",
        "--vnd", "vnd1.txt",
        monkeypatch=monkeypatch,
        llm_mock=counting,
        prepare_vnd_mock=mock_prepare_vnd,
    )

    assert rc == 0
    parsed = json.loads(out)
    assert parsed["status"] == "success"
    assert parsed["data"]["normalized"].startswith("В организации")
    assert called["analyze"] == 1
    assert called["search"] == 0
    assert called["synth"] == 0


def test_cli_estimate_only(
    monkeypatch: pytest.MonkeyPatch, mock_prepare_vnd: Any
) -> None:
    """``--estimate-only`` → без LLM-вызовов."""
    called = {"any": 0}

    def counting(system, user, operation):
        called["any"] += 1
        return _all_mock(system, user, operation)

    rc, out = _run_cli(
        "--mode", "all",
        "--violation", "...",
        "--vnd", "vnd1.txt",
        "--estimate-only",
        monkeypatch=monkeypatch,
        llm_mock=counting,
        prepare_vnd_mock=mock_prepare_vnd,
    )

    assert rc == 0
    assert called["any"] == 0  # нет LLM-вызовов при --estimate-only
    parsed = json.loads(out)
    assert parsed["status"] == "success"
    assert "llm_calls_planned" in parsed["data"] or "vnd_chunks_total" in parsed["data"]


def test_cli_no_vnd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Без ``--vnd`` → ошибка exit-code 2."""
    called: list[str] = []

    def counting(system, user, operation):
        called.append(operation)
        return _all_mock(system, user, operation)

    rc, out = _run_cli(
        "--mode", "analyze",
        "--violation", "что-то",
        monkeypatch=monkeypatch,
        llm_mock=counting,
    )

    assert rc == 2
    parsed = json.loads(out)
    assert parsed["status"] == "error"
    assert called == []  # LLM не вызывался


def test_cli_empty_violation(
    monkeypatch: pytest.MonkeyPatch, mock_prepare_vnd: Any
) -> None:
    """Пустой ``--violation`` → ошибка ``empty_violation``."""
    called: list[str] = []

    def counting(system, user, operation):
        called.append(operation)
        return _all_mock(system, user, operation)

    rc, out = _run_cli(
        "--mode", "analyze",
        "--violation", "",
        "--vnd", "vnd1.txt",
        monkeypatch=monkeypatch,
        llm_mock=counting,
        prepare_vnd_mock=mock_prepare_vnd,
    )

    assert rc == 2
    parsed = json.loads(out)
    assert parsed["data"]["error_type"] == "empty_violation"
    assert called == []


def test_cli_all_mode_saves_report(
    monkeypatch: pytest.MonkeyPatch,
    mock_prepare_vnd: Any,
    tmp_path: Path,
) -> None:
    """``--mode all --output <path>`` → сохраняет файл."""
    output_file = tmp_path / "report.md"

    rc, out = _run_cli(
        "--mode", "all",
        "--violation", "что-то",
        "--vnd", "vnd1.txt",
        "--output", str(output_file),
        "--output-format", "md",
        monkeypatch=monkeypatch,
        llm_mock=_all_mock,
        prepare_vnd_mock=mock_prepare_vnd,
    )

    assert rc == 0
    parsed = json.loads(out)
    assert parsed["status"] == "success"
    assert parsed.get("saved_to") == str(output_file)
    assert output_file.exists()
    assert "Анализ отклонения" in output_file.read_text(encoding="utf-8")


def test_cli_default_mode_is_all(
    monkeypatch: pytest.MonkeyPatch, mock_prepare_vnd: Any
) -> None:
    """Без ``--mode`` → ``all`` (по умолчанию)."""
    called = {"search_map": 0}

    def counting(system, user, operation):
        if operation == "search_map":
            called["search_map"] += 1
        return _all_mock(system, user, operation)

    rc, out = _run_cli(
        "--violation", "текст",
        "--vnd", "vnd1.txt",
        "--output-format", "md",
        monkeypatch=monkeypatch,
        llm_mock=counting,
        prepare_vnd_mock=mock_prepare_vnd,
    )

    assert rc == 0
    assert called["search_map"] >= 1  # search был запущен
