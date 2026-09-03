"""Tests for legal_summarizer PR2 (Phase 1-9).

Provenance correctness, structural brief coverage, cache-assisted
follow-up retrieval, context expansion, reconstruction, E2E.

Phase 1: Chunk provenance (source_char_start/end + reconstruct_source_fragment)
Phase 2: brief structural coverage + bounded representation (PhysicalDocument invariant)
Phase 4: cache provenance persistence + freshness check
Phase 5: cached candidate selection (lexical scoring + confidence threshold)
Phase 6: exact PhysicalDocument reconstruction
Phase 7: context expansion (target + prev + next)
Phase 8: cache-assisted follow-up retrieval integration
Phase 9: E2E second-run test (cache → reload PhysicalDocument → detailed answer)
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest


_REPO = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO / "workspace" / "skills" / "legal_summarizer" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_provenance_block(
    ordinal: int,
    content: str,
    *,
    page_index: int | None = None,
    block_type: str = "paragraph",
    table_index: int | None = None,
    row_count: int = 1,
) -> "DocumentBlock":
    from workspace.skills.legal_summarizer.scripts.structure.physical import (
        DocumentBlock,
    )

    meta: dict = {}
    if block_type == "table":
        meta["row_count"] = row_count
    return DocumentBlock(
        block_id=f"b_{ordinal:04d}",
        block_type=block_type,
        content=content,
        char_count=len(content),
        page_index=page_index,
        page_start=page_index,
        page_end=page_index,
        paragraph_index=ordinal if block_type != "table" else None,
        table_index=table_index,
        ordinal=ordinal,
        block_metadata=meta,
    )


def _make_provenance_doc(blocks: tuple) -> "PhysicalDocument":
    from workspace.skills.legal_summarizer.scripts.structure.physical import (
        PhysicalDocument,
    )

    pages = sorted({b.page_index for b in blocks if b.page_index is not None})
    return PhysicalDocument(
        path="<inline>",
        format="txt",
        title=None,
        size_bytes=100,
        blocks=tuple(blocks),
        page_count=pages[-1] if pages else 1,
    )


def _make_cached_candidate(
    cid: str,
    *,
    block_indices: tuple[int, ...] = (0,),
    source_char_start: int | None = None,
    source_char_end: int | None = None,
    table_id: str | None = None,
    table_row_start: int | None = None,
    table_row_end: int | None = None,
    preview: str = "",
) -> "CachedCandidate":
    from workspace.skills.legal_summarizer.scripts.cached_retrieval import (
        CachedCandidate,
    )
    return CachedCandidate(
        chunk_id=cid,
        score=3,
        summary="Штраф за просрочку договора подряда.",
        section_id="s_0001",
        section_path="5.3",
        page_start=42,
        page_end=42,
        block_indices=block_indices,
        block_types=("paragraph",) * len(block_indices),
        source_char_start=source_char_start,
        source_char_end=source_char_end,
        table_id=table_id,
        table_row_start=table_row_start,
        table_row_end=table_row_end,
        chunk_text_preview=preview,
    )


def _make_cache_record(
    cid: str,
    *,
    summary: str = "",
    preview: str = "",
    section_id: str | None = "s_0001",
    section_path: str | None = "1",
    block_indices: tuple[int, ...] = (0,),
    source_start: int | None = None,
    source_end: int | None = None,
) -> dict:
    return {
        "chunk_id": cid,
        "summary": summary,
        "chunk_text_preview": preview,
        "section_id": section_id,
        "section_path": section_path,
        "page_start": 1,
        "page_end": 1,
        "block_indices": list(block_indices),
        "block_types": ["paragraph"] * len(block_indices),
        "source_char_start": source_start,
        "source_char_end": source_end,
        "table_id": None,
        "table_row_start": None,
        "table_row_end": None,
        "saved_at": "2026-09-02T00:00:00Z",
    }


# ---------------------------------------------------------------------------
# Phase 1: Chunk provenance + reconstruct_source_fragment
# ---------------------------------------------------------------------------


def test_reconstruct_whole_block_returns_exact_content():
    """reconstruct_source_fragment: целый block (source_char_*=None) → block.content."""
    from workspace.skills.legal_summarizer.scripts.structure.chunks import (
        Chunk,
        reconstruct_source_fragment,
    )

    block = _make_provenance_block(0, "Полный текст одного блока про договор.")
    doc = _make_provenance_doc((block,))

    chunk = Chunk(
        chunk_id="000",
        index=0,
        text=block.content,
        char_count=len(block.content),
        token_estimate=10,
        page_start=block.page_index,
        page_end=block.page_end,
        section_id="s_0001",
        section_path="1",
        section_heading="1. Раздел",
        block_indices=(0,),
        block_types=("paragraph",),
    )

    assert reconstruct_source_fragment(chunk, doc=doc) == block.content
    assert reconstruct_source_fragment(chunk, doc=doc) == chunk.text


def test_reconstruct_split_chunk_exactly():
    """reconstruct_source_fragment: split chunk → точная подстрока block.content."""
    from workspace.skills.legal_summarizer.scripts.structure.chunks import (
        Chunk,
        reconstruct_source_fragment,
    )

    full = "X" * 9000
    block = _make_provenance_block(0, full)
    doc = _make_provenance_doc((block,))

    chunk = Chunk(
        chunk_id="001",
        index=1,
        text=full[3000:6000],
        char_count=3000,
        token_estimate=857,
        page_start=1,
        page_end=1,
        section_id="s_0001",
        section_path="1",
        section_heading="",
        block_indices=(0,),
        block_types=("paragraph",),
        source_char_start=3000,
        source_char_end=6000,
    )

    reconstructed = reconstruct_source_fragment(chunk, doc=doc)
    assert reconstructed == full[3000:6000]
    assert reconstructed == chunk.text
    assert len(reconstructed) == 3000


def test_reconstruct_three_split_parts_cover_block_without_gaps():
    """Каждая из 3 split-частей block 10000 chars восстанавливается точно."""
    from workspace.skills.legal_summarizer.scripts.structure.chunks import (
        Chunk,
        reconstruct_source_fragment,
    )

    full = ("A" * 3000) + ("B" * 3000) + ("C" * 3000) + ("D" * 1000)
    block = _make_provenance_block(0, full)
    doc = _make_provenance_doc((block,))

    for cid, cs, ce in [(0, 0, 3000), (1, 3000, 6000), (2, 6000, 9000)]:
        c = Chunk(
            chunk_id=f"{cid:03d}",
            index=cid,
            text=full[cs:ce],
            char_count=ce - cs,
            token_estimate=(ce - cs) // 4,
            page_start=1,
            page_end=1,
            section_id="s_0001",
            section_path="1",
            section_heading="",
            block_indices=(0,),
            block_types=("paragraph",),
            source_char_start=cs,
            source_char_end=ce,
        )
        assert reconstruct_source_fragment(c, doc=doc) == full[cs:ce]


def test_reconstruct_table_chunk_returns_full_table():
    """Table chunks атомарны (max_chunk_chars > table_chunk_threshold)."""
    from workspace.skills.legal_summarizer.scripts.structure.chunks import (
        Chunk,
        reconstruct_source_fragment,
    )

    table_text = "Заголовок | Колонка 1 | Колонка 2\nA1 | B1 | C1\nA2 | B2 | C2"
    block = _make_provenance_block(
        0, table_text, block_type="table", table_index=0, row_count=3,
    )
    doc = _make_provenance_doc((block,))

    chunk = Chunk(
        chunk_id="000",
        index=0,
        text=table_text,
        char_count=len(table_text),
        token_estimate=20,
        page_start=1,
        page_end=1,
        section_id="s_0001",
        section_path="1",
        section_heading="",
        block_indices=(0,),
        block_types=("table",),
        table_id="t_0000",
        table_row_start=1,
        table_row_end=3,
    )

    assert reconstruct_source_fragment(chunk, doc=doc) == table_text


def test_reconstruct_multi_block_chunk_returns_concat():
    """Chunk, покрывающий 2+ body blocks → конкатенация содержимого."""
    from workspace.skills.legal_summarizer.scripts.structure.chunks import (
        Chunk,
        reconstruct_source_fragment,
    )

    b1 = _make_provenance_block(0, "Тело первого блока.")
    b2 = _make_provenance_block(1, "Тело второго блока.")
    doc = _make_provenance_doc((b1, b2))

    chunk = Chunk(
        chunk_id="000",
        index=0,
        text="\n\n".join([b1.content, b2.content]),
        char_count=len(b1.content) + len(b2.content) + 2,
        token_estimate=15,
        page_start=1,
        page_end=2,
        section_id="s_0001",
        section_path="1",
        section_heading="",
        block_indices=(0, 1),
        block_types=("paragraph", "paragraph"),
    )

    assert reconstruct_source_fragment(chunk, doc=doc) == chunk.text


def test_reconstruct_preserves_page_section_metadata():
    """Provenance сохраняет page + section metadata."""
    from workspace.skills.legal_summarizer.scripts.structure.chunks import (
        Chunk,
        reconstruct_source_fragment,
    )

    full = "Какой-то юридический текст" * 200
    block = _make_provenance_block(0, full, page_index=42)
    doc = _make_provenance_doc((block,))

    chunk = Chunk(
        chunk_id="000",
        index=0,
        text=full[100:200],
        char_count=100,
        token_estimate=29,
        page_start=42,
        page_end=42,
        section_id="s_0005",
        section_path="5.3",
        section_heading="5.3 Штрафные санкции",
        block_indices=(0,),
        block_types=("paragraph",),
        source_char_start=100,
        source_char_end=200,
    )

    assert chunk.page_start == 42
    assert chunk.section_id == "s_0005"
    assert chunk.section_path == "5.3"
    assert reconstruct_source_fragment(chunk, doc=doc) == full[100:200]


def test_chunker_sets_source_offsets_for_split_block():
    """StructureAwareChunker проставляет source_char_* при split fallback."""
    from workspace.skills.legal_summarizer.scripts.structure.chunks import (
        ChunkConfig,
        StructureAwareChunker,
        reconstruct_source_fragment,
    )
    from workspace.skills.legal_summarizer.scripts.structure.sections import (
        detect_sections,
    )

    text = (
        "Первое предложение длинного параграфа. " * 100
        + "Второе предложение другого характера. " * 100
        + "Третий блок предложений в этом же параграфе. " * 100
    )
    big = text
    assert len(big) > 2000

    blocks = (
        _make_provenance_block(0, "1. Раздел с большим блоком"),
        _make_provenance_block(1, big, page_index=1),
    )
    doc = _make_provenance_doc(blocks)
    tree = detect_sections(doc, pdf_path=None)
    chunks = StructureAwareChunker().chunk(
        doc, tree,
        ChunkConfig(max_chunk_chars=2000, chunk_overlap_chars=0),
    )
    body_chunks = [c for c in chunks if c.block_indices == (1,)]
    assert len(body_chunks) >= 2, (
        f"oversized block должен давать >1 chunks, got {len(body_chunks)}"
    )
    for c in body_chunks:
        assert c.source_char_start is not None
        assert c.source_char_end is not None
        assert 0 <= c.source_char_start < c.source_char_end <= len(big)
        assert reconstruct_source_fragment(c, doc=doc) == big[c.source_char_start:c.source_char_end]


def test_chunker_whole_block_has_none_offsets():
    """Chunk без split (целый block) имеет source_char_*=None."""
    from workspace.skills.legal_summarizer.scripts.structure.chunks import (
        ChunkConfig,
        StructureAwareChunker,
    )
    from workspace.skills.legal_summarizer.scripts.structure.sections import (
        detect_sections,
    )

    blocks = (
        _make_provenance_block(0, "1. Короткий раздел"),
        _make_provenance_block(1, "Короткий текст, не split."),
    )
    doc = _make_provenance_doc(blocks)
    tree = detect_sections(doc, pdf_path=None)
    chunks = StructureAwareChunker().chunk(
        doc, tree,
        ChunkConfig(max_chunk_chars=4000, chunk_overlap_chars=0),
    )

    for c in chunks:
        if len(c.block_indices) == 1 and c.block_types != ("table",):
            assert c.source_char_start is None
            assert c.source_char_end is None


def test_reconstruct_raises_on_unknown_block_ordinal():
    """reconstruct_source_fragment падает, если block_index не найден."""
    from workspace.skills.legal_summarizer.scripts.structure.chunks import (
        Chunk,
        reconstruct_source_fragment,
    )

    block = _make_provenance_block(0, "x")
    doc = _make_provenance_doc((block,))

    chunk = Chunk(
        chunk_id="000",
        index=0,
        text="x",
        char_count=1,
        token_estimate=1,
        page_start=1,
        page_end=1,
        section_id="s_root",
        section_path="",
        section_heading="",
        block_indices=(999,),
        block_types=("paragraph",),
    )

    with pytest.raises(ValueError, match="не найден"):
        reconstruct_source_fragment(chunk, doc=doc)


def test_reconstruct_raises_on_offsets_out_of_range():
    """reconstruct_source_fragment падает на out-of-range offsets."""
    from workspace.skills.legal_summarizer.scripts.structure.chunks import (
        Chunk,
        reconstruct_source_fragment,
    )

    block = _make_provenance_block(0, "ABCDE")
    doc = _make_provenance_doc((block,))

    chunk = Chunk(
        chunk_id="000",
        index=0,
        text="CDE",
        char_count=3,
        token_estimate=1,
        page_start=1,
        page_end=1,
        section_id="s_root",
        section_path="",
        section_heading="",
        block_indices=(0,),
        block_types=("paragraph",),
        source_char_start=2,
        source_char_end=999,
    )

    with pytest.raises(ValueError, match="вне диапазона"):
        reconstruct_source_fragment(chunk, doc=doc)


def test_chunk_to_dict_round_trip_includes_source_offsets():
    """Chunk.to_dict() сериализует и десериализует source_char_*."""
    from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk

    c = Chunk(
        chunk_id="007",
        index=7,
        text="X",
        char_count=1,
        token_estimate=1,
        page_start=1,
        page_end=1,
        section_id="s_0001",
        section_path="1",
        section_heading="",
        block_indices=(0,),
        block_types=("paragraph",),
        source_char_start=42,
        source_char_end=128,
    )

    d = c.to_dict()
    assert d["source_char_start"] == 42
    assert d["source_char_end"] == 128

    c2 = Chunk(**d)
    assert c2.source_char_start == 42
    assert c2.source_char_end == 128


def test_chunk_to_dict_whole_block_offsets_none():
    """Целый block → source_char_* = None (legacy cache формат)."""
    from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk

    c = Chunk(
        chunk_id="000",
        index=0,
        text="abc",
        char_count=3,
        token_estimate=1,
        page_start=1,
        page_end=1,
        section_id="s_root",
        section_path="",
        section_heading="",
        block_indices=(0,),
        block_types=("paragraph",),
    )

    d = c.to_dict()
    assert d["source_char_start"] is None
    assert d["source_char_end"] is None


# ---------------------------------------------------------------------------
# Phase 2-3: structural brief coverage + bounded LLM-representation
# ---------------------------------------------------------------------------


def test_apply_brief_truncate_disabled_is_noop():
    """brief_truncate_chars_per_block=None → chunks возвращаются без изменений."""
    from workspace.skills.legal_summarizer.scripts.brief_representation import (
        apply_brief_text_budget,
    )
    from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk

    c = Chunk(
        chunk_id="000", index=0, text="A" * 5000, char_count=5000, token_estimate=1429,
        page_start=1, page_end=1, section_id="s_0001", section_path="1", section_heading="",
        block_indices=(0,), block_types=("paragraph",),
    )
    out = apply_brief_text_budget([c], truncate_chars=None)
    assert out == [c]
    out0 = apply_brief_text_budget([c], truncate_chars=0)
    assert out0[0] is c


def test_apply_brief_truncate_does_not_mutate_physical_document():
    """PhysicalDocument.blocks[].content не обрезается — invariant #1, #2."""
    from workspace.skills.legal_summarizer.scripts.brief_representation import (
        apply_brief_text_budget,
    )
    from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk

    original_text = "ABCDEFGH" * 500
    block = _make_provenance_block(0, original_text)
    doc = _make_provenance_doc((block,))

    c = Chunk(
        chunk_id="000", index=0, text=original_text, char_count=4000,
        token_estimate=1143, page_start=1, page_end=1,
        section_id="s_0001", section_path="1", section_heading="",
        block_indices=(0,), block_types=("paragraph",),
        source_char_start=0, source_char_end=4000,
    )

    out = apply_brief_text_budget([c], truncate_chars=200)
    assert len(out[0].text) <= 250
    assert len(out[0].text) < len(c.text)
    assert c.text == original_text
    assert doc.blocks[0].content == original_text
    assert doc.blocks[0].char_count == 4000
    assert out[0].source_char_start == 0
    assert out[0].source_char_end == 4000


