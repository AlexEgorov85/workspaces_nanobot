"""Regression для heading classifier на реалистичных пунктах НК РФ.

Этот тест воспроизводит **реальный сценарий НК РФ**:

* статья = короткий heading (Статья N. Title);
* пункты статьи = **отдельные короткие blocks** ``1. ...`` / ``2. ...`` /
  ``3. ...`` (~30-80 chars каждый);
* между ними — substantial body blocks (≥ 100 chars), которые
  разрывают list-run в ``detect_list_runs``.

При такой структуре:

* ``_RE_NUMBERED_LEVEL_1`` (``{2,200}``) **матчит** короткие ``1.``;
* ``detect_list_runs`` находит run, но ``is_list=False``, потому что
  между нумерованными блоками есть body blocks (contiguity check);
* ``list_penalty_for_candidate`` возвращает **только** 0.08 (ambiguous);
* итоговый score: ``0.65 + 0.05 (short) + 0.05 (body_after) − 0.08 = 0.67``
  → проходит ``CONFIDENCE_THRESHOLD=0.60``;
* каждый ``1.`` становится section.

Результат: 10 статей × 3 пунктов = 30 sections + 10 статей = 40 headings
вместо 10. На реальном НК РФ (199 blocks) это даёт «199 chunks».

После фикса этот тест должен проходить: голые ``1. ...``
в runs, разорванных body, **не должны** проходить как headings.
"""
from __future__ import annotations

from pathlib import Path
import sys

_SKILL_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
_PROJECT_ROOT = _SKILL_ROOT.parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


from document.heading import (
    detect_heading_candidates,
    apply_confidence_penalties,
    apply_evidence_scoring,
    filter_above_threshold,
)
from document.physical import DocumentBlock
from document.list_detection import detect_list_runs


def _b(ordinal: int, content: str, block_type: str = "paragraph") -> DocumentBlock:
    return DocumentBlock(
        block_id=f"b_{ordinal:04d}",
        block_type=block_type,
        content=content,
        char_count=len(content),
        page_index=None,
        page_start=None,
        page_end=None,
        paragraph_index=None,
        table_index=None,
        ordinal=ordinal,
        block_metadata={},
    )


def _build_nk_realistic(num_articles: int = 10, punkts_per_article: int = 3) -> tuple[DocumentBlock, ...]:
    """Реалистичный НК РФ: статьи + короткие пункты + substantial body.

    Структура одной статьи (для ``punkts_per_article=3``):
        block i:     "Статья N. Заголовок статьи номер N."
        block i+1:   "1. Содержание пункта 1 статьи N. ..."
        block i+2:   "Подробное описание пункта 1 ..."  (substantial body, ~1K chars)
        block i+3:   "2. Содержание пункта 2 статьи N. ..."
        block i+4:   "Подробное описание пункта 2 ..."
        block i+5:   "3. Содержание пункта 3 статьи N. ..."
        block i+6:   "Подробное описание пункта 3 ..."
    """
    body_text = (
        "Подробное описание пункта статьи нормативного правового акта, "
        "содержащее существенный объём юридического текста, "
        "необходимый для отражения реального размера абзаца. "
    )
    body_text = body_text * 5

    blocks: list[DocumentBlock] = []
    for art in range(1, num_articles + 1):
        blocks.append(_b(
            len(blocks),
            f"Статья {art}. Заголовок статьи номер {art}.",
        ))
        for p in range(1, punkts_per_article + 1):
            blocks.append(_b(
                len(blocks),
                f"{p}. Содержание пункта {p} статьи {art}.",
            ))
            blocks.append(_b(len(blocks), body_text))
    return tuple(blocks)


def _accepted(blocks: tuple[DocumentBlock, ...]):
    raw = detect_heading_candidates(blocks, pdf_path=None)
    penalized = apply_confidence_penalties(raw, blocks)
    scored = apply_evidence_scoring(penalized, blocks)
    return filter_above_threshold(scored)


