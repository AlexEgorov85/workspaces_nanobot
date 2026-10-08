"""Тесты helpers: prompts, output, llm._parse_json."""

from __future__ import annotations

import json

import pytest

from workspace.skills.audit_formulation_strengthener.scripts.llm import _parse_json
from workspace.skills.audit_formulation_strengthener.scripts.output import (
    make_error,
    prepare_output,
    sanitize_output,
)
from workspace.skills.audit_formulation_strengthener.scripts.prompts import (
    PromptUnresolvedVarError,
    load_prompt,
    render_prompt,
)


# -----------------------------------------------------------------------------
# load_prompt
# -----------------------------------------------------------------------------


def test_load_prompt_existing() -> None:
    text = load_prompt("analyze_system")
    assert "VIOLATION_TEXT" in text


def test_load_prompt_missing_raises() -> None:
    with pytest.raises(FileNotFoundError):
        load_prompt("nonexistent_prompt_xyz")


# -----------------------------------------------------------------------------
# render_prompt
# -----------------------------------------------------------------------------


def test_render_prompt_substitutes_all() -> None:
    out = render_prompt(
        "Hello {{NAME}}, age {{AGE}}",
        NAME="World",
        AGE=42,
    )
    assert out == "Hello World, age 42"


def test_render_prompt_none_value_becomes_empty() -> None:
    out = render_prompt("[{{X}}]", X=None)
    assert out == "[]"


def test_render_prompt_unresolved_var_raises_with_name() -> None:
    with pytest.raises(PromptUnresolvedVarError) as excinfo:
        render_prompt("Hello {{NAME}} {{MISSING}}", NAME="World")
    assert "MISSING" in excinfo.value.unresolved
    assert "NAME" not in excinfo.value.unresolved


def test_render_prompt_no_unresolved() -> None:
    # Без {{...}} — должно вернуться как есть.
    out = render_prompt("static text", ANY="value")
    assert out == "static text"


# -----------------------------------------------------------------------------
# make_error
# -----------------------------------------------------------------------------


def test_make_error_with_error_type() -> None:
    err = make_error("файл не найден", error_type="vnd_not_found")
    assert err == {
        "status": "error",
        "data": {"message": "файл не найден", "error_type": "vnd_not_found"},
    }


def test_make_error_without_error_type() -> None:
    err = make_error("internal")
    assert err == {"status": "error", "data": {"message": "internal"}}
    assert "error_type" not in err["data"]


# -----------------------------------------------------------------------------
# prepare_output
# -----------------------------------------------------------------------------


def test_prepare_output_success_envelope() -> None:
    result = {"status": "success", "data": {"k": "v"}}
    out = prepare_output(result, mode="analyze")
    assert out["mode"] == "analyze"
    assert out["status"] == "success"
    assert out["data"] == {"k": "v"}


def test_prepare_output_empty_result_returns_error_envelope() -> None:
    out = prepare_output({}, mode="search")
    assert out["status"] == "error"
    assert "message" in out["data"]


def test_prepare_output_uses_sanitize_for_special_types() -> None:
    from datetime import datetime, timezone

    result = {
        "status": "success",
        "data": {"now": datetime(2026, 9, 18, tzinfo=timezone.utc)},
    }
    out = prepare_output(result, mode="analyze")
    assert isinstance(out["data"]["now"], str)  # isoformat


def test_sanitize_output_passes_through() -> None:
    out = sanitize_output({"x": 1})
    assert out == {"x": 1}


# -----------------------------------------------------------------------------
# llm._parse_json (К4: корректный парсинг JSON + хвостовая prose с })
# -----------------------------------------------------------------------------


def test_llm_parse_json_handles_trailing_prose_with_brace() -> None:
    """К4 фикс-тест: ``raw_decode`` парсит JSON до баланса скобок.

    Жадный regex (r'{.*}', re.DOTALL) захватывал бы до последней }
    в тексте — для ответа LLM вида ``{"k":1} и потом prose с } здесь``
    это давало бы невалидный JSON.

    ``raw_decode`` корректно учитывает баланс скобок и возвращает
    первый валидный JSON-объект.
    """
    # JSON-объект + хвостовая prose с лишней }.
    assert _parse_json('{"key": "value"} and then some text with } in it.') == {
        "key": "value"
    }

    # JSON с вложенным объектом + prose после.
    text2 = '{"outer": {"inner": 1}, "ok": true} trailing text with } brace'
    assert _parse_json(text2) == {"outer": {"inner": 1}, "ok": True}

    # ```-fenced JSON + prose после.
    text3 = '```json\n{"fenced": 1}\n```\n then prose with } brace'
    assert _parse_json(text3) == {"fenced": 1}

    # Никакого JSON → JSONDecodeError.
    with pytest.raises(json.JSONDecodeError):
        _parse_json("no json here at all")