def test_apply_brief_truncate_preserves_table_chunks():
    """Table chunks не обрезаются обычным text-truncate."""
    from workspace.skills.legal_summarizer.scripts.brief_representation import (
        apply_brief_text_budget,
    )
    from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk

    table_text = "Заголовок | Кол | Кол\n" + "A | 1 | 2\n" * 50
    c = Chunk(
        chunk_id="000", index=0, text=table_text, char_count=len(table_text),
        token_estimate=len(table_text) // 4, page_start=1, page_end=1,
        section_id="s_0001", section_path="1", section_heading="",
        block_indices=(0,), block_types=("table",),
        table_id="t_0000", table_row_start=1, table_row_end=51,
    )
    out = apply_brief_text_budget([c], truncate_chars=200)
    assert out[0] is c
    assert out[0].table_id == "t_0000"
    assert out[0].table_row_start == 1
    assert out[0].table_row_end == 51


def test_apply_brief_truncate_short_chunk_passthrough():
    """Chunk короче truncate_chars → возвращается как есть."""
    from workspace.skills.legal_summarizer.scripts.brief_representation import (
        apply_brief_text_budget,
    )
    from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk

    c = Chunk(
        chunk_id="000", index=0, text="Короткий текст", char_count=13,
        token_estimate=4, page_start=1, page_end=1,
        section_id="s_0001", section_path="1", section_heading="",
        block_indices=(0,), block_types=("paragraph",),
    )
    out = apply_brief_text_budget([c], truncate_chars=2000)
    assert out[0] is c


