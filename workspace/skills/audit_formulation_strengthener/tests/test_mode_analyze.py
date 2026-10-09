"""Тесты режима ``analyze``."""

from __future__ import annotations

import pytest

from workspace.skills.audit_formulation_strengthener.scripts.modes import analyze
from workspace.skills.audit_formulation_strengthener.scripts.llm import JsonParseError


# -----------------------------------------------------------------------------
# estimate-only (без LLM)
# -----------------------------------------------------------------------------


def test_estimate_only_without_vnd() -> None:
    result, report = analyze.run(
        violation="Срок хранения ПДн установлен 1 год",
        vnd_paths=None,
        estimate_only=True,
    )
    assert result["status"] == "success"
    assert result["data"]["estimate"] is True
    assert result["data"]["vnd_count"] == 0
    assert result["data"]["llm_calls_planned"] == 1
    assert "violation_chars" in result["data"]
    assert report is None


def test_estimate_only_with_vnd_counts_paths() -> None:
    result, _ = analyze.run(
        violation="x",
        vnd_paths=["a.pdf", "b.pdf", "c.pdf"],
        estimate_only=True,
    )
    assert result["data"]["vnd_count"] == 3


def test_empty_violation_returns_empty_violation_error(mock_llm_all) -> None:
    result, _ = analyze.run(violation="   ", estimate_only=False)
    assert result["status"] == "error"
    assert result["data"]["error_type"] == "empty_violation"
    assert mock_llm_all.counter.chat_json_calls == 0


# -----------------------------------------------------------------------------
# LLM flow
# -----------------------------------------------------------------------------


def test_success_path(mock_llm_all, monkeypatch) -> None:
    mock_llm_all.set_response(
        "analyze",
        {
            "normalized": "Срок хранения ПДн — 1 год.",
            "key_concepts": ["пдн", "хранение"],
            "severity": "высокая",
            "suggested_vnd_sections": ["5. Сроки"],
        },
    )
    result, report = analyze.run(
        violation="Срок хранения ПДн установлен 1 год",
    )
    assert result["status"] == "success"
    assert result["data"]["normalized"] == "Срок хранения ПДн — 1 год."
    assert result["data"]["key_concepts"] == ["пдн", "хранение"]
    assert result["data"]["severity"] == "высокая"
    assert result["data"]["suggested_vnd_sections"] == ["5. Сроки"]
    assert report is None
    assert mock_llm_all.counter.chat_json_calls == 1


def test_invalid_severity_coerced_to_medium(mock_llm_all) -> None:
    mock_llm_all.set_response(
        "analyze",
        {
            "normalized": "X",
            "key_concepts": [],
            "severity": "катастрофическая",
            "suggested_vnd_sections": [],
        },
    )
    result, _ = analyze.run(violation="X")
    assert result["status"] == "success"
    assert result["data"]["severity"] == "средняя"


def test_key_concepts_filters_non_strings(mock_llm_all) -> None:
    mock_llm_all.set_response(
        "analyze",
        {
            "normalized": "X",
            "key_concepts": ["valid", 42, 3.14, None, "", "  ", "another"],
            "severity": "средняя",
            "suggested_vnd_sections": [],
        },
    )
    result, _ = analyze.run(violation="X")
    assert result["data"]["key_concepts"] == ["valid", "42", "3.14", "another"]


def test_schema_mismatch_when_normalized_missing(mock_llm_all) -> None:
    mock_llm_all.set_response(
        "analyze",
        {"key_concepts": [], "severity": "средняя"},
    )
    result, _ = analyze.run(violation="X")
    assert result["status"] == "error"
    assert result["data"]["error_type"] == "schema_mismatch"


def test_json_parse_failed_after_two_attempts(
    mock_llm_all, monkeypatch
) -> None:
    # Заставляем chat всегда возвращать мусор (не JSON).
    def bad_chat_json(**kwargs):
        raise JsonParseError("Test-induced", raw_text="not json", attempt=2)

    analyze_mod = __import__(
        "workspace.skills.audit_formulation_strengthener.scripts.modes.analyze",
        fromlist=["chat_json"],
    )
    monkeypatch.setattr(analyze_mod, "chat_json", bad_chat_json)

    result, _ = analyze.run(violation="X")
    assert result["status"] == "error"
    assert result["data"]["error_type"] == "json_parse_failed"


def test_llm_error_on_unexpected_exception(mock_llm_all, monkeypatch) -> None:
    def explode(**kwargs):
        raise RuntimeError("boom")

    analyze_mod = __import__(
        "workspace.skills.audit_formulation_strengthener.scripts.modes.analyze",
        fromlist=["chat_json"],
    )
    monkeypatch.setattr(analyze_mod, "chat_json", explode)

    result, _ = analyze.run(violation="X")
    assert result["status"] == "error"
    assert result["data"]["error_type"] == "llm_error"
