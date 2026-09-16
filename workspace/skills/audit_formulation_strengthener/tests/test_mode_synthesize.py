"""Тесты режима ``synthesize`` — финальный отчёт."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_SKILL_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(_SKILL_SCRIPTS))


def test_synthesize_estimate_only() -> None:
    """``--estimate-only`` без LLM."""
    from modes import synthesize

    result, report_text = synthesize.run(
        violation="текст",
        search_result={
            "status": "success",
            "data": {"vnd_findings": [{"x": 1}, {"x": 2}]},
        },
        estimate_only=True,
    )
    assert result["status"] == "success"
    assert result["data"]["vnd_findings_available"] == 2
    assert report_text is None


def test_synthesize_full_with_mocks(mock_llm_synthesize) -> None:
    """Полный синтез: analyze + search → markdown-отчёт."""
    from modes import synthesize

    analyze_result = {
        "status": "success",
        "data": {
            "normalized": "В организации установлен срок хранения ПДн — 1 год.",
            "severity": "высокая",
            "key_concepts": ["срок хранения", "ПДн"],
        },
    }
    search_result = {
        "status": "success",
        "data": {
            "vnd_findings": [
                {
                    "source_file": "vnd1.txt",
                    "section_title": "5.4",
                    "section_path": "5 / 5.4",
                    "text_excerpt": "Срок хранения ПДн — не менее 5 лет.",
                    "relation_type": "прямое_противоречие",
                    "relevance_score": 0.9,
                    "why_matches": "Прямое противоречие.",
                }
            ]
        },
    }

    with pytest.MonkeyPatch.context() as m:
        m.setattr(synthesize, "call_llm_json", mock_llm_synthesize)
        result, report_text = synthesize.run(
            violation="исходный",
            analyze_result=analyze_result,
            search_result=search_result,
        )

    assert result["status"] == "success"
    data = result["data"]
    assert data["title"].startswith("Анализ отклонения")
    assert data["severity"] == "высокая"
    assert "date_iso" in data
    assert isinstance(data["vnd_citations"], list)
    assert len(data["vnd_citations"]) >= 1
    assert isinstance(data["recommended_formulation"], list)

    # Markdown-отчёт не пуст и содержит ключевые секции.
    assert report_text is not None
    md = report_text
    assert "# Анализ отклонения" in md
    assert "## 1. Краткое изложение" in md
    assert "## 5. Итоговая классификация" in md
    assert "## 6. Рекомендуемая усиленная формулировка" in md


def test_synthesize_no_vnd_findings(mock_llm_synthesize) -> None:
    """Если нет ВНД-findings — синтез всё равно работает."""
    from modes import synthesize

    with pytest.MonkeyPatch.context() as m:
        m.setattr(synthesize, "call_llm_json", mock_llm_synthesize)
        result, report_text = synthesize.run(
            violation="что-то",
            search_result={"status": "success", "data": {"vnd_findings": []}},
        )

    assert result["status"] == "success"
    # В citations — что-то (LLM может вернуть пустой массив или дефолты).
    assert isinstance(result["data"]["vnd_citations"], list)
    assert report_text is not None


def test_synthesize_severity_fallback_when_no_analyze(mock_llm_synthesize) -> None:
    """Если analyze не передан — severity = 'средняя'."""
    from modes import synthesize

    with pytest.MonkeyPatch.context() as m:
        m.setattr(synthesize, "call_llm_json", mock_llm_synthesize)
        result, _ = synthesize.run(violation="что-то")

    assert result["status"] == "success"
    assert result["data"]["severity"] == "средняя"


def test_synthesize_save_to_md(mock_llm_synthesize, tmp_path: Path) -> None:
    """Сохранение в .md-файл."""
    from modes import synthesize

    target = tmp_path / "report.md"

    with pytest.MonkeyPatch.context() as m:
        m.setattr(synthesize, "call_llm_json", mock_llm_synthesize)
        result, report_text = synthesize.run(
            violation="что-то",
            output_format="md",
            output_path=str(target),
        )

    assert result["status"] == "success"
    assert result.get("saved_to") == str(target)
    assert target.exists()
    content = target.read_text(encoding="utf-8")
    assert content == report_text
    assert "# Анализ отклонения" in content


def test_synthesize_save_to_txt(mock_llm_synthesize, tmp_path: Path) -> None:
    """Сохранение в .txt (с простым strip-markdown)."""
    from modes import synthesize

    target = tmp_path / "report.txt"

    with pytest.MonkeyPatch.context() as m:
        m.setattr(synthesize, "call_llm_json", mock_llm_synthesize)
        result, _ = synthesize.run(
            violation="что-то",
            output_format="txt",
            output_path=str(target),
        )

    assert result["status"] == "success"
    assert target.exists()
    content = target.read_text(encoding="utf-8")
    # Нет markdown-заголовков.
    assert "##" not in content
    assert "**" not in content
    # Но есть текст.
    assert "Анализ отклонения" in content


def test_synthesize_save_to_docx(mock_llm_synthesize, tmp_path: Path) -> None:
    """Сохранение в .docx через python-docx."""
    from modes import synthesize

    target = tmp_path / "report.docx"

    with pytest.MonkeyPatch.context() as m:
        m.setattr(synthesize, "call_llm_json", mock_llm_synthesize)
        result, _ = synthesize.run(
            violation="что-то",
            output_format="docx",
            output_path=str(target),
        )

    assert result["status"] == "success"
    assert target.exists()
    assert target.stat().st_size > 1000  # не пустой


def test_synthesize_load_from_file(
    mock_llm_synthesize, tmp_path: Path
) -> None:
    """Загрузка analyze/search из файлов."""
    from modes import synthesize

    a_file = tmp_path / "a.json"
    a_file.write_text(
        '{"data": {"normalized": "FROM FILE", "severity": "низкая"}}',
        encoding="utf-8",
    )
    s_file = tmp_path / "s.json"
    s_file.write_text('{"data": {"vnd_findings": []}}', encoding="utf-8")

    with pytest.MonkeyPatch.context() as m:
        m.setattr(synthesize, "call_llm_json", mock_llm_synthesize)
        result, _ = synthesize.run(
            violation="orig",
            analyze_result_path=str(a_file),
            search_result_path=str(s_file),
        )

    assert result["status"] == "success"
    assert result["data"]["normalized_violation"] == "FROM FILE"
    assert result["data"]["severity"] == "низкая"


def test_synthesize_invalid_citation_relation_normalized(mock_llm_synthesize) -> None:
    """Невалидный relation_type в citations → ``контекст``."""
    from modes import synthesize

    def bad_citations(system, user, operation):
        d = dict(mock_llm_synthesize(system, user, operation))
        d["vnd_citations"] = [
            {
                "source_file": "x",
                "section_title": "y",
                "excerpt": "...",
                "relation_type": "непонятно_что",
                "relation_explanation": "...",
            }
        ]
        return d

    with pytest.MonkeyPatch.context() as m:
        m.setattr(synthesize, "call_llm_json", bad_citations)
        result, _ = synthesize.run(violation="что-то")

    assert result["status"] == "success"
    assert result["data"]["vnd_citations"][0]["relation_type"] == "контекст"
