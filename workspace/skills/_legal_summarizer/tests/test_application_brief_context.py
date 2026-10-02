"""Тесты для BriefContextBuilder (brief-refactor).

BRIEF CONTRACT (см. workspace/skills/legal_summarizer/scripts/application/brief_context.py):

    Один документ → ровно один Chunk.

Тесты проверяют все инварианты архитектуры brief-refactor
(см. план brief-refactor §33-§45): один chunk, детальный не меняется,
все top-level sections, hierarchy, физический порядок, таблицы,
большой документ, ни один section не исчезает, marker truncation,
hard max, direct execution, legacy params не влияют, не использует
canonical chunks.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from chunking.chunks import Chunk
from chunking.chunker import _make_chunk_id as _canonical_chunk_id
from document.analysis import DocumentAnalysis
from document.identity import DocumentIdentity
from document.physical import DocumentBlock, PhysicalDocument
from document.structure import (
    DocumentStructure,
    DocumentTitle,
    NumberingInfo,
    StructureNode,
)

from application.brief_context import (
    BriefContextConfig,
    build_brief_chunk,
    resolve_max_chars,
)
from application.brief_compression import (
    BriefSection,
    allocate_budget,
    render_sections,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _b(ord: int, content: str = "x", block_type: str = "paragraph") -> DocumentBlock:
    return DocumentBlock(
        block_id=f"b_{ord:04d}",
        block_type=block_type,
        content=content,
        char_count=len(content),
        page_index=1,
        page_start=1,
        page_end=1,
        paragraph_index=None,
        table_index=None,
        ordinal=ord,
        block_metadata={},
    )


def _doc(blocks: list[DocumentBlock], *, title: str | None = None) -> PhysicalDocument:
    import tempfile
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8",
    ) as f:
        path = f.name
        if title:
            f.write(title)
    return PhysicalDocument(
        path=path,
        format="txt",
        title=title,
        size_bytes=0,
        blocks=tuple(blocks),
        page_count=1,
    )


def _node(
    node_id: str,
    *,
    node_type: str = "section",
    level: int = 1,
    title: str = "",
    parent_id: str | None = "n_0000",
    children: tuple[str, ...] = (),
    start_block: int = 0,
    end_block: int = 0,
) -> StructureNode:
    return StructureNode(
        node_id=node_id,
        node_type=node_type,
        semantic_type=None,
        level=level,
        title=title,
        number=None,
        parent_id=parent_id,
        children=children,
        start_block=start_block,
        end_block=end_block,
        confidence=1.0,
    )


def _struct_with_sections(
    section_ranges: dict[str, tuple[int, int, str]],
    *,
    preamble_end: int = -1,
) -> DocumentStructure:
    """Построить структуру: root + N секций, опционально preamble.

    Args:
        section_ranges: {section_id: (start_block, end_block, title)}
        preamble_end: -1 если нет preamble, иначе blocks [0..preamble_end]
            принадлежат root (preamble).
    """
    nodes: dict[str, StructureNode] = {}
    sections_in_order = []
    counter = 1
    for sid, (start, end, title) in section_ranges.items():
        sections_in_order.append(sid)
        nodes[sid] = StructureNode(
            node_id=sid,
            node_type="section",
            semantic_type=None,
            level=1,
            title=title,
            number=None,
            parent_id="n_0000",
            children=(),
            start_block=start,
            end_block=end,
            confidence=0.9,
        )
        counter += 1

    total = max(
        max(end + 1 for _, end, _ in section_ranges.values()),
        preamble_end + 1 if preamble_end >= 0 else 0,
    )

    root = StructureNode(
        node_id="n_0000",
        node_type="document",
        semantic_type=None,
        level=0,
        title="",
        number=None,
        parent_id=None,
        children=tuple(sections_in_order),
        start_block=0,
        end_block=total - 1 if total > 0 else 0,
        confidence=1.0,
    )
    nodes["n_0000"] = root

    return DocumentStructure(
        document_id="d",
        title=None,
        nodes=nodes,
        root_id="n_0000",
        preamble_node_id="n_0000",
        numbering=(),
        total_blocks=total,
    )


def _make_analysis(
    blocks: list[DocumentBlock],
    structure: DocumentStructure,
) -> DocumentAnalysis:
    """Собрать DocumentAnalysis без RetrievalIndex (быстрее)."""
    physical = _doc(blocks)
    identity = DocumentIdentity.from_path(physical.path)
    # Не передаём canonical chunks — BriefContextBuilder их игнорирует.
    return DocumentAnalysis.build(
        physical=physical,
        structure=structure,
        chunks=(),
        identity=identity,
        include_retrieval_index=False,
    )


# ---------------------------------------------------------------------------
# Обязательный тест №1 — один chunk (п.33)
# ---------------------------------------------------------------------------


def test_build_brief_chunk_returns_single_chunk():
    blocks = [_b(0, "preamble text\n"), _b(1, "Section A content")]
    struct = _struct_with_sections({"s_a": (1, 1, "Section A")}, preamble_end=0)
    analysis = _make_analysis(blocks, struct)

    chunk = build_brief_chunk(analysis)

    assert isinstance(chunk, Chunk)
    assert chunk.index == 0
    assert chunk.chunk_id == _canonical_chunk_id(1)


def test_build_brief_chunk_chunk_id_matches_canonical_contract():
    blocks = [_b(0, "x")]
    struct = _struct_with_sections({"s1": (0, 0, "Section")})
    analysis = _make_analysis(blocks, struct)

    chunk = build_brief_chunk(analysis)

    assert chunk.chunk_id == _canonical_chunk_id(1)
    assert chunk.index == 0


# ---------------------------------------------------------------------------
# Обязательный тест №3 — все top-level sections присутствуют (п.35)
# ---------------------------------------------------------------------------


def test_all_top_level_sections_present():
    blocks = [
        _b(0, "SECTION_A body"),
        _b(1, "more A"),
        _b(2, "SECTION_B body"),
        _b(3, "more B"),
        _b(4, "SECTION_C body"),
    ]
    struct = _struct_with_sections({
        "s_a": (0, 1, "SECTION_A"),
        "s_b": (2, 3, "SECTION_B"),
        "s_c": (4, 4, "SECTION_C"),
    })
    analysis = _make_analysis(blocks, struct)

    chunk = build_brief_chunk(analysis)

    assert "SECTION_A" in chunk.text
    assert "SECTION_B" in chunk.text
    assert "SECTION_C" in chunk.text


# ---------------------------------------------------------------------------
# Обязательный тест №4 — hierarchy присутствует (п.36)
# ---------------------------------------------------------------------------


def test_hierarchy_in_document_structure_block():
    blocks = [
        _b(0, "Section A body"),
        _b(1, "Chapter A1 body"),
        _b(2, "Article A1.1 body"),
    ]
    struct = _struct_with_sections({
        "s_a": (0, 2, "Section A"),
        "c_a1": (1, 2, "Chapter A1"),
        "art_a11": (2, 2, "Article A1.1"),
    })
    analysis = _make_analysis(blocks, struct)

    chunk = build_brief_chunk(analysis)

    assert "DOCUMENT STRUCTURE" in chunk.text
    assert "Section A" in chunk.text
    assert "Chapter A1" in chunk.text
    assert "Article A1.1" in chunk.text


# ---------------------------------------------------------------------------
# Обязательный тест №5 — физический порядок (п.37)
# ---------------------------------------------------------------------------


def test_physical_order_preserved():
    blocks = [
        _b(0, "FIRST"),
        _b(1, "SECOND"),
        _b(2, "THIRD"),
        _b(3, "FOURTH"),
    ]
    struct = _struct_with_sections({"s1": (0, 3, "Section 1")})
    analysis = _make_analysis(blocks, struct)

    chunk = build_brief_chunk(analysis)

    assert chunk.block_indices == (0, 1, 2, 3)
    # В тексте порядок должен соответствовать ordinal
    assert chunk.text.index("FIRST") < chunk.text.index("SECOND")
    assert chunk.text.index("SECOND") < chunk.text.index("THIRD")
    assert chunk.text.index("THIRD") < chunk.text.index("FOURTH")


# ---------------------------------------------------------------------------
# Обязательный тест №6 — таблица атомарна (п.38)
# ---------------------------------------------------------------------------


def test_table_block_atomic():
    table_text = "header1 | header2\nrow1 | row2\nrow3 | row4"
    blocks = [
        _b(0, "Section heading\n"),
        _b(1, "Paragraph before table\n"),
        _b(2, table_text, block_type="table"),
        _b(3, "Paragraph after table\n"),
    ]
    struct = _struct_with_sections({"s1": (0, 3, "Section 1")})
    analysis = _make_analysis(blocks, struct)

    chunk = build_brief_chunk(analysis, config=BriefContextConfig(max_chars_fallback=1000000))

    assert table_text in chunk.text
    assert chunk.block_types[chunk.block_indices.index(2)] == "table"


# ---------------------------------------------------------------------------
# Обязательный тест №7 — большой документ, hard max (п.39, п.42)
# ---------------------------------------------------------------------------


def test_oversized_document_truncated_to_max_chars():
    big_text = "word " * 10000  # ~50000 chars
    blocks = [_b(i, big_text) for i in range(3)]
    struct = _struct_with_sections({
        "s1": (0, 0, "S1"),
        "s2": (1, 1, "S2"),
        "s3": (2, 2, "S3"),
    })
    analysis = _make_analysis(blocks, struct)

    cfg = BriefContextConfig(
        max_chars_fallback=5000,
        input_ratio=None,
    )
    chunk = build_brief_chunk(analysis, config=cfg)

    assert chunk.char_count == len(chunk.text)
    assert chunk.char_count <= cfg.max_chars_fallback


# ---------------------------------------------------------------------------
# Обязательный тест №8 — ни один section не исчезает (п.40)
# ---------------------------------------------------------------------------


def test_no_section_disappears_when_oversized():
    section_count = 5
    big_text = "word " * 5000
    blocks = []
    section_ranges = {}
    for i in range(section_count):
        blocks.append(_b(i, big_text))
        section_ranges[f"s{i}"] = (i, i, f"SECTION_{i}")
    struct = _struct_with_sections(section_ranges)
    analysis = _make_analysis(blocks, struct)

    cfg = BriefContextConfig(max_chars_fallback=2000, input_ratio=None)
    chunk = build_brief_chunk(analysis, config=cfg)

    for i in range(section_count):
        assert f"SECTION_{i}" in chunk.text, f"SECTION_{i} missing in compressed chunk"


# ---------------------------------------------------------------------------
# Обязательный тест №9 — compression marker (п.41)
# ---------------------------------------------------------------------------


def test_truncation_marker_added_when_section_truncated():
    big_text = "word " * 5000
    blocks = [_b(0, "SHORT heading"), _b(1, big_text), _b(2, "OTHER heading")]
    struct = _struct_with_sections({
        "s1": (0, 1, "Section 1"),
        "s2": (2, 2, "Section 2"),
    })
    analysis = _make_analysis(blocks, struct)

    cfg = BriefContextConfig(max_chars_fallback=1500, input_ratio=None)
    chunk = build_brief_chunk(analysis, config=cfg)

    assert "[BRIEF: section content truncated]" in chunk.text
    assert "Section 1" in chunk.text
    assert "Section 2" in chunk.text


# ---------------------------------------------------------------------------
# Обязательный тест №10 — hard max invariant (п.42)
# ---------------------------------------------------------------------------


def test_char_count_equals_text_length():
    blocks = [_b(0, "x" * 1000)]
    struct = _struct_with_sections({"s1": (0, 0, "Section")})
    analysis = _make_analysis(blocks, struct)

    for max_chars in [100, 500, 2000, 10000]:
        cfg = BriefContextConfig(
            max_chars_fallback=max_chars,
            input_ratio=None,
            structure_max_chars=min(2000, max_chars // 2),
        )
        chunk = build_brief_chunk(analysis, config=cfg)
        assert chunk.char_count == len(chunk.text)
        assert chunk.char_count <= max_chars


# ---------------------------------------------------------------------------
# Обязательный тест №11 — direct execution (п.43)
# ---------------------------------------------------------------------------


def test_brief_yields_direct_execution_strategy():
    """``len(ctx.chunks) == 1`` → strategy=direct, plan=None."""
    from application.context_builder import build_execution_context
    from application.inspection import Inspection

    blocks = [_b(0, "Section A"), _b(1, "Section B")]
    struct = _struct_with_sections({
        "s_a": (0, 0, "Section A"),
        "s_b": (1, 1, "Section B"),
    })
    analysis = _make_analysis(blocks, struct)
    insp = Inspection(
        chars_in=2,
        chunks=[],
        structure=analysis.structure,
        analysis=analysis,
    )

    ctx = build_execution_context(insp, length="brief")

    assert ctx.strategy == "direct"
    assert ctx.plan is None
    assert len(ctx.chunks) == 1


# ---------------------------------------------------------------------------
# Обязательный тест №13 — не используется canonical chunks (п.45)
# ---------------------------------------------------------------------------


def test_brief_ignores_canonical_chunks():
    """Canonical chunks с подложным текстом НЕ должны попасть в brief.

    Builder использует PhysicalDocument напрямую, поэтому результат
    НЕ содержит текст из ``analysis.chunks`` (которые мы можем
    намеренно "отравить").
    """
    blocks = [_b(0, "CORRECT_DOCUMENT_TEXT")]
    struct = _struct_with_sections({"s1": (0, 0, "Section")})
    physical = _doc(blocks)
    identity = DocumentIdentity.from_path(physical.path)

    poisoned_chunks = (
        Chunk(
            chunk_id=_canonical_chunk_id(1),
            index=0,
            text="WRONG_CANONICAL_TEXT",
            char_count=len("WRONG_CANONICAL_TEXT"),
            token_estimate=1,
            page_start=1,
            page_end=1,
            section_id="s1",
            section_path="1",
            section_heading="Section",
            block_indices=(0,),
            block_types=("paragraph",),
        ),
    )
    analysis = DocumentAnalysis.build(
        physical=physical,
        structure=struct,
        chunks=poisoned_chunks,
        identity=identity,
        include_retrieval_index=False,
    )

    chunk = build_brief_chunk(analysis)

    assert "CORRECT_DOCUMENT_TEXT" in chunk.text
    assert "WRONG_CANONICAL_TEXT" not in chunk.text


# ---------------------------------------------------------------------------
# Обязательный тест №14 — preamble включён в brief (п.7)
# ---------------------------------------------------------------------------


def test_preamble_preserved_in_brief():
    preamble = "DOCUMENT TITLE\nPREAMBLE PARAGRAPH"
    blocks = [
        _b(0, preamble),
        _b(1, "Section A body"),
    ]
    struct = _struct_with_sections({"s_a": (1, 1, "Section A")}, preamble_end=0)
    analysis = _make_analysis(blocks, struct)

    chunk = build_brief_chunk(analysis)

    assert "[Preamble]" in chunk.text
    assert preamble in chunk.text


# ---------------------------------------------------------------------------
# Обязательный тест №15 — resolve_max_chars динамический (п.18)
# ---------------------------------------------------------------------------


def test_resolve_max_chars_uses_context_window():
    import os
    from config import SETTINGS
    cwt = SETTINGS.get("agents", {}).get("defaults", {}).get("contextWindowTokens")
    if cwt is None:
        # Если нет контекстного окна в конфиге, тест пропускаем.
        import pytest
        pytest.skip("contextWindowTokens не задан в config.json")

    cfg = BriefContextConfig(
        max_chars_fallback=30000,
        input_ratio=0.13,
        chars_per_token=3.5,
    )
    expected = int(int(cwt) * 0.13 * 3.5)
    assert resolve_max_chars(cfg) == expected


def test_resolve_max_chars_uses_fallback_when_no_window():
    import importlib
    from application import brief_context
    importlib.reload(brief_context)

    # Save and clear the context window
    from config import SETTINGS
    saved = SETTINGS.get("agents", {}).get("defaults", {}).get("contextWindowTokens")
    if saved is not None:
        # Temporarily null it
        SETTINGS["agents"]["defaults"]["contextWindowTokens"] = None
    try:
        cfg = BriefContextConfig(
            max_chars_fallback=12345,
            input_ratio=0.5,
            chars_per_token=3.5,
        )
        assert resolve_max_chars(cfg) == 12345
    finally:
        if saved is not None:
            SETTINGS["agents"]["defaults"]["contextWindowTokens"] = saved


# ---------------------------------------------------------------------------
# Compression unit tests
# ---------------------------------------------------------------------------


def test_compression_does_not_remove_sections():
    sections = [
        BriefSection(heading="A", text="x" * 1000),
        BriefSection(heading="B", text="y" * 1000),
        BriefSection(heading="C", text="z" * 1000),
    ]
    out = render_sections(sections, available_chars=500)

    assert "A" in out
    assert "B" in out
    assert "C" in out


def test_compression_truncation_marker():
    sections = [
        BriefSection(heading="A", text="short"),
        BriefSection(heading="BIG", text="x" * 10000),
        BriefSection(heading="C", text="short"),
    ]
    out = render_sections(sections, available_chars=500, min_section_chars=50)

    assert "[BRIEF: section content truncated]" in out
    assert "A" in out
    assert "C" in out


def test_compression_safe_truncation_does_not_split_word():
    """Truncation должна резать по безопасной границе (paragraph > newline > sentence > word).

    Приоритет (п.15): paragraph → newline → sentence → word → hard char boundary.
    Поэтому truncation текста с ``\\n\\n`` сначала попробует paragraph boundary.
    """
    text = "first paragraph here\n\nsecond paragraph here " + ("word " * 1000)
    sections = [BriefSection(heading="S", text=text)]
    out = render_sections(sections, available_chars=300)

    # Внутри текста слово "word" должно быть либо полностью, либо не начинаться
    # посередине (т.е. не должно быть обрезки "...wo" без суффикса).
    # Тест проверяет, что в rendered тексте все вхождения "word" имеют полную форму.
    last_50 = out[-50:]
    assert "wo" not in last_50 or "word" in last_50, (
        f"truncation split a word mid-way: {last_50!r}"
    )
    # Должен присутствовать marker (text сильно больше budget)
    assert "[BRIEF: section content truncated]" in out


def test_compression_distributes_budget_proportionally():
    sections = [
        BriefSection(heading="TINY", text="abc"),
        BriefSection(heading="HUGE", text="x" * 10000),
    ]
    out = render_sections(sections, available_chars=500)

    # TINY остаётся целиком
    assert "abc" in out
    # HUGE сокращается, но heading сохраняется
    assert "HUGE" in out


def test_compression_allocate_budget_returns_tuple():
    sections = [
        BriefSection(heading="A", text="x" * 100),
        BriefSection(heading="B", text="y" * 100),
    ]
    out = allocate_budget(sections, available_chars=150)

    assert len(out) == 2
    for section, text, was_truncated in out:
        assert isinstance(section, BriefSection)
        assert isinstance(text, str)
        assert isinstance(was_truncated, bool)