def test_apply_brief_truncate_keeps_provenance_for_reconstruction():
    """После truncate, reconstruct_source_fragment возвращает ПОЛНЫЙ текст chunk."""
    from workspace.skills.legal_summarizer.scripts.brief_representation import (
        apply_brief_text_budget,
    )
    from workspace.skills.legal_summarizer.scripts.structure.chunks import (
        Chunk,
        reconstruct_source_fragment,
    )

    full = "Какой-то юридический текст. " * 157
    block = _make_provenance_block(0, full)
    doc = _make_provenance_doc((block,))

    c = Chunk(
        chunk_id="000", index=0, text=full, char_count=len(full),
        token_estimate=len(full) // 4,
        page_start=1, page_end=1, section_id="s_0001", section_path="1",
        section_heading="", block_indices=(0,), block_types=("paragraph",),
        source_char_start=0, source_char_end=len(full),
    )

    truncated_chunks = apply_brief_text_budget([c], truncate_chars=500)
    assert len(truncated_chunks[0].text) < 510
    assert truncated_chunks[0].source_char_start == 0
    assert truncated_chunks[0].source_char_end == len(full)

    full_reconstructed = reconstruct_source_fragment(truncated_chunks[0], doc=doc)
    assert full_reconstructed == full


def test_brief_structural_coverage_covers_multiple_sections():
    """select_brief_chunks_structured round-robin охватывает все секции."""
    from workspace.skills.legal_summarizer.scripts.brief_strategy import (
        select_brief_chunks_structured,
    )
    from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk

    sections = ["s_root", "s_0001", "s_0005", "s_0012", "s_0020"]
    section_objs = {
        "s_root": type("S", (), {"section_id": "s_root", "heading": "", "section_path": ""})(),
        "s_0001": type("S", (), {"section_id": "s_0001", "heading": "1. Раздел 1", "section_path": "1"})(),
        "s_0005": type("S", (), {"section_id": "s_0005", "heading": "5. Раздел 5", "section_path": "5"})(),
        "s_0012": type("S", (), {"section_id": "s_0012", "heading": "12. Раздел 12", "section_path": "12"})(),
        "s_0020": type("S", (), {"section_id": "s_0020", "heading": "20. Раздел 20", "section_path": "20"})(),
    }
    tree = type("T", (), {"root_id": "s_root", "sections": section_objs})()

    chunks = []
    for i in range(16):
        sid = f"s_{['0001','0005','0012','0020'][i % 4]}"
        chunks.append(Chunk(
            chunk_id=f"{i:03d}", index=i, text=f"x{i}", char_count=2,
            token_estimate=1, page_start=1, page_end=1,
            section_id=sid, section_path=sid.lstrip("s_"), section_heading="",
            block_indices=(i,), block_types=("paragraph",),
        ))

    chosen = select_brief_chunks_structured(
        chunks, tree, max_chunks=100, coverage_ratio=0.5,
    )
    sections_covered = {c.section_id for c in chosen if c.section_id != "s_root"}
    assert sections_covered == {"s_0001", "s_0005", "s_0012", "s_0020"}
    for sid in ("s_0001", "s_0005", "s_0012", "s_0020"):
        assert any(c.section_id == sid for c in chosen)


# ---------------------------------------------------------------------------
# Phase 4: document cache provenance + freshness
# ---------------------------------------------------------------------------


