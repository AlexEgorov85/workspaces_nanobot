"""Тесты для SemanticRecord (Этап 29 из PLAN.md)."""

from __future__ import annotations

from workspace.skills.legal_summarizer.scripts.structure.semantic_record import (
    Provenance,
    SemanticRecord,
)


def test_semantic_record_minimal():
    r = SemanticRecord.from_minimal("c1", "s1", "short summary")
    assert r.chunk_id == "c1"
    assert r.section_id == "s1"
    assert r.summary == "short summary"
    assert r.facts == ()
    assert r.confidence == 0.5


def test_semantic_record_full():
    prov = Provenance(start_block=10, end_block=20, page_start=2, page_end=3)
    r = SemanticRecord(
        chunk_id="c1",
        section_id="s1",
        summary="main",
        facts=("fact 1", "fact 2"),
        entities=("Company A",),
        obligations=("X обязуется Z",),
        dates=("01.01.2024",),
        amounts=("10000 руб.",),
        risks=("риск задержки",),
        references=("Статья 5",),
        confidence=0.95,
        provenance=prov,
    )
    assert r.facts == ("fact 1", "fact 2")
    assert r.provenance.page_start == 2


def test_semantic_record_to_dict_roundtrip():
    r = SemanticRecord.from_minimal(
        "c1", "s1", "x",
        provenance=Provenance(start_block=0, end_block=10),
    )
    d = r.to_dict()
    assert d["chunk_id"] == "c1"
    assert d["provenance"]["end_block"] == 10


def test_provenance_optional():
    r = SemanticRecord(chunk_id="c1", section_id="s1", summary="x")
    assert r.provenance is None


def test_semantic_record_immutable():
    import dataclasses
    r = SemanticRecord.from_minimal("c1", "s1", "x")
    with __import__("pytest").raises(dataclasses.FrozenInstanceError):
        r.summary = "mutated"


def test_semantic_record_empty_collections_default():
    r = SemanticRecord(chunk_id="c", section_id="s", summary="x")
    assert r.facts == ()
    assert r.entities == ()
    assert r.obligations == ()
    assert r.dates == ()
    assert r.amounts == ()
    assert r.risks == ()
    assert r.references == ()