"""Heading detection (candidates + scoring) — ``structure/heading.py``.

Поведение НЕ меняется.

Содержит:
    * ``CONFIDENCE_THRESHOLD`` — порог score для принятия кандидата.
    * ``HeadingCandidate`` — dataclass-кандидат на heading.
    * ``_classify_regex`` — определение level/score/source по regex'ам.
    * ``_extract_pdf_outline`` — PDF outline → list[HeadingCandidate].
    * ``_detect_candidates`` — найти кандидатов среди DocumentBlock.
    * ``_apply_confidence_penalties`` — снижение score для «голых» headings.
    * ``_filter_above_threshold`` — финальный фильтр.

Построение SectionTree (DocumentSection + SectionTree) вынесено в
``structure/tree.py``. Facade — ``structure/sections.py``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from workspace.skills.legal_summarizer.scripts.structure.models import NumberingInfo
from workspace.skills.legal_summarizer.scripts.structure.numbering import parse_numbering
from workspace.skills.legal_summarizer.scripts.structure.pdf_outline import (
    map_pdf_outline,
    mapped_to_heading_candidates,
)
from workspace.skills.legal_summarizer.scripts.structure.physical import (
    DocumentBlock,
    PhysicalDocument,
)
from workspace.skills.legal_summarizer.scripts.structure.list_detection import (
    detect_list_runs,
    list_penalty_for_candidate,
)


CONFIDENCE_THRESHOLD = 0.60

_HEADING_KEYWORDS = ("heading ", "heading_", "заголовок")
_TITLE_KEYWORDS = ("title", "титул", "название", "subtitle", "подзаголовок")

_RE_NUMBERED_LEVEL_1 = re.compile(r"^\s*(\d+)\.\s+(.{2,200})$")
_RE_NUMBERED_LEVEL_2 = re.compile(r"^\s*(\d+)\.(\d+)\.?\s+(.{2,200})$")
_RE_NUMBERED_LEVEL_3 = re.compile(r"^\s*(\d+)\.(\d+)\.(\d+)\.?\s+(.{2,200})$")

_RE_STATIYA = re.compile(r"^\s*Статья\s+(\d+(?:\.\d+)?)\s*\.?\s*(.{2,200})$", re.IGNORECASE)
_RE_GLAVA = re.compile(r"^\s*Глава\s+(\d+(?:\.\d+)?)\s*\.?\s*(.{2,200})$", re.IGNORECASE)
_RE_RAZDEL = re.compile(r"^\s*Раздел\s+(\d+(?:\.\d+)?)\s*\.?\s*(.{2,200})$", re.IGNORECASE)
_RE_PARAGRAPH = re.compile(r"^\s*§\s*(\d+(?:\.\d+)?)\s*\.?\s*(.{2,200})$")

_ANY_HEADING_RE = re.compile(
    r"^\s*(?:\d+\.(?:\d+\.?)?(?:\d+\.?)?|Статья\s+\d+|Глава\s+\d+|Раздел\s+\d+|§\s*\d+)\s*[.:]?\s*\S"
)


@dataclass(frozen=True)
class HeadingCandidate:
    """Кандидат на heading с confidence score."""

    block_index: int
    text: str
    score: float
    source: str
    level: int
    raw_number: str | None = None


def _is_docx_heading_style(style_name: str) -> bool:
    if not style_name:
        return False
    name = style_name.lower()
    return any(name.startswith(prefix) for prefix in _HEADING_KEYWORDS)


def _is_docx_title_style(style_name: str) -> bool:
    """True если DOCX style — это Title / Subtitle (PLAN §14).

    Title-стили дают очень высокую уверенность, что параграф — это
    document title (а не heading). Используется при формировании
    ``DocumentTitle`` в ``DocumentStructure``.
    """
    if not style_name:
        return False
    name = style_name.lower()
    return any(name.startswith(prefix) for prefix in _TITLE_KEYWORDS)


def _looks_like_heading(text: str) -> bool:
    """Грубая проверка формата heading в тексте."""
    return bool(_ANY_HEADING_RE.match(text.strip()))


def _classify_regex(text: str) -> tuple[int, float, str, str | None] | None:
    """Классифицировать текст по regex'ам. Вернуть (level, score, source, number)."""
    s = text.strip()
    if not s:
        return None
    m = _RE_NUMBERED_LEVEL_3.match(s)
    if m:
        return (3, 0.70, "regex_numbered_3", f"{m.group(1)}.{m.group(2)}.{m.group(3)}")
    m = _RE_NUMBERED_LEVEL_2.match(s)
    if m:
        return (2, 0.70, "regex_numbered_2", f"{m.group(1)}.{m.group(2)}")
    m = _RE_NUMBERED_LEVEL_1.match(s)
    if m:
        return (1, 0.65, "regex_numbered_1", m.group(1))
    m = _RE_STATIYA.match(s)
    if m:
        return (1, 0.85, "regex_statiya", f"статья_{m.group(1)}")
    m = _RE_GLAVA.match(s)
    if m:
        return (1, 0.80, "regex_glava", f"глава_{m.group(1)}")
    m = _RE_RAZDEL.match(s)
    if m:
        return (1, 0.80, "regex_razdel", f"раздел_{m.group(1)}")
    m = _RE_PARAGRAPH.match(s)
    if m:
        return (2, 0.80, "regex_paragraph", f"§{m.group(1)}")
    return None


