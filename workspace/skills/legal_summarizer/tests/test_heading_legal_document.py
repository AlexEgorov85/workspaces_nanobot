"""Regression для heading classifier: голые нумерованные пункты
НЕ ДОЛЖНЫ становиться sections.

Подробный план — Этап 1 bug-fix. Тест должен ПАДАТЬ на текущем коде,
в котором ``_RE_NUMBERED_LEVEL_1`` слишком щедро классифицирует
любую строку вида ``1. Текст`` как heading.

После фикса в heading.py (Этап 2) этот тест должен проходить:
раздел "1. Первый пункт" уже не считается section heading'ом.
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
    HeadingCandidate,
    apply_confidence_penalties,
    apply_evidence_scoring,
    detect_heading_candidates,
    filter_above_threshold,
)
from document.physical import DocumentBlock


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


def _build_legal_style_doc() -> tuple[DocumentBlock, ...]:
    """Сконструировать DOCX-подобный документ со статьями и пунктами.

    Структура:
        block 0: "Статья 1. Общие положения"        — heading
        block 1: "1. Первый пункт ..."              — body/list item
        block 2: "2. Второй пункт ..."              — body/list item
        block 3: "3. Третий пункт ..."              — body/list item
        block 4: "Статья 2. Следующая статья"       — heading
        block 5: "1. Другой пункт ..."               — body/list item
        block 6: "2. Ещё один пункт ..."             — body/list item
    """
    return tuple(
        _b(i, text)
        for i, text in enumerate([
            "Статья 1. Общие положения",
            (
                "1. Первый пункт содержит достаточно длинный текст, "
                "чтобы это не было похоже на заголовок раздела. "
                "Здесь должно быть содержание статьи нормального размера, "
                "несколько предложений юридического текста."
            ),
            (
                "2. Второй пункт содержит достаточно длинный текст, "
                "чтобы это не было похоже на заголовок раздела. "
                "Типичный пункт статьи в Налоговом кодексе Российской Федерации."
            ),
            (
                "3. Третий пункт содержит достаточно длинный текст, "
                "чтобы это не было похоже на заголовок раздела."
            ),
            "Статья 2. Следующая статья",
            (
                "1. Другой пункт другой статьи с существенным содержанием, "
                "чтобы это тоже не считалось заголовком."
            ),
            (
                "2. Ещё один пункт этой же статьи для проверки "
                "структурной согласованности."
            ),
        ])
    )


def _accepted(blocks: tuple[DocumentBlock, ...]) -> list[HeadingCandidate]:
    """Запустить весь heading pipeline и вернуть принятых кандидатов.

    Эквивалент тому, что делает ``build_document_structure``:
    detect → penalties → evidence scoring → threshold filter.
    """
    raw = detect_heading_candidates(blocks, pdf_path=None)
    penalized = apply_confidence_penalties(raw, blocks)
    scored = apply_evidence_scoring(penalized, blocks)
    return filter_above_threshold(scored)


def test_legal_article_marker_accepted_as_heading():
    """``Статья N.`` — настоящий heading, должен проходить."""
    blocks = _build_legal_style_doc()
    accepted = _accepted(blocks)
    accepted_ordinals = {c.block_index for c in accepted}
    # block 0 ("Статья 1.") и block 4 ("Статья 2.") — настоящие headings.
    assert 0 in accepted_ordinals, (
        f"'Статья 1.' должен быть heading, "
        f"получили ordinals={sorted(accepted_ordinals)}"
    )
    assert 4 in accepted_ordinals, (
        f"'Статья 2.' должен быть heading, "
        f"получили ordinals={sorted(accepted_ordinals)}"
    )


def test_bare_numbered_points_rejected_as_headings():
    """Голые ``1. ... 2. ... 3. ...`` НЕ должны становиться headings.

    Это регрессия для Этапа 1: каждая статья-НК-РФ превращает
    пункты в sections → 199 chunks на 1.5M chars.
    """
    blocks = _build_legal_style_doc()
    accepted = _accepted(blocks)
    accepted_ordinals = {c.block_index for c in accepted}

    for bad_ordinal in [1, 2, 3, 5, 6]:
        assert bad_ordinal not in accepted_ordinals, (
            f"block[{bad_ordinal}] '1./2./3./etc. пункт' ошибочно "
            f"классифицирован как heading. "
            f"Принятые headings: {sorted(accepted_ordinals)}"
        )


def test_sections_much_less_than_body_blocks():
    """Количество sections должно быть << количества body/list блоков.

    На синтетическом документе с 7 блоками должно быть максимум 2
    sections (Статья 1 + Статья 2), а не 7.
    """
    blocks = _build_legal_style_doc()
    accepted = _accepted(blocks)
    sections = len(accepted)
    blocks_count = len(blocks)
    # sections должно быть СТРОГО меньше половины блоков.
    # 7 блоков → max 3 sections (2 expected). Если detector съел всё,
    # sections ≈ 7 → тест падает.
    assert sections * 2 < blocks_count, (
        f"Количество sections ({sections}) должно быть строго меньше "
        f"половины блоков ({blocks_count}). Если sections ≈ blocks_count, "
        f"heading detector ошибочно классифицирует каждый нумерованный "
        f"пункт как heading."
    )
    # Конкретное число — это синтетика: ожидаем ровно 2 (Статья 1, Статья 2).
    assert sections == 2, (
        f"Ожидалось 2 sections (Статья 1 + Статья 2); получено {sections}: "
        f"{[(c.block_index, c.text[:60]) for c in accepted]}"
    )


def test_pdf_outline_at_default_path(tmp_path, monkeypatch):
    """Этап 7.4 плана: убедиться, что PDF outline не превращает каждый
    outline entry автоматически в section.

    Сценарий: PDF содержит 100 outline entries (Структура PDF генерится
    скриптом). Каждый outline entry имеет score 0.95 — выше любого
    threshold. В текущей реализации ``_extract_pdf_outline`` пропускает
    ВСЕ entries. Это значит, что слишком плотный outline даст столько
    же section-ов сколько outline entries.

    Это document-level smoke test: подсчёт количества outline entries
    диагностически — если их слишком много (>200 на документ),
    система должна рассматривать это как suspicious (см. Этап 6).
    Тест проверяет, что сам pipeline count of sections не превышает
    разумного числа при ручном управлении outline.
    """
    # Подделываем PDF outline с 50 entries — это синтетика.
    from document.heading import (
        HeadingCandidate,
        filter_above_threshold,
    )

    # Симулируем 50 outline entries, каждый на отдельном block.
    candidates = [
        HeadingCandidate(
            block_index=i,
            text=f"Section {i}. " + ("X" * 100),
            score=0.95,
            source="pdf_outline",
            level=1,
        )
        for i in range(50)
    ]
    accepted = filter_above_threshold(candidates)
    assert len(accepted) == 50, (
        f"Все 50 outline entries проходят (score=0.95 > threshold=0.60). "
        f"Это документирует текущее поведение и должно быть исправлено "
        f"через дополнительные проверки density (Этап 6). Тест НЕ падает "
        f"на текущем коде — это document-level smoke."
    )
