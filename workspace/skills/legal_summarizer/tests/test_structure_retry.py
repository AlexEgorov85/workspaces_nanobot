"""Тесты для smart retry (Этап 30 из PLAN.md)."""

from __future__ import annotations

import pytest

from workspace.skills.legal_summarizer.scripts.structure.retry import (
    ChunkResultParseError,
    build_repair_prompt,
    parse_batch_response_local,
)


def test_parse_valid_json():
    response = '{"summaries": {"c1": "summary 1", "c2": "summary 2"}}'
    result = parse_batch_response_local(response, ("c1", "c2"))
    assert result.summaries == {"c1": "summary 1", "c2": "summary 2"}
    assert result.failed_chunk_ids == ()


def test_parse_code_fenced_json():
    response = '```json\n{"summaries": {"c1": "s"}}\n```'
    result = parse_batch_response_local(response, ("c1",))
    assert result.summaries == {"c1": "s"}


def test_parse_missing_chunks_marks_failed():
    response = '{"summaries": {"c1": "s"}}'
    result = parse_batch_response_local(response, ("c1", "c2"))
    assert result.summaries == {"c1": "s"}
    assert result.failed_chunk_ids == ("c2",)


def test_parse_invalid_json_all_failed():
    response = "not json at all"
    result = parse_batch_response_local(response, ("c1", "c2"))
    assert result.summaries == {}
    assert result.failed_chunk_ids == ("c1", "c2")


def test_parse_summaries_as_dict():
    response = '{"summaries": {"c1": {"summary": "long"}}}'
    result = parse_batch_response_local(response, ("c1",))
    assert result.summaries == {"c1": "long"}


def test_parse_empty_value_failed():
    response = '{"summaries": {"c1": ""}}'
    result = parse_batch_response_local(response, ("c1",))
    assert result.summaries == {}
    assert result.failed_chunk_ids == ("c1",)


def test_repair_prompt_includes_only_failed_ids():
    prompt = build_repair_prompt("bad response", ("c3", "c5"))
    assert "'c3'" in prompt
    assert "'c5'" in prompt
    assert "c1" not in prompt
    assert "c2" not in prompt


def test_chunk_result_parse_error_attributes():
    err = ChunkResultParseError(
        "bad", chunk_ids=("c1",), raw_response="raw",
    )
    assert err.chunk_ids == ("c1",)
    assert err.raw_response == "raw"


def test_parse_with_extra_text_around_json():
    response = 'Some text before {"summaries": {"c1": "ok"}} and after'
    result = parse_batch_response_local(response, ("c1",))
    assert result.summaries == {"c1": "ok"}