def test_doc_cache_legacy_record_loads_without_provenance():
    """Старый cache без provenance-полей успешно загружается (back-compat)."""
    from workspace.skills.legal_summarizer.scripts.document_cache import (
        doc_cache_dir,
        load_doc_cache,
    )
    import tempfile, json
    from pathlib import Path

    workspace_root = Path(tempfile.mkdtemp())
    document_id = "test_legacy_doc"
    session_key = "test_session_legacy"

    cache = doc_cache_dir(document_id, session_key, workspace_root)
    chunks_dir = cache / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    legacy_record = {
        "chunk_id": "001",
        "summary": "Краткое саммари без provenance.",
        "section_id": "s_root",
        "section_path": "",
        "page_start": 1,
        "page_end": 1,
        "saved_at": "2026-09-01T00:00:00Z",
    }
    (chunks_dir / "001.json").write_text(
        json.dumps(legacy_record, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    out = load_doc_cache(document_id, session_key, workspace_root)
    assert "001" in out
    assert out["001"]["summary"] == legacy_record["summary"]
    assert out["001"].get("block_indices") is None
    assert out["001"].get("source_char_start") is None
    assert out["001"].get("chunk_text_preview") is None


def test_doc_cache_meta_contains_physical_cache_key():
    """meta.json хранит physical_cache_key (sha256 файла) для freshness-check."""
    import tempfile
    from pathlib import Path
    from workspace.skills.legal_summarizer.scripts.document_cache import (
        load_doc_cache_meta,
        save_doc_cache,
    )

    workspace_root = Path(tempfile.mkdtemp())
    document_id = "test_prov_doc"
    session_key = "test_session_prov"

    src = workspace_root / "doc.txt"
    src.write_text("Тестовый юридический документ. " * 50, encoding="utf-8")

    save_doc_cache(
        document_id,
        session_key,
        workspace_root,
        {"001": {"chunk_id": "001", "summary": "...", "saved_at": "x"}},
        document_path=src,
    )

    meta = load_doc_cache_meta(document_id, session_key, workspace_root)
    assert meta is not None
    assert meta["document_id"] == document_id
    assert "physical_cache_key" in meta
    assert len(meta["physical_cache_key"]) == 16


def test_cache_is_fresh_when_unchanged():
    """Если файл не изменился, cache_is_fresh = True."""
    import tempfile
    from pathlib import Path
    from workspace.skills.legal_summarizer.scripts.document_cache import (
        cache_is_fresh,
        save_doc_cache,
    )

    workspace_root = Path(tempfile.mkdtemp())
    document_id = "test_fresh"
    session_key = "test_session_fresh"
    src = workspace_root / "doc.txt"
    src.write_text("Содержимое", encoding="utf-8")

    assert cache_is_fresh(document_id, session_key, workspace_root, src) is True

    save_doc_cache(
        document_id, session_key, workspace_root,
        {"001": {"chunk_id": "001", "summary": "...", "saved_at": "x"}},
        document_path=src,
    )
    assert cache_is_fresh(document_id, session_key, workspace_root, src) is True


def test_cache_stale_when_file_changes(tmp_path):
    """Если файл изменился, cache_is_fresh = False."""
    from workspace.skills.legal_summarizer.scripts.document_cache import (
        cache_is_fresh,
        save_doc_cache,
    )

    document_id = "test_stale"
    session_key = "test_session_stale"
    src = tmp_path / "doc.txt"
    src.write_text("Версия 1", encoding="utf-8")

    save_doc_cache(
        document_id, session_key, tmp_path,
        {"001": {"chunk_id": "001", "summary": "...", "saved_at": "x"}},
        document_path=src,
    )
    assert cache_is_fresh(document_id, session_key, tmp_path, src) is True

    src.write_text("Версия 2 с изменениями в содержании", encoding="utf-8")
    assert cache_is_fresh(document_id, session_key, tmp_path, src) is False


def test_cache_is_fresh_unknown_file_path_returns_true():
    """Если путь недоступен — не отклоняем cache (legacy / inline doc)."""
    import tempfile
    from pathlib import Path
    from workspace.skills.legal_summarizer.scripts.document_cache import (
        cache_is_fresh,
        save_doc_cache,
    )

    workspace_root = Path(tempfile.mkdtemp())
    save_doc_cache(
        "test_doc_nopath",
        "session_no_path",
        workspace_root,
        {"001": {"chunk_id": "001", "summary": "..."}},
    )
    assert cache_is_fresh(
        "test_doc_nopath", "session_no_path", workspace_root,
        Path("/this/does/not/exist.pdf"),
    ) is True


def test_save_doc_cache_persists_provenance_fields(tmp_path):
    """save_doc_cache сохраняет новые provenance-поля как обычные JSON-ключи."""
    from workspace.skills.legal_summarizer.scripts.document_cache import (
        load_doc_cache,
        save_doc_cache,
    )

    document_id = "test_prov_full"
    session_key = "test_session_full"
    record = {
        "chunk_id": "017",
        "summary": "Саммари chunk'а с полной provenance.",
        "section_id": "s_0007",
        "section_path": "7.3",
        "page_start": 42,
        "page_end": 45,
        "block_indices": [10, 11, 12],
        "block_types": ["paragraph", "paragraph", "paragraph"],
        "source_char_start": 1000,
        "source_char_end": 1850,
        "table_id": None,
        "table_row_start": None,
        "table_row_end": None,
        "chunk_text_preview": "Это начало превью для lexical match в follow-up.",
        "saved_at": "2026-09-02T10:00:00Z",
    }

    save_doc_cache(
        document_id, session_key, tmp_path, {"017": record},
        document_path=None,
    )

    loaded = load_doc_cache(document_id, session_key, tmp_path)
    assert "017" in loaded
    assert loaded["017"]["block_indices"] == [10, 11, 12]
    assert loaded["017"]["source_char_start"] == 1000
    assert loaded["017"]["source_char_end"] == 1850
    assert loaded["017"]["chunk_text_preview"] == record["chunk_text_preview"]


# ---------------------------------------------------------------------------
# Phase 5: cached candidate selection (lexical scoring + confidence)
# ---------------------------------------------------------------------------


def test_cached_question_finds_relevant_chunk():
    """Релевантный вопрос находит нужный chunk по lexical match."""
    from workspace.skills.legal_summarizer.scripts.cached_retrieval import (
        select_cached_candidates,
    )

    cache = {
        "001": _make_cache_record(
            "001",
            summary="Определение сторон договора",
            preview="Стороны договора определяют заказчик и подрядчик.",
            block_indices=(1,),
        ),
        "002": _make_cache_record(
            "002",
            summary="Условия об уведомлениях при нарушении сроков",
            preview="При нарушении сроков стороны обязаны направить уведомление.",
            block_indices=(2,),
        ),
        "003": _make_cache_record(
            "003",
            summary="Штрафные санкции",
            preview="За просрочку предусмотрена неустойка 0.1% в день.",
            block_indices=(3,),
        ),
    }

    candidates = select_cached_candidates(
        "Какие штрафы предусмотрены за просрочку?",
        cache,
    )
    assert candidates is not None
    assert len(candidates) >= 1
    assert candidates[0].chunk_id == "003"
    assert candidates[0].score >= 3


def test_cached_question_weak_match_returns_none():
    """Слабое совпадение (1 матч) → возвращаем None для existing fallback."""
    from workspace.skills.legal_summarizer.scripts.cached_retrieval import (
        select_cached_candidates,
    )

    cache = {
        "001": _make_cache_record(
            "001",
            summary="Определение договора",
            preview="Договор есть соглашение двух сторон.",
            block_indices=(0,),
        ),
        "002": _make_cache_record(
            "002",
            summary="Условия",
            preview="Условия определяются по соглашению сторон.",
            block_indices=(1,),
        ),
    }

    candidates = select_cached_candidates(
        "Что такое договор?", cache,
        min_score=2, min_top_score=3,
    )
    assert candidates is None


def test_cached_question_no_match_returns_none():
    """Нет ни одного матча → возвращаем None для fallback."""
    from workspace.skills.legal_summarizer.scripts.cached_retrieval import (
        select_cached_candidates,
    )

    cache = {
        "001": _make_cache_record(
            "001",
            summary="Полностью о другом",
            preview="Котики в офисе создают позитивную атмосферу.",
            block_indices=(0,),
        ),
    }
    candidates = select_cached_candidates(
        "Какая ответственность предусмотрена?", cache,
    )
    assert candidates is None


def test_cached_question_respects_max_candidates():
    """max_candidates ограничивает размер выборки."""
    from workspace.skills.legal_summarizer.scripts.cached_retrieval import (
        select_cached_candidates,
    )

    cache = {}
    for i in range(10):
        cache[f"{i:03d}"] = _make_cache_record(
            f"{i:03d}",
            summary=f"Штраф за просрочку платежа номер {i}.",
            preview=f"Штрафные санкции при просрочке описаны в пункте {i}.",
            block_indices=(i,),
        )

    candidates = select_cached_candidates(
        "Какие штрафы за просрочку?", cache,
        max_candidates=3, min_score=2, min_top_score=2,
    )
    assert candidates is not None
    assert len(candidates) == 3


def test_cached_question_sorts_by_score_then_chunk_id():
    """Сортировка: score DESC, chunk_id ASC (document order)."""
    from workspace.skills.legal_summarizer.scripts.cached_retrieval import (
        select_cached_candidates,
    )

    cache = {
        "001": _make_cache_record(
            "001",
            summary="Договор",
            preview="Договорные отношения регулируются ГК РФ.",
            block_indices=(0,),
        ),
        "002": _make_cache_record(
            "002",
            summary="Штраф за просрочку договора подряда",
            preview="Штрафы за просрочку договорных обязательств.",
            block_indices=(1,),
        ),
        "003": _make_cache_record(
            "003",
            summary="Штраф за просрочку",
            preview="Штрафные санкции.",
            block_indices=(2,),
        ),
    }

    candidates = select_cached_candidates(
        "Какие штрафы за просрочку?", cache,
        min_score=1, min_top_score=1,
    )
    assert candidates is not None
    assert candidates[0].chunk_id == "002"
    scores_seen = [c.score for c in candidates]
    assert scores_seen == sorted(scores_seen, reverse=True)


def test_cached_question_does_not_read_canonical_source():
    """cached_retrieval.py не импортирует canonical-source модули."""
    import workspace.skills.legal_summarizer.scripts.cached_retrieval as cr

    src = open(cr.__file__, encoding="utf-8").read()
    assert "DocumentBlock" not in src
    assert "load_physical_document" not in src


def test_cached_question_works_with_legacy_records():
    """Legacy cache без chunk_text_preview ломается мягко (только summary)."""
    from workspace.skills.legal_summarizer.scripts.cached_retrieval import (
        select_cached_candidates,
    )

    cache = {
        "001": {
            "chunk_id": "001",
            "summary": "Штраф за просрочку договора подряда",
            "section_id": "s_0001",
        },
    }
    candidates = select_cached_candidates(
        "Какие штрафы за просрочку?", cache,
        min_score=2, min_top_score=2,
    )
    assert candidates is not None
    assert candidates[0].chunk_id == "001"
    assert candidates[0].chunk_text_preview == ""


def test_is_confident_helper():
    """is_confident() — отдельный helper для confidence threshold."""
    from workspace.skills.legal_summarizer.scripts.cached_retrieval import (
        CachedCandidate,
        is_confident,
    )

    high = CachedCandidate(
        chunk_id="001", score=5, summary="",
        section_id=None, section_path=None,
        page_start=None, page_end=None,
        block_indices=(), block_types=(),
        source_char_start=None, source_char_end=None,
        table_id=None, table_row_start=None, table_row_end=None,
        chunk_text_preview="",
    )
    low = CachedCandidate(
        chunk_id="002", score=2, summary="",
        section_id=None, section_path=None,
        page_start=None, page_end=None,
        block_indices=(), block_types=(),
        source_char_start=None, source_char_end=None,
        table_id=None, table_row_start=None, table_row_end=None,
        chunk_text_preview="",
    )
    assert is_confident([high]) is True
    assert is_confident([high, low], min_score=3) is True
    assert is_confident([low], min_score=3) is False
    assert is_confident(None) is False
    assert is_confident([]) is False


# ---------------------------------------------------------------------------
# Phase 6: provenance-aware reconstruction
# ---------------------------------------------------------------------------


def test_reconstruct_whole_block_via_candidate():
    """Candidate с одним block + None offsets → block.content."""
    from workspace.skills.legal_summarizer.scripts.provenance_reconstruction import (
        reconstruct_candidate_source,
    )

    full = "Полный текст юридического абзаца про штрафы."
    block = _make_provenance_block(0, full, page_index=42)
    doc = _make_provenance_doc((block,))
    candidate = _make_cached_candidate("001", block_indices=(0,), preview=full[:20])

    text, is_stale = reconstruct_candidate_source(candidate, doc=doc, is_fresh=True)
    assert is_stale is False
    assert text == full


def test_reconstruct_split_chunk_via_candidate_exactly():
    """Candidate со split offsets → точная подстрока block.content."""
    from workspace.skills.legal_summarizer.scripts.provenance_reconstruction import (
        reconstruct_candidate_source,
    )

    full = "X" * 9000
    block = _make_provenance_block(0, full, page_index=42)
    doc = _make_provenance_doc((block,))

    candidate = _make_cached_candidate(
        "002",
        block_indices=(0,),
        source_char_start=3000,
        source_char_end=6000,
        preview=full[3000:6000],
    )
    text, is_stale = reconstruct_candidate_source(candidate, doc=doc, is_fresh=True)
    assert is_stale is False
    assert text == full[3000:6000]
    assert len(text) == 3000


def test_reconstruct_table_chunk_via_candidate():
    """Table candidate (table_id) → block.content атомарно."""
    from workspace.skills.legal_summarizer.scripts.provenance_reconstruction import (
        reconstruct_candidate_source,
    )

    table_text = "Заголовок | Кол1 | Кол2\nA | 1 | 2\nB | 3 | 4"
    block = _make_provenance_block(
        0, table_text, block_type="table", table_index=0, row_count=3,
    )
    doc = _make_provenance_doc((block,))
    candidate = _make_cached_candidate(
        "003",
        block_indices=(0,),
        table_id="t_0000",
        table_row_start=1,
        table_row_end=3,
        preview=table_text,
    )
    text, is_stale = reconstruct_candidate_source(candidate, doc=doc, is_fresh=True)
    assert is_stale is False
    assert text == table_text


def test_reconstruct_stale_returns_none():
    """Stale cache (is_fresh=False) → (None, True) → caller fallback."""
    from workspace.skills.legal_summarizer.scripts.provenance_reconstruction import (
        reconstruct_candidate_source,
    )

    block = _make_provenance_block(0, "полный текст")
    doc = _make_provenance_doc((block,))
    candidate = _make_cached_candidate("001")

    text, is_stale = reconstruct_candidate_source(candidate, doc=doc, is_fresh=False)
    assert text is None
    assert is_stale is True


def test_reconstruct_unknown_ordinal_returns_none():
    """Block ordinal из cache не найден в PhysicalDocument → None."""
    from workspace.skills.legal_summarizer.scripts.provenance_reconstruction import (
        reconstruct_candidate_source,
    )

    block = _make_provenance_block(0, "x")
    doc = _make_provenance_doc((block,))
    candidate = _make_cached_candidate("001", block_indices=(999,))

    text, is_stale = reconstruct_candidate_source(candidate, doc=doc, is_fresh=True)
    assert text is None
    assert is_stale is False


# ---------------------------------------------------------------------------
# Phase 7: context expansion
# ---------------------------------------------------------------------------


def test_expand_context_adds_one_prev_and_one_next():
    """Обычный body block: target + 1 prev + 1 next."""
    from workspace.skills.legal_summarizer.scripts.context_expansion import (
        expand_followup_context,
    )

    b0 = _make_provenance_block(0, "Первый абзац — это просто текст.")
    b1 = _make_provenance_block(1, "Целевой абзац про штрафы.")
    b2 = _make_provenance_block(2, "Следующий абзац после целевого.")
    doc = _make_provenance_doc((b0, b1, b2))

    out = expand_followup_context(
        target_ordinal=1,
        doc=doc,
        neighbor_count=1,
        max_total_chars=10000,
    )
    assert out.bounded is False
    indices = [idx for idx, _ in out.blocks]
    assert indices == [0, 1, 2]


def test_expand_context_no_prev_at_document_start():
    """target = первый блок документа → нет prev."""
    from workspace.skills.legal_summarizer.scripts.context_expansion import (
        expand_followup_context,
    )

    b0 = _make_provenance_block(0, "Целевой block в начале.")
    b1 = _make_provenance_block(1, "Следующий block.")
    doc = _make_provenance_doc((b0, b1))

    out = expand_followup_context(target_ordinal=0, doc=doc)
    indices = [idx for idx, _ in out.blocks]
    assert indices == [0, 1]


def test_expand_context_no_next_at_document_end():
    """target = последний блок → нет next."""
    from workspace.skills.legal_summarizer.scripts.context_expansion import (
        expand_followup_context,
    )

    b0 = _make_provenance_block(0, "Первый.")
    b1 = _make_provenance_block(1, "Последний — целевой.")
    doc = _make_provenance_doc((b0, b1))

    out = expand_followup_context(target_ordinal=1, doc=doc)
    indices = [idx for idx, _ in out.blocks]
    assert 0 in indices
    assert 1 in indices


def test_expand_context_skips_table_as_neighbor():
    """Neighbors не должны быть table-блоками."""
    from workspace.skills.legal_summarizer.scripts.context_expansion import (
        expand_followup_context,
    )

    body = _make_provenance_block(0, "Просто текст.")
    table = _make_provenance_block(
        1, "Заголовок | A | B\n1 | 2 | 3",
        block_type="table", table_index=0, row_count=2,
    )
    body2 = _make_provenance_block(2, "После таблицы.")
    doc = _make_provenance_doc((body, table, body2))

    out = expand_followup_context(
        target_ordinal=0, doc=doc, neighbor_count=2,
    )
    indices = [idx for idx, _ in out.blocks]
    assert indices == [0, 2]


def test_expand_context_bounded_by_max_chars():
    """Если суммарно > max_total_chars → bounded=True, не весь ок."""
    from workspace.skills.legal_summarizer.scripts.context_expansion import (
        expand_followup_context,
    )

    big_prev = _make_provenance_block(0, "A" * 5000)
    target = _make_provenance_block(1, "B" * 100)
    big_next = _make_provenance_block(2, "C" * 5000)
    doc = _make_provenance_doc((big_prev, target, big_next))

    out = expand_followup_context(
        target_ordinal=1,
        doc=doc,
        max_total_chars=1000,
    )
    assert out.bounded is True
    assert out.total_chars <= 1000


def test_expand_context_table_rule_includes_before_and_after():
    """Для table → prev (heading/before) + table + next (after)."""
    from workspace.skills.legal_summarizer.scripts.context_expansion import (
        expand_followup_context,
    )

    heading = _make_provenance_block(0, "5.3 Штрафы")
    table = _make_provenance_block(
        1, "Тип | Размер\nНеустойка | 0.1%",
        block_type="table", table_index=0, row_count=2,
    )
    after = _make_provenance_block(2, "Подробнее об условиях.")
    doc = _make_provenance_doc((heading, table, after))

    out = expand_followup_context(target_ordinal=1, doc=doc)
    indices = [idx for idx, _ in out.blocks]
    assert indices == [0, 1, 2]


def test_expand_context_heading_rule_no_prev_for_heading():
    """Heading: target + next paragraphs (без prev)."""
    from workspace.skills.legal_summarizer.scripts.context_expansion import (
        expand_followup_context,
    )
    from workspace.skills.legal_summarizer.scripts.structure.physical import (
        DocumentBlock,
    )

    heading = DocumentBlock(
        block_id="b_0001",
        block_type="paragraph",
        content="5. Раздел про санкции",
        char_count=27,
        page_index=1, page_start=1, page_end=1,
        paragraph_index=1, table_index=None, ordinal=1,
        block_metadata={"style": "Heading 1"},
    )
    body = _make_provenance_block(2, "Содержимое раздела.")
    body2 = _make_provenance_block(3, "Продолжение.")
    doc = _make_provenance_doc(
        (_make_provenance_block(0, "Просто предыдущий."), heading, body, body2)
    )

    out = expand_followup_context(target_ordinal=1, doc=doc)
    indices = [idx for idx, _ in out.blocks]
    assert 0 not in indices
    assert 1 in indices
    assert 2 in indices
    assert 3 in indices


def test_expand_context_raises_on_out_of_range_target():
    """target_ordinal не найден в doc.blocks → ValueError."""
    from workspace.skills.legal_summarizer.scripts.context_expansion import (
        expand_followup_context,
    )

    block = _make_provenance_block(0, "x")
    doc = _make_provenance_doc((block,))

    with pytest.raises(ValueError, match="вне диапазона"):
        expand_followup_context(target_ordinal=999, doc=doc)
    with pytest.raises(ValueError, match="вне диапазона"):
        expand_followup_context(target_ordinal=-1, doc=doc)


# ---------------------------------------------------------------------------
# Phase 8: cache-assisted follow-up retrieval integration
# ---------------------------------------------------------------------------


def test_retrieve_followup_context_via_cache_returns_chunks_for_fresh_cache():
    """confident + fresh cache → list[Chunk]."""
    from workspace.skills.legal_summarizer.scripts.cache_followup import (
        retrieve_followup_context_via_cache,
    )
    import tempfile
    from pathlib import Path
    from workspace.skills.legal_summarizer.scripts.document_cache import (
        save_doc_cache,
    )
    from workspace.skills.legal_summarizer.scripts.structure.physical import (
        DocumentBlock,
        PhysicalDocument,
    )

    workspace = Path(tempfile.mkdtemp())
    document_id = "test_int_followup"
    session_key = "session_int_followup"
    text_path = workspace / "doc.txt"
    text_path.write_text(
        "Первый блок про определения договора подряда. " * 20
        + "Второй блок про штрафы за просрочку договора подряда. " * 30
        + "Третий блок про порядок расторжения. " * 20,
        encoding="utf-8",
    )

    blocks = []
    for i, sentence in enumerate([
        "Первый блок про определения договора подряда. " * 20,
        "Второй блок про штрафы за просрочку договора подряда. " * 30,
        "Третий блок про порядок расторжения. " * 20,
    ]):
        blocks.append(_make_provenance_block(i, sentence, page_index=1))
    doc = PhysicalDocument(
        path=str(text_path), format="txt", title=None,
        size_bytes=text_path.stat().st_size, blocks=tuple(blocks), page_count=1,
    )

    cache_records = {
        "005": {
            "chunk_id": "005",
            "summary": "Штрафные санкции предусмотрены за просрочку договора подряда",
            "chunk_text_preview": (
                "Штрафные санкции предусмотрены за просрочку договорных "
                "обязательств в размере 0.1 процента в день"
            ),
            "section_id": "s_0002",
            "section_path": "5.3",
            "page_start": 1,
            "page_end": 1,
            "block_indices": [1],
            "block_types": ["paragraph"],
            "source_char_start": 0,
            "source_char_end": len(blocks[1].content),
            "table_id": None,
            "table_row_start": None,
            "table_row_end": None,
            "saved_at": "2026-09-03T00:00:00Z",
        },
    }
    save_doc_cache(
        document_id, session_key, workspace, cache_records,
        document_path=text_path,
    )

    chunks = retrieve_followup_context_via_cache(
        question="Какие штрафы предусмотрены за просрочку?",
        document_id=document_id,
        session_key=session_key,
        document_path=str(text_path),
        workspace_root=workspace,
        doc=doc,
    )
    assert chunks is not None
    assert len(chunks) == 1
    assert 1 in chunks[0].block_indices


def test_retrieve_followup_context_returns_none_without_session():
    """Без session_key → None → existing fallback."""
    from workspace.skills.legal_summarizer.scripts.cache_followup import (
        retrieve_followup_context_via_cache,
    )
    from workspace.skills.legal_summarizer.scripts.structure.physical import (
        PhysicalDocument,
    )

    block = _make_provenance_block(0, "x")
    doc = PhysicalDocument(
        path="<inline>", format="txt", title=None, size_bytes=1,
        blocks=(block,), page_count=1,
    )
    chunks = retrieve_followup_context_via_cache(
        question="Что?", document_id="x", session_key=None,
        document_path=None, workspace_root=None, doc=doc,
    )
    assert chunks is None


def test_retrieve_followup_context_returns_none_for_stale_cache():
    """Stale cache (file changed) → None → existing fallback."""
    from workspace.skills.legal_summarizer.scripts.cache_followup import (
        retrieve_followup_context_via_cache,
    )
    from workspace.skills.legal_summarizer.scripts.document_cache import (
        save_doc_cache,
    )
    from workspace.skills.legal_summarizer.scripts.structure.physical import (
        PhysicalDocument,
    )
    import tempfile
    from pathlib import Path
    workspace = Path(tempfile.mkdtemp())
    doc_path = workspace / "doc.txt"
    doc_path.write_text("v1", encoding="utf-8")
    save_doc_cache(
        "test_stale", "session_stale", workspace,
        {"001": {"chunk_id": "001", "summary": "..."}},
        document_path=doc_path,
    )
    doc_path.write_text("v2 совершенно другой текст", encoding="utf-8")

    block = _make_provenance_block(0, "v1")
    doc = PhysicalDocument(
        path=str(doc_path), format="txt", title=None, size_bytes=10,
        blocks=(block,), page_count=1,
    )
    chunks = retrieve_followup_context_via_cache(
        question="v2?", document_id="test_stale", session_key="session_stale",
        document_path=str(doc_path), workspace_root=workspace, doc=doc,
    )
    assert chunks is None


def test_retrieve_followup_context_uses_full_source_text():
    """PR2 главный критерий: LLM получает ТОЧНЫЙ исходный текст."""
    from workspace.skills.legal_summarizer.scripts.cache_followup import (
        retrieve_followup_context_via_cache,
    )
    from workspace.skills.legal_summarizer.scripts.document_cache import (
        save_doc_cache,
    )
    from workspace.skills.legal_summarizer.scripts.structure.physical import (
        PhysicalDocument,
    )
    import tempfile
    from pathlib import Path
    workspace = Path(tempfile.mkdtemp())
    doc_path = workspace / "doc.txt"
    full_block_text = (
        "Уникальная_строка_AAA " * 50
        + "Штрафные санкции за просрочку договорных обязательств "
        + "Уникальная_строка_BBB " * 50
    )
    doc_path.write_text(full_block_text, encoding="utf-8")
    block = _make_provenance_block(0, full_block_text)
    doc = PhysicalDocument(
        path=str(doc_path), format="txt", title=None,
        size_bytes=len(full_block_text), blocks=(block,), page_count=1,
    )

    save_doc_cache(
        "test_full", "session_full", workspace,
        {
            "001": {
                "chunk_id": "001",
                "summary": "Штрафные санкции предусмотрены за просрочку договорных обязательств",
                "chunk_text_preview": (
                    "Штрафные санкции предусмотрены за просрочку договорных "
                    "обязательств в размере 0.1 процента в день"
                ),
                "block_indices": [0],
                "block_types": ["paragraph"],
                "source_char_start": 0,
                "source_char_end": len(full_block_text),
                "section_id": "s_0001",
            },
        },
        document_path=doc_path,
    )

    chunks = retrieve_followup_context_via_cache(
        question="Какие штрафы предусмотрены за просрочку?",
        document_id="test_full",
        session_key="session_full",
        document_path=str(doc_path),
        workspace_root=workspace,
        doc=doc,
    )
    assert chunks is not None
    assert "Уникальная_строка_AAA" in chunks[0].text
    assert "Уникальная_строка_BBB" in chunks[0].text
    assert "Штрафные санкции за просрочку" in chunks[0].text


# ---------------------------------------------------------------------------
# Phase 9: E2E second-run test (cache → reload PhysicalDocument → follow-up)
# ---------------------------------------------------------------------------


def test_e2e_second_run_followup_question_uses_cache_then_reloads_physical_document(
    tmp_path,
):
    """E2E: cache → reload PhysicalDocument → exact source → LLM-input.

    Lightweight integration: подтверждает, что cache (с provenance)
    корректно восстанавливается через PhysicalDocument после reload и
    возвращает text, содержащий exact source fragment. Без LLM.
    """
    from workspace.skills.legal_summarizer.scripts.cache_followup import (
        retrieve_followup_context_via_cache,
    )
    from workspace.skills.legal_summarizer.scripts.document_cache import (
        load_doc_cache,
        save_doc_cache,
    )
    from workspace.skills.legal_summarizer.scripts.structure.physical import (
        DocumentBlock,
        PhysicalDocument,
        load_physical_document,
    )

    workspace_root = tmp_path
    session_key = "e2e_session_pr2"
    document_id = "e2e_doc_pr2"
    doc_path = workspace_root / "data_store" / "cache" / "sessions" / session_key / "contract.txt"
    doc_path.parent.mkdir(parents=True, exist_ok=True)

    full_doc = (
        "1. Общие положения.\n"
        + "Стороны договора определяют заказчик и подрядчик. " * 30 + "\n\n"
        + "2. Права и обязанности.\n"
        + "Заказчик вправе требовать от подрядчика надлежащего исполнения. " * 30 + "\n\n"
        + "3. Ответственность сторон.\n"
        + "При нарушении сроков подрядчик уплачивает заказчику штраф в размере "
        + "0.1 процента от суммы договора за каждый день просрочки. " * 30 + "\n\n"
        + "4. Заключительные положения.\n"
        + "Споры разрешаются по соглашению сторон, а при недостижении — в суде. " * 30
    )
    doc_path.write_text(full_doc, encoding="utf-8")

    target_text = full_doc
    run1_block = DocumentBlock(
        block_id="b_0000", block_type="text", content=target_text,
        char_count=len(target_text), page_index=1, page_start=1, page_end=1,
        paragraph_index=None, table_index=None, ordinal=0,
        block_metadata={},
    )
    doc_run1 = PhysicalDocument(
        path=str(doc_path), format="txt", title=None,
        size_bytes=len(full_doc), blocks=(run1_block,), page_count=1,
    )
    save_doc_cache(
        document_id, session_key, workspace_root,
        {
            "002": {
                "chunk_id": "002",
                "summary": "Штрафные санкции предусмотрены за просрочку договора подряда",
                "chunk_text_preview": (
                    "Штрафные санкции предусмотрены за просрочку. "
                    "Подрядчик уплачивает заказчику штраф в размере "
                    "0.1 процента за каждый день просрочки."
                ),
                "section_id": "s_0002",
                "section_path": "3",
                "page_start": 1,
                "page_end": 1,
                "block_indices": [0],
                "block_types": ["paragraph"],
                "source_char_start": 0,
                "source_char_end": len(target_text),
                "table_id": None,
                "table_row_start": None,
                "table_row_end": None,
                "saved_at": "2026-09-03T00:00:00Z",
            },
        },
        document_path=doc_path,
    )

    doc_run2 = load_physical_document(
        str(doc_path), workspace_root=workspace_root,
    )
    assert doc_run2.path == doc_run1.path
    assert len(doc_run2.blocks) == len(doc_run1.blocks)
    assert "0.1 процента" in doc_run2.blocks[0].content
    assert doc_run2.blocks[0].ordinal == 0

    cache_run2 = load_doc_cache(document_id, session_key, workspace_root)
    assert "002" in cache_run2
    assert cache_run2["002"]["block_indices"] == [0]

    chunks = retrieve_followup_context_via_cache(
        question="Какие штрафы предусмотрены за просрочку?",
        document_id=document_id,
        session_key=session_key,
        document_path=str(doc_path),
        workspace_root=workspace_root,
        doc=doc_run2,
    )
    assert chunks is not None
    assert len(chunks) >= 1

    chunk = chunks[0]
    assert "0.1 процента" in chunk.text
    assert len(chunk.text) > len(cache_run2["002"]["chunk_text_preview"])
    assert 0 in chunk.block_indices


# ---------------------------------------------------------------------------
# Phase 9 (refined): true E2E через inspect() — без fake LLM, через
# save_doc_cache точно так же, как это делает summarizer.run(). Подтверждает
# полный runtime contract: inspect → save provenance → reload → retrieve.
# ---------------------------------------------------------------------------


def test_e2e_via_inspect_then_reload_then_followup(tmp_path):
    """E2E через ``summarizer.inspect``: первый проход использует inspect для
    генерации chunks с provenance, имитирует summarizer.run() сохраняя
    cache (как делает run), затем второй проход загружает PhysicalDocument
    с диска и обращается через ``retrieve_followup_context_via_cache``.
    """
    from workspace.skills.legal_summarizer.scripts import summarizer
    from workspace.skills.legal_summarizer.scripts.cache_followup import (
        retrieve_followup_context_via_cache,
    )
    from workspace.skills.legal_summarizer.scripts.document_cache import (
        cache_is_fresh,
        load_doc_cache,
        save_doc_cache,
    )
    from workspace.skills.legal_summarizer.scripts.fingerprint import (
        fingerprint_file,
    )
    from workspace.skills.legal_summarizer.scripts.structure.physical import (
        load_physical_document,
    )

    workspace_root = tmp_path
    session_key = "e2e_via_inspect"
    document_id = "e2e_via_inspect_doc"
    doc_path = workspace_root / "data_store" / "cache" / "sessions" / session_key / "contract.txt"
    doc_path.parent.mkdir(parents=True, exist_ok=True)

    section_3_marker = (
        "3. Ответственность сторон.\n"
        "При нарушении сроков подрядчик уплачивает заказчику штраф в "
        "размере 0.1 процента от суммы договора за каждый день просрочки."
    )
    full_doc = (
        "1. Общие положения.\n"
        + ("Стороны договора определяют заказчик и подрядчик. " * 5) + "\n\n"
        + "2. Права и обязанности.\n"
        + ("Заказчик вправе требовать от подрядчика надлежащего исполнения. " * 5) + "\n\n"
        + section_3_marker + "\n\n"
        + "4. Заключительные положения.\n"
        + ("Споры разрешаются по соглашению сторон. " * 5)
    )
    doc_path.write_text(full_doc, encoding="utf-8")

    # === RUN #1 ===
    insp = summarizer.inspect(full_doc, document_path=str(doc_path))
    assert insp.strategy in {"map_reduce", "single"}
    assert len(insp.chunks) >= 1

    selected = insp.chunks[0]

    _PREVIEW_MAX = 500
    preview_src = selected.text
    if len(preview_src) > _PREVIEW_MAX:
        preview = preview_src[:_PREVIEW_MAX]
    else:
        preview = preview_src
    payload: dict = {
        "chunk_id": selected.chunk_id,
        "summary": (
            "Штрафные санкции предусмотрены за просрочку договора подряда"
        ),
        "chunk_text_preview": preview,
        "section_id": selected.section_id,
        "section_path": selected.section_path,
        "page_start": selected.page_start,
        "page_end": selected.page_end,
        "block_indices": list(selected.block_indices),
        "block_types": list(selected.block_types),
        "source_char_start": selected.source_char_start,
        "source_char_end": selected.source_char_end,
        "table_id": selected.table_id,
        "table_row_start": selected.table_row_start,
        "table_row_end": selected.table_row_end,
        "saved_at": "2026-09-03T00:00:00Z",
    }
    save_doc_cache(
        document_id, session_key, workspace_root,
        {selected.chunk_id: payload},
        document_path=doc_path,
    )

    assert cache_is_fresh(document_id, session_key, workspace_root, doc_path)

    # === RESTART (новый runtime, новые объекты) ===
    doc_run2 = load_physical_document(
        str(doc_path), workspace_root=workspace_root,
    )
    assert fingerprint_file(doc_path)

    cache_run2 = load_doc_cache(document_id, session_key, workspace_root)
    assert selected.chunk_id in cache_run2

    chunks = retrieve_followup_context_via_cache(
        question="Какие штрафы предусмотрены за просрочку договора подряда?",
        document_id=document_id,
        session_key=session_key,
        document_path=str(doc_path),
        workspace_root=workspace_root,
        doc=doc_run2,
    )
    assert chunks is not None
    assert len(chunks) >= 1

    chunk = chunks[0]
    # Provenance target сохранён после context expansion
    assert chunk.target_block_indices is not None
    assert chunk.target_source_char_start == selected.source_char_start
    assert chunk.target_source_char_end == selected.source_char_end
    # Source spans присутствуют и помечают primary target
    assert any(
        span[3] == 1 for span in chunk.source_spans
    ), "expected at least one source span marked as target"

    # === STALE FALLBACK: при изменении файла cache становится stale ===
    doc_path.write_text(
        full_doc + "\n\nНовая секция после модификации файла.",
        encoding="utf-8",
    )
    assert not cache_is_fresh(document_id, session_key, workspace_root, doc_path)

    doc_run3 = load_physical_document(
        str(doc_path), workspace_root=workspace_root,
    )
    chunks_stale = retrieve_followup_context_via_cache(
        question="Какие штрафы предусмотрены за просрочку договора подряда?",
        document_id=document_id,
        session_key=session_key,
        document_path=str(doc_path),
        workspace_root=workspace_root,
        doc=doc_run3,
    )
    assert chunks_stale is None, "stale cache must return None → existing fallback"


# ---------------------------------------------------------------------------
# Project config: brief_truncate_chars_per_block
# ---------------------------------------------------------------------------


def test_skill_config_chunking_includes_brief_truncate():
    """lib.core.skill_config.get_chunking_config читает brief_truncate_chars_per_block."""
    from lib.core.skill_config import get_chunking_config

    cfg = get_chunking_config("legal_summarizer")
    assert "brief_truncate_chars_per_block" in cfg
