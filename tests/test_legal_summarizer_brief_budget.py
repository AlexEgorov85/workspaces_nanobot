"""Регрессия: brief_total_budget ограничивает LLM-input общими chars.

План: 4 бага legal_summarizer / шаг 4.

Двухуровневая модель:
  - coverage: select_brief_chunks_structured выбирает N chunks
    (round-robin по sections)
  - LLM budget: allocate_brief_budget распределяет общий лимит chars
    пропорционально между выбранными chunks

Тесты проверяют:
  A. Большие chunks (10 × 10K) → LLM-input ≤ budget (30K)
  B. Маленький документ → текст НЕ обрезается искусственно
  C. Coverage сохраняется: budget не заставляет выбирать только первую
     section (round-robin работает)
  D. Tables атомарны: budget не ломает их
  E. Обратная совместимость: если brief_max_input_chars отсутствует,
     legacy brief_max_chars_per_chunk продолжает работать.
  F. Backward compat: legacy при null budget → no-op.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parents[1] / "workspace" / "skills" / "legal_summarizer"
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


@dataclass(frozen=True)
class _FakeChunk:
    """Минимальный Chunk-stand-in для тестов allocate_brief_budget.

    Только те поля, которые нужны: ``text``/``char_count``/
    ``block_types``/``chunk_id``. ``replace(c, ...)`` создаёт новый
    объект — ``frozen=True`` + ``field(default_factory=...)`` нужно
    для hashable.
    """

    chunk_id: str
    index: int
    text: str
    char_count: int
    token_estimate: int = 0
    page_start: int | None = None
    page_end: int | None = None
    section_id: str = "ROOT"
    section_path: str = ""
    section_heading: str = ""
    block_indices: tuple = ()
    block_types: tuple = ("text",)
    source_char_start: int | None = None
    source_char_end: int | None = None
    table_id: str | None = None
    table_row_start: int | None = None
    table_row_end: int | None = None

    @classmethod
    def make(cls, *, chunk_id: str, text: str, block_types: tuple = ("text",)):
        return cls(
            chunk_id=chunk_id,
            index=int(chunk_id),
            text=text,
            char_count=len(text),
            block_types=block_types,
        )


from workspace.skills.legal_summarizer.scripts.brief_representation import (  # noqa: E402
    allocate_brief_budget,
    apply_brief_text_budget,
    total_input_chars,
)

# ---------------------------------------------------------------------------
# Test A — несколько больших chunks: суммарный ввод ≤ budget.
# ---------------------------------------------------------------------------


def test_large_chunks_total_input_within_budget():
    """10 chunks × 10K chars, budget=30K → LLM-input ≤ 30K chars
    (для текстовых chunks; см. инвариант budget применяется к тексту)."""
    chunks = [
        _FakeChunk.make(chunk_id=str(i), text="X" * 10_000)
        for i in range(10)
    ]
    out = allocate_brief_budget(chunks, total_budget_chars=30_000)

    total = total_input_chars(out)
    assert total <= 30_000, (
        f"budget violated: total_input_chars == {total}, "
        f"budget=30000"
    )
    # Все 10 chunks должны остаться (coverage сохраняется).
    assert len(out) == 10


def test_large_chunks_proportional_distribution():
    """Пропорциональное распределение: более длинные chunks получают
    больше chars (в пределах budget)."""
    chunks = [
        _FakeChunk.make(chunk_id="0", text="A" * 1000),
        _FakeChunk.make(chunk_id="1", text="B" * 4000),
        _FakeChunk.make(chunk_id="2", text="C" * 1000),
    ]
    out = allocate_brief_budget(chunks, total_budget_chars=3000)

    total = total_input_chars(out)
    assert total <= 3000

    # chunk 1 был самым длинным (4000 chars) → должен получить больше
    chars_by_id = {c.chunk_id: len(c.text) for c in out}
    assert chars_by_id["1"] >= chars_by_id["0"]
    assert chars_by_id["1"] >= chars_by_id["2"]


# ---------------------------------------------------------------------------
# Test B — маленький документ: текст НЕ обрезается искусственно.
# ---------------------------------------------------------------------------


def test_small_document_not_artificially_truncated():
    """Если суммарный объём текстовых chunks ≤ budget — обрезки нет,
    chunks возвращаются без изменений (но это могут быть те же объекты)."""
    chunks = [
        _FakeChunk.make(chunk_id="0", text="Короткий текст чанка 1."),
        _FakeChunk.make(chunk_id="1", text="Короткий текст чанка 2."),
        _FakeChunk.make(chunk_id="2", text="Короткий текст чанка 3."),
    ]
    out = allocate_brief_budget(chunks, total_budget_chars=100_000)

    chars_by_id = {c.chunk_id: c.text for c in out}
    assert chars_by_id["0"] == "Короткий текст чанка 1."
    assert chars_by_id["1"] == "Короткий текст чанка 2."
    assert chars_by_id["2"] == "Короткий текст чанка 3."


# ---------------------------------------------------------------------------
# Test C — coverage: budget не ломает round-robin selection.
# ---------------------------------------------------------------------------


def test_coverage_preserved_with_smaller_budget():
    """Budget ограничивает ТОЛЬКО объём текста, не coverage.
    Все выбранные chunks остаются в результате (10 chunks → 10 chunks)."""
    chunks = [
        _FakeChunk.make(chunk_id=str(i), text="Z" * 5000)
        for i in range(10)
    ]
    out = allocate_brief_budget(chunks, total_budget_chars=15_000)

    # Все 10 chunks сохранены.
    assert len(out) == 10
    chunk_ids = sorted(c.chunk_id for c in out)
    assert chunk_ids == sorted(str(i) for i in range(10))


def test_budget_does_not_drop_chunks():
    """Budget не должен удалять chunks (только обрезать text)."""
    chunks = [
        _FakeChunk.make(chunk_id="0", text="x" * 100_000),
        _FakeChunk.make(chunk_id="1", text="y" * 100_000),
    ]
    out = allocate_brief_budget(chunks, total_budget_chars=5000)

    assert len(out) == 2
    # Оба chunk'а имеют текст, обрезанный до budget.
    total = total_input_chars(out)
    assert total <= 5000


# ---------------------------------------------------------------------------
# Test D — Tables атомарны: budget их НЕ режет.
# ---------------------------------------------------------------------------


def test_tables_preserved_atomically():
    """Tables не должны обрезаться budget'ом — они атомарны (invariant §6)."""
    table_text = "T" * 5000
    chunks = [
        _FakeChunk.make(chunk_id="0", text="text chunk 1"),
        _FakeChunk.make(
            chunk_id="1", text=table_text, block_types=("table",),
        ),
        _FakeChunk.make(chunk_id="2", text="text chunk 3"),
    ]
    out = allocate_brief_budget(chunks, total_budget_chars=100)

    # Table chunk не обрезан.
    table_chunk = next(c for c in out if c.chunk_id == "1")
    assert table_chunk.text == table_text, (
        "Table chunk был обрезан — atomicity invariant §6 нарушен"
    )


