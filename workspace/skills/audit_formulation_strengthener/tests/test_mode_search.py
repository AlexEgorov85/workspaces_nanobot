"""Тесты режима ``search``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from workspace.skills.audit_formulation_strengthener.scripts.modes import search
from workspace.skills.audit_formulation_strengthener.scripts.vnd_io import (
    VndInputError,
)


# -----------------------------------------------------------------------------
# estimate-only
# -----------------------------------------------------------------------------


def test_estimate_only_basic(mock_llm_all, sample_vnd_files) -> None:
    result, report = search.run(
        violation="X",
        vnd_paths=[str(sample_vnd_files["file1"])],
        estimate_only=True,
    )
    assert result["status"] == "success"
    assert result["data"]["estimate"] is True
    assert result["data"]["llm_calls_planned"] >= 1
    assert result["data"]["size_estimate"]["files"] == 1
    assert report is None
    assert mock_llm_all.counter.chat_json_calls == 0


def test_empty_vnd_list_returns_no_vnd_error() -> None:
    result, _ = search.run(violation="X", vnd_paths=[])
    assert result["status"] == "error"
    assert result["data"]["error_type"] == "no_vnd"


def test_missing_file_returns_vnd_not_found(sample_vnd_files) -> None:
    result, _ = search.run(
        violation="X",
        vnd_paths=[str(sample_vnd_files["file1"]), "/nonexistent/file.pdf"],
    )
    assert result["status"] == "error"
    assert result["data"]["error_type"] == "vnd_not_found"


def test_empty_file_returns_vnd_empty_with_filename(mock_llm_all, sample_vnd_files) -> None:
    result, _ = search.run(
        violation="X",
        vnd_paths=[str(sample_vnd_files["empty"])],
        estimate_only=True,
    )
    assert result["status"] == "error"
    assert result["data"]["error_type"] == "vnd_empty"
    # D4 fix: имя файла в сообщении.
    assert "empty.txt" in result["data"]["message"]


# -----------------------------------------------------------------------------
# graceful degradation
# -----------------------------------------------------------------------------


def test_all_chunks_failed_returns_llm_error(
    mock_llm_all, sample_vnd_files, monkeypatch
) -> None:
    """Все чанки провалились на JSON-парсинге → llm_error (D7)."""
    from workspace.skills.audit_formulation_strengthener.scripts.llm import (
        JsonParseError,
    )

    def always_fail_json(**kwargs):
        raise JsonParseError("simulated", raw_text="bad", attempt=2)

    search_mod = __import__(
        "workspace.skills.audit_formulation_strengthener.scripts.modes.search",
        fromlist=["chat_json"],
    )
    monkeypatch.setattr(search_mod, "chat_json", always_fail_json)

    result, _ = search.run(
        violation="X",
        vnd_paths=[str(sample_vnd_files["file1"])],
    )
    assert result["status"] == "error"
    assert result["data"]["error_type"] == "llm_error"


def test_partial_failure_yields_success_with_chunks_failed(
    mock_llm_all, sample_vnd_files, monkeypatch
) -> None:
    """1 из 2 чанков упал → success, chunks_failed=1, остальные findings есть."""
    search_mod = __import__(
        "workspace.skills.audit_formulation_strengthener.scripts.modes.search",
        fromlist=["chat_json"],
    )

    counter = {"n": 0}

    def partial_chat_json(**kwargs):
        counter["n"] += 1
        if counter["n"] == 1:
            from workspace.skills.audit_formulation_strengthener.scripts.llm import (
                JsonParseError,
            )

            raise JsonParseError("simulated", raw_text="bad", attempt=2)
        return {
            "relation_type": "контекст",
            "relevance_score": 0.8,
            "why_matches": "ok",
        }

    monkeypatch.setattr(search_mod, "chat_json", partial_chat_json)

    # Берём 2 файла, чтобы было > 1 чанк (split_text разобьёт первый по абзацам).
    result, _ = search.run(
        violation="X",
        vnd_paths=[
            str(sample_vnd_files["file1"]),
            str(sample_vnd_files["file2"]),
        ],
    )
    assert result["status"] == "success"
    assert result["data"]["chunks_failed"] >= 1
    assert len(result["data"]["vnd_findings"]) >= 1


# -----------------------------------------------------------------------------
# фильтрация / top-K / evidence_id
# -----------------------------------------------------------------------------


def test_evidence_id_assigned_sequentially(mock_llm_all, sample_vnd_files) -> None:
    result, _ = search.run(
        violation="X",
        vnd_paths=[
            str(sample_vnd_files["file1"]),
            str(sample_vnd_files["file2"]),
        ],
    )
    assert result["status"] == "success"
    findings = result["data"]["vnd_findings"]
    eids = [f["evidence_id"] for f in findings]
    assert eids[0] == "F1"
    assert eids == [f"F{i+1}" for i in range(len(eids))]


def test_relation_type_outside_whitelist_coerced_to_context(
    mock_llm_all, sample_vnd_files
) -> None:
    mock_llm_all.set_response(
        "search_map",
        {
            "relation_type": "неизвестный_тип",  # не в whitelist
            "relevance_score": 0.7,
            "why_matches": "x",
        },
    )
    result, _ = search.run(
        violation="X",
        vnd_paths=[str(sample_vnd_files["file1"])],
    )
    for f in result["data"]["vnd_findings"]:
        assert f["relation_type"] == "контекст"


def test_score_below_threshold_excluded(mock_llm_all, sample_vnd_files) -> None:
    mock_llm_all.set_response(
        "search_map",
        {
            "relation_type": "контекст",
            "relevance_score": 0.1,  # < MIN_RELEVANCE_SCORE=0.3
            "why_matches": "x",
        },
    )
    result, _ = search.run(
        violation="X",
        vnd_paths=[str(sample_vnd_files["file1"])],
    )
    assert result["data"]["vnd_findings"] == []


# -----------------------------------------------------------------------------
# too_many_chunks
# -----------------------------------------------------------------------------


def test_too_many_chunks_returns_error(
    mock_llm_all, sample_vnd_files, monkeypatch
) -> None:
    # Подменяем get_tool_config чтобы вернуть max_chunks=0.
    from workspace.skills.audit_formulation_strengthener.scripts import vnd_io

    def fake_get_tool_config(skill_name):
        return {"execution": {"max_chunks_for_execution": 0}}

    monkeypatch.setattr(vnd_io, "get_tool_config", fake_get_tool_config)

    result, _ = search.run(
        violation="X",
        vnd_paths=[str(sample_vnd_files["file1"])],
        estimate_only=True,
    )
    assert result["status"] == "error"
    assert result["data"]["error_type"] == "too_many_chunks"


# -----------------------------------------------------------------------------
# analyze_result_unreadable
# -----------------------------------------------------------------------------


def test_analyze_result_path_unreadable(sample_vnd_files) -> None:
    result, _ = search.run(
        violation="X",
        vnd_paths=[str(sample_vnd_files["file1"])],
        analyze_result_path="/nonexistent/analyze.json",
    )
    assert result["status"] == "error"
    assert result["data"]["error_type"] == "analyze_result_unreadable"


# -----------------------------------------------------------------------------
# Фолбэк на сырой violation
# -----------------------------------------------------------------------------


def test_no_analyze_result_falls_back_to_raw_violation(
    mock_llm_all, sample_vnd_files
) -> None:
    result, _ = search.run(
        violation="Срок хранения ПДн 1 год",
        vnd_paths=[str(sample_vnd_files["file1"])],
    )
    assert result["status"] == "success"
    assert result["data"]["normalized_violation_used"] == "Срок хранения ПДн 1 год"
