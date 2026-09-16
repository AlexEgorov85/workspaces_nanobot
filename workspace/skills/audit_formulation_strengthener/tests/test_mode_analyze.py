"""Тесты режима ``analyze`` — нормализация отклонения через LLM."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


# Импортируем модуль (для изоляции подменяем мок LLM на уровне импорта).
_SKILL_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(_SKILL_SCRIPTS))


def test_analyze_empty_violation() -> None:
    """Пустая формулировка → ошибка ``empty_violation``."""
    from modes import analyze

    result, report_text = analyze.run(violation="")
    assert result["status"] == "error"
    assert result["data"]["error_type"] == "empty_violation"
    assert report_text is None


def test_analyze_whitespace_only_violation() -> None:
    """Только пробелы → ошибка ``empty_violation``."""
    from modes import analyze

    result, _ = analyze.run(violation="   \n\t  ")
    assert result["status"] == "error"
    assert result["data"]["error_type"] == "empty_violation"


def test_analyze_estimate_only() -> None:
    """``--estimate-only`` → нет LLM-вызова, возвращает оценку."""
    from modes import analyze

    result, report_text = analyze.run(
        violation="Срок хранения ПДн — 1 год",
        estimate_only=True,
    )
    assert result["status"] == "success"
    assert result["data"]["violation_chars"] == len("Срок хранения ПДн — 1 год")
    assert result["data"]["llm_calls_planned"] == 1
    assert report_text is None


def test_analyze_success_with_mock_llm(mock_llm_analyze) -> None:
    """Полный успешный прогон analyze с моком LLM."""
    from modes import analyze

    with pytest.MonkeyPatch.context() as m:
        m.setattr(analyze, "call_llm_json", mock_llm_analyze)
        result, report_text = analyze.run(
            violation="Срок хранения персональные данные — 1 год"
        )

    assert result["status"] == "success"
    data = result["data"]
    assert data["normalized"].startswith("В организации")
    assert "срок хранения" in data["key_concepts"]
    assert "персональные данные" in data["key_concepts"]
    assert data["severity"] == "высокая"
    assert "Сроки хранения ПДн" in data["suggested_vnd_sections"]
    assert report_text is None


def test_analyze_severity_normalization(mock_llm_analyze) -> None:
    """LLM вернул нестандартный severity → нормализуется в 'средняя'."""
    from modes import analyze

    def bad_severity(system, user, operation):
        d = dict(mock_llm_analyze(system, user, operation))
        d["severity"] = "критическая!!"
        return d

    with pytest.MonkeyPatch.context() as m:
        m.setattr(analyze, "call_llm_json", bad_severity)
        result, _ = analyze.run(violation="...любой текст...")

    assert result["status"] == "success"
    assert result["data"]["severity"] == "средняя"


def test_analyze_invalid_json_after_retries() -> None:
    """LLM возвращает невалидный JSON после всех попыток → json_parse_failed."""
    from modes import analyze
    from llm import JsonParseError

    def raises(system, user, operation):
        raise JsonParseError("invalid JSON")

    with pytest.MonkeyPatch.context() as m:
        m.setattr(analyze, "call_llm_json", raises)
        result, _ = analyze.run(violation="что-то")

    assert result["status"] == "error"
    assert result["data"]["error_type"] == "json_parse_failed"


def test_analyze_schema_mismatch(mock_llm_analyze) -> None:
    """LLM вернул dict без ``normalized`` → schema_mismatch."""
    from modes import analyze

    def no_normalized(system, user, operation):
        return {"key_concepts": [], "severity": "средняя"}

    with pytest.MonkeyPatch.context() as m:
        m.setattr(analyze, "call_llm_json", no_normalized)
        result, _ = analyze.run(violation="что-то")

    assert result["status"] == "error"
    assert result["data"]["error_type"] == "schema_mismatch"


def test_analyze_extras_preserved(mock_llm_analyze) -> None:
    """Дополнительные поля от LLM (например, ``notes``) сохраняются в ``extras``."""
    from modes import analyze

    def with_extras(system, user, operation):
        d = dict(mock_llm_analyze(system, user, operation))
        d["notes"] = "дополнительные пояснения"
        return d

    with pytest.MonkeyPatch.context() as m:
        m.setattr(analyze, "call_llm_json", with_extras)
        result, _ = analyze.run(violation="...")

    assert result["status"] == "success"
    assert result["data"].get("extras", {}).get("notes") == "дополнительные пояснения"