def test_nk_realistic_only_articles_accepted():
    """Только 'Статья N.' — headings, голые '1./2./3.' — НЕ headings.

    На документе с 10 статьями и 3 пунктами в каждой = 70 blocks:
    - 10 должны быть headings (Статья 1..10)
    - 30 нумерованных пунктов НЕ должны быть headings
    - 30 body блоков никогда не были headings
    """
    blocks = _build_nk_realistic(num_articles=10, punkts_per_article=3)
    accepted = _accepted(blocks)
    accepted_ordinals = {c.block_index for c in accepted}

    punkt_ordinals: set[int] = set()
    for art in range(1, 11):
        base = (art - 1) * 7
        for p in range(1, 4):
            punkt_ordinals.add(base + (p - 1) * 2 + 1)

    bad = punkt_ordinals & accepted_ordinals
    assert not bad, (
        f"blocks {sorted(bad)} — голые нумерованные пункты, "
        f"ошибочно приняты как headings. "
        f"Источники: {[(c.block_index, c.source) for c in accepted if c.block_index in bad]}"
    )


def test_nk_realistic_correct_count():
    """Должно быть ровно num_articles (10) headings."""
    blocks = _build_nk_realistic(num_articles=10, punkts_per_article=3)
    accepted = _accepted(blocks)
    assert len(accepted) == 10, (
        f"Ожидалось 10 headings (10 статей); получено {len(accepted)}: "
        f"{[(c.block_index, c.source, c.text[:40]) for c in accepted]}"
    )


def test_nk_realistic_density_under_threshold():
    """section_density = sections / total_blocks должна быть < 30%.

    Защита от регрессии: даже если одна edge-case проскочит, density
    > 50% явно указывает на over-fragmentation.
    """
    blocks = _build_nk_realistic(num_articles=10, punkts_per_article=3)
    accepted = _accepted(blocks)
    density = len(accepted) / len(blocks)
    assert density < 0.30, (
        f"section_density={density:.2%} слишком высокая "
        f"({len(accepted)} sections / {len(blocks)} blocks); "
        f"на реалистичном НК РФ это приводит к ~199 chunks"
    )


def test_short_section_heading_still_accepted():
    """Короткий heading '1. Заголовок раздела' — настоящий heading,
    проходит благодаря evidence bonuses.

    Здесь heading: 1) короткий, 2) имеет body_after ≥ 100 chars,
    3) есть нумерационная consistency (один heading без numbered neighbors).
    """
    blocks = tuple(
        _b(i, text)
        for i, text in enumerate([
            "1. Общие положения раздела нормативного акта",
            "Развёрнутое содержание раздела нормального объёма, "
            "чтобы body_after работал как evidence для heading-кандидата.",
            "Дополнительный абзац раздела.",
        ])
    )
    accepted = _accepted(blocks)
    accepted_ordinals = {c.block_index for c in accepted}
    assert 0 in accepted_ordinals, (
        f"'1. Общие положения' — короткий heading с body_after, должен "
        f"приниматься. Получили: {sorted(accepted_ordinals)}"
    )


def test_detect_list_runs_produces_ambiguous_for_realistic_nk():
    """Sanity-check: на реалистичном НК РФ ``detect_list_runs`` даёт
    ambiguous run (is_list=False), а не is_list=True.

    Это документирует, почему ``list_penalty_for_candidate`` не помогает:
    на разорванных body runs возвращается только 0.08 penalty, и этого
    недостаточно чтобы сбить score 0.65 ниже 0.60.
    """
    blocks = _build_nk_realistic(num_articles=5, punkts_per_article=3)
    runs = detect_list_runs(blocks)
    assert runs, "Должен быть хотя бы один run нумерованных блоков"
    is_list_runs = [r for r in runs if r.is_list]
    assert not is_list_runs, (
        f"На реалистичном НК РФ list-runs разорваны body, "
        f"is_list должно быть False для всех; "
        f"получили is_list runs: {[(r.block_ordinals, r.numbers) for r in is_list_runs]}"
    )
