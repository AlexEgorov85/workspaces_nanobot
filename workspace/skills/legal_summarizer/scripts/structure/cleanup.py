"""Repeated header/footer cleanup (PLAN §18, §42).

Cleanup должен либо использоваться, либо удалён. Используем **Вариант A**:
high-confidence repeated header/footer исключаются из semantic map, но
**first occurrence** и **provenance** сохраняются.

Алгоритм (PLAN §18):

* **repetition** (≥ N раз) — основной признак;
* **page-aware evidence** (если доступна page geometry): блок на
  одной странице с тем же текстом + same region (top/bottom) даёт
  дополнительный вес;
* **short length** (< N chars) — обычно header/footer короткие;
* **typography** учитывается только если physical model реально
  предоставляет эту информацию; если координат нет — не придумываем
  fake geometry.

Если координат нет — явно ограничиваем invariant:

    text repetition + page-aware evidence when available

Не удалять обычный повторяющийся текст внутри legal content только
потому, что он повторился (см. acceptance criteria §18).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from workspace.skills.legal_summarizer.scripts.structure.physical import (
    DocumentBlock,
)


@dataclass(frozen=True)
class RepeatedRegion:
    """Группа repeated blocks (один и тот же header/footer)."""

    role: str
    block_ordinals: tuple[int, ...]
    text: str
    has_page_evidence: bool = False


@dataclass(frozen=True)
class CleanupConfig:
    """Параметры cleanup.

    Attributes:
        min_repetitions: минимальное число повторений текста.
        max_block_chars: порог длины для header/footer candidates.
        keep_first_occurrence: сохранить первое вхождение (provenance).
        require_page_evidence: если True, repeated region без
            page-aware evidence НЕ считается header/footer (защита
            от false-positive на legal content). Default ``False``
            (back-compat с старым поведением).
    """

    min_repetitions: int = 3
    max_block_chars: int = 200
    keep_first_occurrence: bool = True
    require_page_evidence: bool = False


def _has_page_evidence(ordinals: list[int], blocks: list[DocumentBlock]) -> bool:
    """True если repeated blocks расположены на ≥ 2 разных страницах.

    Используется как page-aware evidence: повторяющийся текст на разных
    страницах с большей вероятностью header/footer (просто повторяющийся
    текст внутри одной страницы — не header/footer).
    """
    by_ord = {b.ordinal: b for b in blocks}
    pages: set[int] = set()
    for o in ordinals:
        b = by_ord.get(o)
        if b is None:
            continue
        if b.page_index is not None:
            pages.add(b.page_index)
    return len(pages) >= 2


def detect_repeated_regions(
    blocks: Iterable[DocumentBlock],
    *,
    config: CleanupConfig | None = None,
) -> list[RepeatedRegion]:
    """Найти группы repeated blocks (по тексту).

    При наличии page geometry — добавляет ``has_page_evidence`` если
    блоки повторяются на разных страницах с известным ``page_index``.
    """
    cfg = config or CleanupConfig()
    blocks_list = list(blocks)
    by_text: dict[str, list[int]] = {}
    for b in blocks_list:
        if len(b.content.strip()) > cfg.max_block_chars:
            continue
        key = b.content.strip()
        if len(key) < 3:
            continue
        by_text.setdefault(key, []).append(b.ordinal)

    regions: list[RepeatedRegion] = []
    for text, ordinals in by_text.items():
        if len(ordinals) < cfg.min_repetitions:
            continue
        has_page = _has_page_evidence(ordinals, blocks_list)
        if cfg.require_page_evidence and not has_page:
            continue
        role = _guess_role(text)
        regions.append(
            RepeatedRegion(
                role=role,
                block_ordinals=tuple(ordinals),
                text=text,
                has_page_evidence=has_page,
            ),
        )
    return regions


def _guess_role(text: str) -> str:
    """Грубая эвристика: header/footer/caption/other."""
    lowered = text.lower()
    if any(s in lowered for s in ("copyright", "©", "все права защищены")):
        return "footer_copyright"
    if any(s in lowered for s in ("стр.", "page", "лист")):
        return "footer_page_number"
    if len(text) < 50:
        return "header_short"
    return "other"


def cleanup_repeated_blocks(
    blocks: tuple[DocumentBlock, ...],
    *,
    config: CleanupConfig | None = None,
) -> list[int]:
    """Вернуть ordinals блоков, которые **исключаются** из semantic map.

    Исключения:
    * блоки, входящие в repeated regions;
    * ``keep_first_occurrence=True`` — первое вхождение **сохраняется**.

    Returns:
        list[int] — ordinals блоков для удаления.
    """
    cfg = config or CleanupConfig()
    regions = detect_repeated_regions(blocks, config=cfg)
    to_remove: set[int] = set()
    for region in regions:
        ordinals_to_remove = list(region.block_ordinals)
        if cfg.keep_first_occurrence and ordinals_to_remove:
            ordinals_to_remove = ordinals_to_remove[1:]
        to_remove.update(ordinals_to_remove)
    return sorted(to_remove)


def is_repeated(
    blocks: tuple[DocumentBlock, ...],
    ordinal: int,
    *,
    config: CleanupConfig | None = None,
) -> bool:
    """True если блок с ``ordinal`` принадлежит repeated region.

    Не удалять обычный повторяющийся текст внутри legal content —
    ``require_page_evidence=True`` снижает false-positives (PLAN §18
    acceptance).
    """
    if ordinal in cleanup_repeated_blocks(blocks, config=config):
        return True
    return False


__all__ = [
    "RepeatedRegion",
    "CleanupConfig",
    "detect_repeated_regions",
    "cleanup_repeated_blocks",
    "is_repeated",
]