# ---------------------------------------------------------------------------
# Test E — backward compatibility: legacy per-chunk budget работает.
# ---------------------------------------------------------------------------


def test_legacy_per_chunk_budget_still_works():
    """``apply_brief_text_budget`` (legacy per-chunk) продолжает работать
    при отсутствии нового brief_max_input_chars."""
    chunks = [
        _FakeChunk.make(chunk_id="0", text="A" * 1000),
        _FakeChunk.make(chunk_id="1", text="B" * 5000),
    ]
    out = apply_brief_text_budget(chunks, truncate_chars=2000)

    assert len(out) == 2
    chars_by_id = {c.chunk_id: len(c.text) for c in out}
    # chunk 0: 1000 chars < 2000 → без изменений.
    assert chars_by_id["0"] == 1000
    # chunk 1: 5000 chars > 2000 → обрезан до 2000 + " …".
    assert 2000 < chars_by_id["1"] <= 2002


def test_legacy_no_op_when_zero():
    """Legacy с truncate_chars=0 или None → no-op."""
    chunks = [
        _FakeChunk.make(chunk_id="0", text="A" * 1000),
    ]
    out_zero = apply_brief_text_budget(chunks, truncate_chars=0)
    out_none = apply_brief_text_budget(chunks, truncate_chars=None)
    assert len(out_zero) == 1
    assert out_zero[0].text == "A" * 1000
    assert len(out_none) == 1
    assert out_none[0].text == "A" * 1000


# ---------------------------------------------------------------------------
# Test F — allocate_brief_budget no-op при None/0.
# ---------------------------------------------------------------------------


def test_allocate_no_op_when_zero():
    """allocate_brief_budget с total_budget_chars=None или 0 — no-op."""
    chunks = [
        _FakeChunk.make(chunk_id="0", text="A" * 100_000),
        _FakeChunk.make(chunk_id="1", text="B" * 100_000),
    ]
    out_none = allocate_brief_budget(chunks, total_budget_chars=None)
    out_zero = allocate_brief_budget(chunks, total_budget_chars=0)
    out_neg = allocate_brief_budget(chunks, total_budget_chars=-100)

    for out in (out_none, out_zero, out_neg):
        assert len(out) == 2
        for c in out:
            # Текст НЕ обрезан.
            assert len(c.text) == 100_000


def test_allocate_empty_chunks():
    """Пустой список chunks → пустой результат (не crash)."""
    out = allocate_brief_budget([], total_budget_chars=30_000)
    assert out == []


# ---------------------------------------------------------------------------
# Test G — invariants: PhysicalDocument не мутируется.
# ---------------------------------------------------------------------------


def test_provenance_offsets_preserved():
    """Обрезка не меняет source_char_start/source_char_end (provenance).

    Мы не можем полно проверить PhysicalDocument через fake chunk, но
    ``dataclasses.replace`` гарантирует, что остальные поля неизменны.
    """
    c = _FakeChunk(
        chunk_id="0", index=0, text="x" * 10_000, char_count=10_000,
        source_char_start=100, source_char_end=10_100,
    )
    out = allocate_brief_budget([c], total_budget_chars=1000)
    new_c = out[0]
    # source_char_start/end неизменны (provenance для reconstruction).
    assert new_c.source_char_start == 100
    assert new_c.source_char_end == 10_100
    # text обрезан до <= 1000 chars (плюс возможный " …" суффикс).
    assert len(new_c.text) <= 1001


def test_min_per_chunk_no_degenerate_chunks():
    """Минимальный размер chunk'а после обрезки ≥ 200 chars
    (или вся длина, если chunk короче)."""
    chunks = [
        _FakeChunk.make(chunk_id=str(i), text="X" * 5000)
        for i in range(5)
    ]
    out = allocate_brief_budget(chunks, total_budget_chars=1000)

    for c in out:
        assert len(c.text) >= 200 or len(c.text) == 5000, (
            f"Chunk {c.chunk_id} вырождается в <200 chars: "
            f"len={len(c.text)}"
        )
