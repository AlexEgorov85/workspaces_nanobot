"""Тесты для reference QA (Этап 52 из PLAN.md)."""

from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.structure.reference_qa import (
    ReferenceQuestion, ReferenceQASet,
    evaluate_retrieval, standard_qa_set,
)


def test_reference_question_dataclass():
    q = ReferenceQuestion(
        query="test", expected_section_keywords=("a", "b"),
    )
    assert q.query == "test"
    assert len(q.expected_section_keywords) == 2


def test_reference_qa_set_dataclass():
    qa = ReferenceQASet(
        document_name="doc",
        questions=(ReferenceQuestion(query="q", expected_section_keywords=()),),
    )
    assert qa.document_name == "doc"


def test_standard_qa_set_has_six_questions():
    qa = standard_qa_set()
    assert len(qa.questions) == 6
    queries = [q.query for q in qa.questions]
    assert any("цена" in q for q in queries)
    assert any("срок" in q for q in queries)
    assert any("штраф" in q for q in queries)


def test_evaluate_retrieval_hit():
    q = ReferenceQuestion(
        query="цена", expected_section_keywords=("цена", "стоимость"),
    )
    result = evaluate_retrieval(("Цена договора 1000 руб.",), q)
    assert result["hit"] is True
    assert result["score"] >= 0.5


def test_evaluate_retrieval_miss():
    q = ReferenceQuestion(
        query="q", expected_section_keywords=("ключевое",),
    )
    result = evaluate_retrieval(("Другой текст без нужных слов.",), q)
    assert result["hit"] is False
    assert result["score"] == 0.0


def test_evaluate_retrieval_empty():
    q = ReferenceQuestion(
        query="q", expected_section_keywords=("a",),
    )
    result = evaluate_retrieval((), q)
    assert result["hit"] is False
    assert result["score"] == 0.0


def test_evaluate_retrieval_partial_hit():
    q = ReferenceQuestion(
        query="q", expected_section_keywords=("a", "b", "c"),
    )
    result = evaluate_retrieval(("a and b",), q)
    assert result["hits_count"] == 2
    assert abs(result["score"] - 2 / 3) < 0.01