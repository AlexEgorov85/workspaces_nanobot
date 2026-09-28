"""Тесты режима ``synthesize`` (валидация цитат, рендеры)."""

from __future__ import annotations

import pytest

from workspace.skills.audit_formulation_strengthener.scripts.modes import synthesize
from workspace.skills.audit_formulation_strengthener.scripts.report import (
    docx_render,
    markdown,
    plain,
)
from workspace.skills.audit_formulation_strengthener.scripts.llm import JsonParseError

from .conftest import make_analyze_result, make_search_result


# -----------------------------------------------------------------------------
# estimate-only
# -----------------------------------------------------------------------------


def test_estimate_only_requires_vnd(mock_llm_all) -> None:
    result, _ = synthesize.run(
        violation="X",
        vnd_paths=None,
        estimate_only=True,
    )
    assert result["status"] == "error"
    assert result["data"]["error_type"] == "no_vnd"


def test_estimate_only_with_vnd_does_io_no_llm(
    mock_llm_all, sample_vnd_files
) -> None:
    result, report = synthesize.run(
        violation="Срок хранения ПДн установлен 1 год",
        vnd_paths=[str(sample_vnd_files["file1"])],
        estimate_only=True,
    )
    assert result["status"] == "success"
    assert result["data"]["estimate"] is True
    assert result["data"]["llm_calls_planned"] >= 3  # 1 + N + 1
    assert "size_estimate" in result["data"]
    assert report is None
    assert mock_llm_all.counter.chat_json_calls == 0


# -----------------------------------------------------------------------------
# Валидация цитат
# -----------------------------------------------------------------------------


def test_valid_substring_passes_through(mock_llm_all) -> None:
    analyze_res = make_analyze_result(normalized="Нормализованная формулировка")
    search_res = make_search_result(
        findings=[
            {
                "evidence_id": "F1",
                "source_file": "vnd1.txt",
                "chunk_index": 0,
                "text_excerpt": "Срок хранения ПДн — 1 год с момента сбора данных.",
                "relation_type": "прямое_противоречие",
                "relevance_score": 0.85,
                "why_matches": "прямое нарушение",
            },
        ]
    )
    mock_llm_all.set_response(
        "synthesize",
        {
            "title": "Анализ отклонения",
            "violation_summary": ["Срок хранения ПДн"],
            "established_facts": ["Факт"],
            "deviation_analysis": ["Анализ"],
            "vnd_citations": [
                {
                    "evidence_id": "F1",
                    "excerpt": "Срок хранения ПДн — 1 год",  # точная подстрока
                    "relation_explanation": "прямое нарушение нормы",
                },
            ],
            "verdict": {"category": "высокая", "verdict_text": ["вердикт"]},
            "recommended_formulation": ["рекомендация"],
        },
    )

    result, _ = synthesize.run(
        violation="X",
        analyze_result=analyze_res,
        search_result=search_res,
    )
    assert result["status"] == "success"
    assert len(result["data"]["vnd_citations"]) == 1
    assert result["data"]["vnd_citations"][0]["evidence_id"] == "F1"
    assert (
        result["data"]["vnd_citations"][0]["relation_type"] == "прямое_противоречие"
    )  # из находки, не из LLM
    assert result["data"]["citations_dropped"] == 0


def test_fabricated_excerpt_dropped(mock_llm_all) -> None:
    analyze_res = make_analyze_result()
    search_res = make_search_result(
        findings=[
            {
                "evidence_id": "F1",
                "source_file": "vnd1.txt",
                "chunk_index": 0,
                "text_excerpt": "Точный текст из ВНД.",
                "relation_type": "контекст",
                "relevance_score": 0.7,
                "why_matches": "x",
            },
        ]
    )
    mock_llm_all.set_response(
        "synthesize",
        {
            "title": "Анализ",
            "violation_summary": ["x"],
            "established_facts": ["x"],
            "deviation_analysis": ["x"],
            "vnd_citations": [
                {
                    "evidence_id": "F1",
                    "excerpt": "Точный текст ИЗ ВНД.",  # изменено
                    "relation_explanation": "x",
                },
            ],
            "verdict": {"category": "средняя", "verdict_text": ["x"]},
            "recommended_formulation": ["x"],
        },
    )

    result, _ = synthesize.run(
        violation="X",
        analyze_result=analyze_res,
        search_result=search_res,
    )
    assert result["status"] == "success"
    assert result["data"]["vnd_citations"] == []
    assert result["data"]["citations_dropped"] == 1


def test_unknown_evidence_id_dropped(mock_llm_all) -> None:
    analyze_res = make_analyze_result()
    search_res = make_search_result(findings=[])
    mock_llm_all.set_response(
        "synthesize",
        {
            "title": "x",
            "violation_summary": ["x"],
            "established_facts": ["x"],
            "deviation_analysis": ["x"],
            "vnd_citations": [
                {"evidence_id": "F99", "excerpt": "x", "relation_explanation": "x"},
            ],
            "verdict": {"category": "средняя", "verdict_text": ["x"]},
            "recommended_formulation": ["x"],
        },
    )

    result, _ = synthesize.run(
        violation="X",
        analyze_result=analyze_res,
        search_result=search_res,
    )
    assert result["data"]["citations_dropped"] == 1
    assert result["data"]["vnd_citations"] == []