def _extract_pdf_outline(path: str) -> list[HeadingCandidate]:
    """Прочитать PDF outline → list[HeadingCandidate].

    Не возвращаем уровень/номер — outline сам даёт структуру,
    а нам нужно только текст заголовка и порядок.
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        return []

    try:
        reader = PdfReader(path)
    except Exception:
        return []

    candidates: list[HeadingCandidate] = []
    ordinal_counter = 0
    try:
        items = list(reader.outline)
    except Exception:
        items = []

    def _walk(items: list[Any], level: int) -> None:
        nonlocal ordinal_counter
        for item in items:
            if isinstance(item, list):
                _walk(item, level + 1)
                continue
            try:
                title = getattr(item, "title", None) or str(item)
            except Exception:
                continue
            if not title or not str(title).strip():
                continue
            candidates.append(
                HeadingCandidate(
                    block_index=-1,
                    text=str(title).strip(),
                    score=0.95,
                    source="pdf_outline",
                    level=level,
                    raw_number=None,
                )
            )
            ordinal_counter += 1

    try:
        _walk(items, 1)
    except Exception:
        pass
    return candidates


def detect_heading_candidates(
    blocks: tuple[DocumentBlock, ...],
    pdf_path: str | None,
    *,
    physical_doc: Optional[PhysicalDocument] = None,
) -> list[HeadingCandidate]:
    """Найти всех кандидатов в heading'и (DOCX style + regex + PDF outline).

    Public API этапа 7: было приватной ``_detect_candidates`` в ``sections.py``,
    теперь экспортируется из ``heading.py``.

    Этап 7 (PLAN): numbering detection делегирован в
    ``scripts/structure/numbering.py`` (``parse_numbering``). Старый
    ``_classify_regex`` оставлен для back-compat, но теперь результат
    сверяется с новым parser'ом — и если новый parser даёт иную
    информацию (например, level на основе nested components),
    используется он.

    Этап 11 (PLAN §11): PDF outline mapping теперь делается через
    ``scripts/structure/pdf_outline.py::map_pdf_outline``, который
    возвращает ``HeadingCandidate`` с реальным ``block_index >= 0``
    (раньше outline кандидаты имели ``block_index = -1`` и отбрасывались
    в ``build_section_tree``). Для этого требуется ``physical_doc`` —
    если он передан, mapping делается; если нет — fallback на legacy
    ``_extract_pdf_outline`` (без mapping, ``block_index = -1``).
    """
    candidates: list[HeadingCandidate] = []

    for block in blocks:
        if block.block_type == "table":
            continue
        text = block.content.strip()

        style_name = block.block_metadata.get("style", "")
        if _is_docx_heading_style(style_name):
            level = 1
            m = re.search(r"(\d+)", style_name)
            if m:
                try:
                        level = max(1, min(6, int(m.group(1))))
                except ValueError:
                    pass
            candidates.append(
                HeadingCandidate(
                    block_index=block.ordinal,
                    text=text,
                    score=0.95,
                    source="docx_style",
                    level=level,
                    raw_number=None,
                )
            )
            continue

        classified = _classify_regex(text)
        if classified is None:
            continue
        level, score, source, raw_number = classified
        if level == 1 and len(text) > 80:
            score = min(score, 0.55)

        ni = parse_numbering(text)
        if ni is not None:
            level = max(level, ni.level)

        candidates.append(
            HeadingCandidate(
                block_index=block.ordinal,
                text=text,
                score=score,
                source=source,
                level=level,
                raw_number=raw_number,
            )
        )

    if pdf_path:
        if physical_doc is not None:
            mapped = map_pdf_outline(pdf_path, physical_doc)
            outline_candidates = mapped_to_heading_candidates(mapped)
        else:
            outline_candidates = _extract_pdf_outline(pdf_path)
        for c in outline_candidates:
            candidates.append(c)

    return candidates


def apply_confidence_penalties(
    candidates: list[HeadingCandidate],
    blocks: tuple[DocumentBlock, ...],
) -> list[HeadingCandidate]:
    """Снизить score для heading'ов, после которых идёт другой heading
    или пустой block (anti-false-positive)."""
    if not candidates:
        return candidates
    by_index = {c.block_index: c for c in candidates if c.block_index >= 0}
    if not by_index:
        return candidates
    max_ord = max(b.ordinal for b in blocks)
    out: list[HeadingCandidate] = []
    for c in candidates:
        if c.block_index < 0 or c.source == "pdf_outline":
            out.append(c)
            continue
        next_idx = c.block_index + 1
        if next_idx > max_ord:
            out.append(HeadingCandidate(**{**c.__dict__, "score": c.score * 0.5}))
            continue
        next_block = blocks[next_idx] if next_idx < len(blocks) else None
        if next_block is None or not next_block.content.strip():
            out.append(HeadingCandidate(**{**c.__dict__, "score": c.score * 0.5}))
            continue
        if next_block.ordinal in by_index:
            next_score = by_index[next_block.ordinal].score
            if next_score >= CONFIDENCE_THRESHOLD:
                out.append(HeadingCandidate(**{**c.__dict__, "score": c.score * 0.5}))
                continue
        out.append(c)
    return out


# ---------------------------------------------------------------------------
# Evidence-based heading scoring
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HeadingEvidence:
    """Контекстные признаки, влияющие на heading-score.

    Все поля — дельты относительно ``source_score`` (см. ``HeadingCandidate.score``).
    Итоговый score = source + bonuses - penalties.

    Это намеренно **детерминированная** эвристика (без LLM): LLM-классификация
    заголовков была бы дороже и нестабильнее, чем набор локальных правил.

    Уровни уверенности (PLAN §8):

    * Very high: DOCX Heading style, mapped PDF outline, explicit legal markers.
    * High: numbering + typography + body_after + neighbor_consistency.
    * Medium: bold + short + uppercase + centered.
    * Low: только короткая строка или только номер.

    Attributes:
        source_score: копия ``HeadingCandidate.score`` для удобства сводки.
        short_text_bonus: +0.05 если heading короткий (< 80 chars).
        body_after_bonus: +0.05 если после heading идёт substantial body
            (≥ 100 chars в следующем блоке).
        numbering_consistency_bonus: +0.05 если соседние headings одного
            уровня образуют монотонную последовательность (см. ``numbering.assign_sibling_ordinals``).
        typography_bonus: +0.05 если heading имеет «heading-стиль»
            (короткий + title-case ИЛИ весь uppercase).
        legal_marker_bonus: +0.10 если heading содержит явный legal marker
            (Статья / Глава / Раздел / § / Пункт / Приложение).
        docx_title_bonus: +0.15 если DOCX style — это Title/Subtitle
            (PLAN §14: title не heading — но он помогает выбрать «главный»
            heading для первой секции).
        list_penalty: −0.10 если heading окружён list-like соседями
            (≥ 3 коротких нумерованных блока подряд).
        duplicate_penalty: −0.20 если текст совпадает с предыдущим heading'ом.
    """

    source_score: float
    short_text_bonus: float = 0.0
    body_after_bonus: float = 0.0
    numbering_consistency_bonus: float = 0.0
    typography_bonus: float = 0.0
    legal_marker_bonus: float = 0.0
    docx_title_bonus: float = 0.0
    list_penalty: float = 0.0
    duplicate_penalty: float = 0.0

    @property
    def total_delta(self) -> float:
        """Итоговая дельта score (прибавить к source_score)."""
        return (
            self.short_text_bonus
            + self.body_after_bonus
            + self.numbering_consistency_bonus
            + self.typography_bonus
            + self.legal_marker_bonus
            + self.docx_title_bonus
            - self.list_penalty
            - self.duplicate_penalty
        )

    @property
    def final_score(self) -> float:
        """Итоговый score после всех bonuses/penalties."""
        return max(0.0, min(1.0, self.source_score + self.total_delta))


_SHORT_TEXT_MAX = 80
_BODY_AFTER_MIN = 100
_UPPERCASE_MIN_RATIO = 0.7


def _is_short(text: str, max_len: int = _SHORT_TEXT_MAX) -> bool:
    return 0 < len(text.strip()) <= max_len


def _looks_like_heading_typography(text: str) -> bool:
    """``True`` если текст выглядит как heading по типографике.

    Простая эвристика: либо «короткий и title-cased», либо «короткий и uppercase».
    """
    s = text.strip()
    if not _is_short(s):
        return False
    if s.isupper():
        return True
    # title-case: каждое слово начинается с заглавной буквы.
    words = [w for w in s.split() if w]
    if not words:
        return False
    cap_ratio = sum(1 for w in words if w[0].isupper()) / len(words)
    return cap_ratio >= _UPPERCASE_MIN_RATIO


def _is_substantial_body(text: str) -> bool:
    return len(text.strip()) >= _BODY_AFTER_MIN


def _collect_previous_heading_text(
    candidate: HeadingCandidate,
    candidates: list[HeadingCandidate],
) -> str | None:
    """Текст предыдущего кандидата в heading'и (по block_index), если есть."""
    prev_text: str | None = None
    for c in candidates:
        if c.block_index < 0 or c.source == "pdf_outline":
            continue
        if c.block_index >= candidate.block_index:
            break
        prev_text = c.text
    return prev_text


