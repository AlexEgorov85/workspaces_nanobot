"""Deterministic cleanup: header / footer / duplicate detection.

Идея: короткие блоки (≤ ``max_length_for_repetition_detection`` символов),
которые повторяются на многих страницах, — это кандидаты на header/footer.
Мы **не удаляем** их (это было бы рискованно для tables и редких слов),
а только помечаем через ``block_metadata``:

    * ``is_repeated = True`` — нормализованный текст встречается ≥ threshold раз;
    * ``repeated_role`` — ``"header"`` / ``"footer"`` / ``"duplicate"``.

Решение «съесть» блок принимается downstream (например, chunker может
пропускать блоки с ``is_repeated`` или включать только первую копию).
Сейчас мы только **маркируем**.

Алгоритм:
    1. Нормализовать whitespace: ``re.sub(r"\s+", " ", text).strip()``.
    2. Считать частоту нормализованных текстов среди блоков с
       ``len(normalized) <= max_length``.
    3. Для блоков с частотой ≥ ``threshold`` пометить как repeated.
    4. Длинные блоки (> ``max_length``) никогда не считаются повторяющимися.
    5. ``repeated_role`` эвристически:
       * первая встреча в документе → ``"header"``;
       * последняя → ``"footer"``;
       * средняя — ``"duplicate"``.
       Это намеренно грубо: точное определение роли даёт LLM-detection,
       но LLM для этого не используется (deterministic only).

NOTE: модуль top-level, не ``document/cleanup.py``, чтобы не создавать
конфликт имён с существующим ``document_cache.py``. Когда baseline
позволит переименование — мигрируем на целевую структуру.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from workspace.skills.legal_summarizer.scripts.structure.physical import (
    DocumentBlock,
)


_WHITESPACE_RE = re.compile(r"\s+")


def normalize_whitespace(text: str) -> str:
    """Нормализовать whitespace: ``\\s+`` → один пробел, strip по краям.

    Делает «PAGE  1» и «PAGE 1» одинаковыми для целей сравнения.
    """
    return _WHITESPACE_RE.sub(" ", text or "").strip()


@dataclass(frozen=True)
class CleanupConfig:
    """Параметры cleanup."""

    repetition_threshold: int = 3
    max_length_for_repetition_detection: int = 300


@dataclass(frozen=True)
class CleanupResult:
    """Сводка по cleanup."""

    total_blocks: int
    repeated_blocks: int
    header_candidates: int
    footer_candidates: int
    duplicate_candidates: int


def _classify_role(positions: list[int], total: int) -> str:
    """Определить role повторяющегося normalized-текста по позициям вхождений.

    Эвристика (намеренно грубая — точная классификация требует LLM,
    который мы не используем на этапе 6):

    * Если **первое** вхождение — самое раннее в документе (``positions[0] == 0``)
      и **средняя** позиция строго меньше midpoint документа —
      это **header** (встречается в верхней части страниц).
    * Если **последнее** вхождение — самое позднее (``positions[-1] == total - 1``)
      и **средняя** позиция ≥ midpoint — это **footer**.
    * Иначе — **duplicate**.

    Пример: HEADER на позициях ``[0, 2, 4]`` в списке из 6 блоков
    (HEADER/content/HEADER/content/HEADER/content):
        ``positions[0] == 0`` ✓, ``mean == 2.0 < 3`` ✓ → ``"header"``.
    """
    if not positions:
        return "duplicate"
    midpoint = total / 2.0
    mean_pos = sum(positions) / len(positions)
    if positions[0] == 0 and mean_pos < midpoint:
        return "header"
    if positions[-1] == total - 1 and mean_pos >= midpoint:
        return "footer"
    return "duplicate"


def cleanup_blocks(
    blocks: Iterable[DocumentBlock],
    config: CleanupConfig | None = None,
) -> tuple[list[DocumentBlock], CleanupResult]:
    """Пометить повторяющиеся короткие блоки как header/footer/duplicate.

    Возвращает новый список блоков (старые **не мутируются**) +
    статистику ``CleanupResult``.

    Алгоритм:
        1. Нормализовать whitespace каждого блока.
        2. Сгруппировать normalized-тексты с частотой ≥ ``repetition_threshold``
           и длиной ≤ ``max_length_for_repetition_detection``.
        3. Для каждой такой группы определить role на основе позиций
           вхождений (header / footer / duplicate).
        4. Пометить **все** вхождения группы этим role и ``repeated_count``.
    """
    cfg = config or CleanupConfig()

    block_list = list(blocks)
    n_total = len(block_list)

    # 1. Нормализация.
    normalized: list[str] = [normalize_whitespace(b.content) for b in block_list]

    # 2. Группировка по normalized-тексту.
    norm_to_positions: dict[str, list[int]] = {}
    for idx, n in enumerate(normalized):
        if 0 < len(n) <= cfg.max_length_for_repetition_detection:
            norm_to_positions.setdefault(n, []).append(idx)

    # 3. Классификация групп.
    norm_to_role: dict[str, str] = {}
    norm_to_count: dict[str, int] = {}
    for n, positions in norm_to_positions.items():
        if len(positions) >= cfg.repetition_threshold:
            norm_to_role[n] = _classify_role(positions, n_total)
            norm_to_count[n] = len(positions)

    # 4. Маркировка.
    out: list[DocumentBlock] = []
    header_count = footer_count = duplicate_count = 0
    for b, n in zip(block_list, normalized):
        role = norm_to_role.get(n)
        if role is None:
            out.append(b)
            continue

        if role == "header":
            header_count += 1
        elif role == "footer":
            footer_count += 1
        else:
            duplicate_count += 1

        new_meta = dict(b.block_metadata)
        new_meta["is_repeated"] = True
        new_meta["repeated_role"] = role
        new_meta["repeated_count"] = norm_to_count[n]
        out.append(
            DocumentBlock(
                block_id=b.block_id,
                block_type=b.block_type,
                content=b.content,
                char_count=b.char_count,
                page_index=b.page_index,
                page_start=b.page_start,
                page_end=b.page_end,
                paragraph_index=b.paragraph_index,
                table_index=b.table_index,
                ordinal=b.ordinal,
                block_metadata=new_meta,
            )
        )

    stats = CleanupResult(
        total_blocks=n_total,
        repeated_blocks=header_count + footer_count + duplicate_count,
        header_candidates=header_count,
        footer_candidates=footer_count,
        duplicate_candidates=duplicate_count,
    )
    return out, stats


__all__ = [
    "CleanupConfig",
    "CleanupResult",
    "cleanup_blocks",
    "normalize_whitespace",
]