def test_all_citations_dropped_is_still_success(mock_llm_all) -> None:
    analyze_res = make_analyze_result()
    search_res = make_search_result(findings=[])
    mock_llm_all.set_response(
        "synthesize",
        {
            "title": "x",
            "violation_summary": ["x"],
            "established_facts": [],
            "deviation_analysis": [],
            "vnd_citations": [
                {"evidence_id": "F1", "excerpt": "x", "relation_explanation": "x"},
            ],
            "verdict": {"category": "средняя", "verdict_text": ["x"]},
            "recommended_formulation": ["x"],
        },
    )

    result, _ = synthesize.run(
        violation="X",
        analyze_result=analyze_res,
        search_result=search_res,
    )
    assert result["status"] == "success"
    assert result["data"]["citations_dropped"] == 1
    assert "warning" not in result["data"]  # не меняем status


# -----------------------------------------------------------------------------
# Resume из файлов
# -----------------------------------------------------------------------------


def test_unreadable_analyze_result_file(
    mock_llm_all, sample_vnd_files
) -> None:
    result, _ = synthesize.run(
        violation="X",
        vnd_paths=[str(sample_vnd_files["file1"])],
        analyze_result_path="/nonexistent/analyze.json",
    )
    assert result["status"] == "error"
    assert result["data"]["error_type"] == "analyze_result_unreadable"


def test_unreadable_search_result_file(mock_llm_all, sample_vnd_files) -> None:
    result, _ = synthesize.run(
        violation="X",
        vnd_paths=[str(sample_vnd_files["file1"])],
        search_result_path="/nonexistent/search.json",
    )
    assert result["status"] == "error"
    assert result["data"]["error_type"] == "search_result_unreadable"


# -----------------------------------------------------------------------------
# Рендеры
# -----------------------------------------------------------------------------


def test_markdown_renders_6_sections() -> None:
    data = {
        "title": "Тест",
        "violation_summary": ["summary"],
        "established_facts": ["fact"],
        "deviation_analysis": ["analysis"],
        "vnd_citations": [
            {
                "evidence_id": "F1",
                "source_file": "vnd.txt",
                "chunk_index": 0,
                "excerpt": "Цитата из ВНД",
                "relation_type": "контекст",
                "relation_explanation": "объяснение",
            },
        ],
        "citations_dropped": 0,
        "verdict": {"category": "средняя", "verdict_text": ["вердикт"]},
        "recommended_formulation": ["рекомендация"],
        "normalized_violation": "нормал.",
        "severity": "средняя",
        "source_findings_count": 1,
        "date_iso": "2026-09-18T00:00:00+00:00",
    }
    md = markdown.render_markdown(data)
    assert "## 1. Краткое изложение отклонения" in md
    assert "## 2. Установленные факты (по ВНД)" in md
    assert "## 3. Анализ отклонения" in md
    assert "## 4. Релевантные фрагменты ВНД (валидированные цитаты)" in md
    assert "## 5. Итоговая классификация" in md
    assert "## 6. Рекомендуемая усиленная формулировка" in md
    # Цитата в blockquote.
    assert "> Цитата из ВНД" in md
    # Метаданные.
    assert "F1" in md
    assert "vnd.txt" in md


def test_markdown_renders_empty_sections_with_placeholder() -> None:
    data = {
        "title": "x",
        "violation_summary": [],
        "established_facts": [],
        "deviation_analysis": [],
        "vnd_citations": [],
        "citations_dropped": 0,
        "verdict": {"category": "средняя", "verdict_text": []},
        "recommended_formulation": [],
        "normalized_violation": "",
        "severity": "средняя",
        "source_findings_count": 0,
        "date_iso": "",
    }
    md = markdown.render_markdown(data)
    # Все 6 секций с пустым содержимым получают плейсхолдер.
    assert md.count("*(секция не заполнена:") == 6  # 1, 2, 3, 4, 5, 6


def test_plain_strips_markdown() -> None:
    data = {
        "title": "Тест",
        "violation_summary": ["summary"],
        "established_facts": ["fact"],
        "deviation_analysis": ["analysis"],
        "vnd_citations": [],
        "citations_dropped": 0,
        "verdict": {"category": "средняя", "verdict_text": ["вердикт"]},
        "recommended_formulation": ["рекомендация"],
        "normalized_violation": "",
        "severity": "средняя",
        "source_findings_count": 0,
        "date_iso": "",
    }
    out = plain.render_plain(data)
    assert "##" not in out
    assert "**" not in out
    assert "summary" in out


def test_docx_import_error_message() -> None:
    """Если python-docx недоступен — RuntimeError с понятным сообщением."""
    from workspace.skills.audit_formulation_strengthener.scripts.report import (
        docx_render as docx_mod,
    )
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "docx":
            raise ImportError("simulated: python-docx не установлен")
        return real_import(name, *args, **kwargs)

    data = {
        "title": "x",
        "violation_summary": [],
        "established_facts": [],
        "deviation_analysis": [],
        "vnd_citations": [],
        "citations_dropped": 0,
        "verdict": {"category": "средняя", "verdict_text": []},
        "recommended_formulation": [],
        "normalized_violation": "",
        "severity": "средняя",
        "source_findings_count": 0,
        "date_iso": "",
    }

    builtins.__import__ = fake_import
    try:
        with pytest.raises(RuntimeError, match="python-docx"):
            docx_mod.write_docx(data, "/tmp/nope.docx")
    finally:
        builtins.__import__ = real_import
