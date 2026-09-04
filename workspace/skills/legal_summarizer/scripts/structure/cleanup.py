"""Repeated header/footer cleanup (PLAN §42, Этап 43, Этап 50).

Сейчас ``document_cleanup.py`` только **маркирует** блоки
(``is_repeated``, ``repeated_role``, ``repeated_count``), но
downstream не использует эти данные.

PLAN §42 говорит: cleanup должен либо использоваться, либо удалён.
Мы выбираем **Вариант A** (использовать): high-confidence repeated
header/footer исключаются из semantic map, но **first occurrence**
и **provenance** сохраняются.

Алгоритм (PLAN §43):

* repetition (≥ 3 раз) +
* same page position +
* same region (top/bottom of page) +
* short length (< 200 chars) +
* typography (если доступно) +
* (low-confidence блоки не удалять).

Этот модуль предоставляет ``cleanup_repeated_blocks`` как
**детерминированный** фильтр.
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


@dataclass(frozen=True)
class CleanupConfig:
    """Параметры cleanup."""

    min_repetitions: int = 3
    max_block_chars: int = 200
    keep_first_occurrence: bool = True


def detect_repeated_regions(
    blocks: Iterable[DocumentBlock],
    *,
    config: CleanupConfig | None = None,
) -> list[RepeatedRegion]:
    """Найти группы repeated blocks (по тексту).

    Не учитывает page position (нет доступа без page geometry); ищет
    только по тексту и min_repetitions. Это **минимум** PLAN §43 —
    полная реализация требует coordinates. Сейчас достаточно для
    semantic cleanup.
    """
    cfg = config or CleanupConfig()
    by_text: dict[str, list[int]] = {}
    for b in blocks:
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
        role = _guess_role(text)
        regions.append(
            RepeatedRegion(role=role, block_ordinals=tuple(ordinals), text=text),
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


__all__ = [
    "RepeatedRegion",
    "CleanupConfig",
    "detect_repeated_regions",
    "cleanup_repeated_blocks",
]