def _numbering_consistency_with_neighbors(
    candidate: HeadingCandidate,
    candidates: list[HeadingCandidate],
) -> bool:
    """``True`` если кандидат участвует в монотонной последовательности.

    Эвристика: есть предыдущий кандидат того же уровня, и его ``raw_number``
    отличается на 1 (или близкое значение) — означает, что headings
    упорядочены (1, 2, 3, ...).
    """
    if candidate.raw_number is None:
        return False
    try:
        cur = int(candidate.raw_number.split(".")[0])
    except ValueError:
        return False

    for c in candidates:
        if c.block_index < 0 or c.source == "pdf_outline":
            continue
        if c.block_index >= candidate.block_index:
            break
        if c.level != candidate.level or c.raw_number is None:
            continue
        try:
            prev = int(c.raw_number.split(".")[0])
        except ValueError:
            continue
        if abs(cur - prev) == 1:
            return True
    return False


def compute_evidence(
    candidate: HeadingCandidate,
    blocks: tuple[DocumentBlock, ...],
    all_candidates: list[HeadingCandidate],
) -> HeadingEvidence:
    """Вычислить контекстные evidence для одного кандидата.

    Не зависит от того, прошёл ли кандидат threshold — это чистый скоринг,
    используемый после confidence penalties.
    """
    text = candidate.text.strip()

    short_bonus = 0.05 if _is_short(text) else 0.0

    body_bonus = 0.0
    next_idx = candidate.block_index + 1
    if 0 <= candidate.block_index and next_idx < len(blocks):
        next_block = blocks[next_idx]
        if _is_substantial_body(next_block.content):
            body_bonus = 0.05

    num_bonus = (
        0.05
        if _numbering_consistency_with_neighbors(candidate, all_candidates)
        else 0.0
    )

    typo_bonus = 0.05 if _looks_like_heading_typography(text) else 0.0

    legal_bonus = 0.10 if _looks_like_explicit_legal_marker(text) else 0.0

    docx_title_bonus = 0.0
    if 0 <= candidate.block_index < len(blocks):
        style_name = blocks[candidate.block_index].block_metadata.get("style", "")
        if _is_docx_title_style(style_name):
            docx_title_bonus = 0.15

    list_pen = 0.0

    dup_pen = 0.0
    prev_text = _collect_previous_heading_text(candidate, all_candidates)
    if prev_text is not None and prev_text.strip() == text:
        dup_pen = 0.20

    return HeadingEvidence(
        source_score=candidate.score,
        short_text_bonus=short_bonus,
        body_after_bonus=body_bonus,
        numbering_consistency_bonus=num_bonus,
        typography_bonus=typo_bonus,
        legal_marker_bonus=legal_bonus,
        docx_title_bonus=docx_title_bonus,
        list_penalty=list_pen,
        duplicate_penalty=dup_pen,
    )


