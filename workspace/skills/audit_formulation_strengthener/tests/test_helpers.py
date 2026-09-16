"""Тесты вспомогательных модулей: ``vnd_io``, ``prompts``, ``output``."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_SKILL_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(_SKILL_SCRIPTS))


# ---------------------------------------------------------------------------
# tests for vnd_io
# ---------------------------------------------------------------------------


def test_build_cache_key_deterministic() -> None:
    """Тот же violation + vnd_paths → тот же cache_key."""
    from vnd_io import build_cache_key

    k1 = build_cache_key(violation="X", vnd_paths=["a.pdf", "b.pdf"])
    k2 = build_cache_key(violation="X", vnd_paths=["b.pdf", "a.pdf"])  # другой порядок
    # build_cache_key сортирует, так что ключи должны совпасть.
    assert k1 == k2

    k3 = build_cache_key(violation="Y", vnd_paths=["a.pdf"])
    assert k1 != k3


def test_build_cache_key_format() -> None:
    """cache_key — 64 hex chars (SHA-256)."""
    from vnd_io import build_cache_key

    k = build_cache_key(violation="x", vnd_paths=["a"])
    assert len(k) == 64
    int(k, 16)  # hex-валидный


def test_prepare_vnd_no_vnd_paths() -> None:
    """Пустой список путей → VndInputError."""
    from vnd_io import VndInputError, prepare_vnd

    with pytest.raises(VndInputError) as exc_info:
        prepare_vnd(vnd_paths=[])
    assert exc_info.value.error_type == "no_vnd"


def test_prepare_vnd_file_not_found(tmp_path: Path) -> None:
    """Несуществующий файл → VndInputError(vnd_not_found)."""
    from vnd_io import VndInputError, prepare_vnd

    with pytest.raises(VndInputError) as exc_info:
        prepare_vnd(vnd_paths=[str(tmp_path / "nope.pdf")])
    assert exc_info.value.error_type == "vnd_not_found"


# ---------------------------------------------------------------------------
# tests for prompts
# ---------------------------------------------------------------------------


def test_load_prompt_success() -> None:
    """Загрузка существующего промпта."""
    from prompts import load_prompt

    text = load_prompt("analyze_system")
    assert "старший аудитор-методолог" in text
    assert "{{VIOLATION_TEXT}}" in text


def test_load_prompt_missing() -> None:
    """Несуществующий промпт → FileNotFoundError."""
    from prompts import load_prompt

    with pytest.raises(FileNotFoundError):
        load_prompt("does_not_exist")


def test_render_prompt_substitution() -> None:
    """Подстановка плейсхолдеров."""
    from prompts import render_prompt

    template = "Hello, {{NAME}}! You are {{ROLE}}."
    out = render_prompt(template, {"NAME": "Аудитор", "ROLE": "методолог"})
    assert out == "Hello, Аудитор! You are методолог."


def test_render_prompt_missing_var() -> None:
    """Отсутствующая переменная → пустая строка."""
    from prompts import render_prompt

    template = "X={{A}}, Y={{B}}"
    out = render_prompt(template, {"A": "1"})
    assert out == "X=1, Y="


def test_render_prompt_none_value() -> None:
    """``None`` → пустая строка."""
    from prompts import render_prompt

    out = render_prompt("X={{A}}", {"A": None})
    assert out == "X="


# ---------------------------------------------------------------------------
# tests for output
# ---------------------------------------------------------------------------


def test_make_error_basic() -> None:
    """Создание error-результата."""
    from output import make_error

    result = make_error("что-то сломалось", error_type="some_error")
    assert result["status"] == "error"
    assert result["data"]["error_type"] == "some_error"
    assert "что-то сломалось" in result["data"]["message"]


def test_make_error_extra_fields() -> None:
    """Доп. поля добавляются в data."""
    from output import make_error

    result = make_error(
        "ошибка",
        error_type="io",
        **{"retry_after_sec": 5},
    )
    assert result["data"]["retry_after_sec"] == 5