_LEGAL_MARKERS = (
    "Статья", "Глава", "Раздел", "Пункт", "Часть",
    "§", "Приложение",
)


def _looks_like_explicit_legal_marker(text: str) -> bool:
    """``True`` если текст содержит явный юридический маркер (PLAN §8)."""
    s = text.strip()
    if not s:
        return False
    head = s.split(None, 1)[0] if s else ""
    for marker in _LEGAL_MARKERS:
        if head.startswith(marker):
            return True
    return False


def apply_evidence_scoring(
    candidates: list[HeadingCandidate],
    blocks: tuple[DocumentBlock, ...],
) -> list[HeadingCandidate]:
    """Пересчитать score на основе контекстных evidence.

    Применяется ПОСЛЕ ``apply_confidence_penalties``. Итог:
    ``final_score = source + bonuses - penalties``, обрезанный в [0, 1].

    Для кандидатов без каких-либо evidence (например, PDF outline с
    ``block_index=-1``, где контекст недоступен) score остаётся без изменений.

    Дополнительный list-penalty через ``list_detection``. List-runs
    обнаруживаются один раз для всего набора кандидатов (а не per-candidate),
    чтобы не делать дорогой regex-проход повторно.
    """
    if not candidates:
        return candidates

    list_runs = detect_list_runs(blocks)

    out: list[HeadingCandidate] = []
    for c in candidates:
        if c.block_index < 0 or c.source == "pdf_outline":
            out.append(c)
            continue

        ev = compute_evidence(c, blocks, candidates)
        extra_list_penalty = list_penalty_for_candidate(c.block_index, list_runs)
        if extra_list_penalty > ev.list_penalty:
            ev = HeadingEvidence(
                source_score=ev.source_score,
                short_text_bonus=ev.short_text_bonus,
                body_after_bonus=ev.body_after_bonus,
                numbering_consistency_bonus=ev.numbering_consistency_bonus,
                typography_bonus=ev.typography_bonus,
                legal_marker_bonus=ev.legal_marker_bonus,
                docx_title_bonus=ev.docx_title_bonus,
                list_penalty=extra_list_penalty,
                duplicate_penalty=ev.duplicate_penalty,
            )

        delta = ev.total_delta
        if delta == 0.0:
            out.append(c)
            continue
        new_score = max(0.0, min(1.0, c.score + delta))
        out.append(HeadingCandidate(**{**c.__dict__, "score": new_score}))
    return out


def filter_above_threshold(
    candidates: list[HeadingCandidate],
) -> list[HeadingCandidate]:
    return [c for c in candidates if c.score >= CONFIDENCE_THRESHOLD]


__all__ = [
    "CONFIDENCE_THRESHOLD",
    "HeadingCandidate",
    "HeadingEvidence",
    "detect_heading_candidates",
    "apply_confidence_penalties",
    "apply_evidence_scoring",
    "filter_above_threshold",
    "compute_evidence",
    "_classify_regex",
    "_extract_pdf_outline",
    "_is_docx_heading_style",
    "_looks_like_heading",
]
