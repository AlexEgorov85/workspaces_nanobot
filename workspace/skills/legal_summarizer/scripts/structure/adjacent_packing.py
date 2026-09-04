"""Controlled adjacent-section packing (PLAN §9, §22).

Сейчас ``packing_impl.pack_chunks`` строго section-locality greedy —
что безопасно, но для 600-страничного документа даёт
``map_calls == chunks_total`` (см. baseline F3).

Целевая политика (PLAN §9):

1. **Rule 1**: table + non-table → **не смешивать** (отдельные batch).
2. **Rule 2**: table + table → только если это разрешено atomic policy
   (``allow_table_table_batch=False`` по умолчанию — таблицы тоже
   идут в отдельные batch).
3. **Rule 3**: ``<= max_sections_per_batch`` секций на batch
   (по умолчанию 2).
4. **Rule 4**: document order сохраняется внутри batch.
5. **Rule 5**: distant sections не объединяются (только adjacent).
6. **Rule 6**: token budget не превышается.

Это **не** меняет ``Chunk``, только решает, какие chunk_ids идут
в один batch при ``build_map_plan``.
"""

from __future__ import annotations

from dataclasses import dataclass

from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk
from workspace.skills.legal_summarizer.scripts.structure.token_estimator import (
    TokenEstimator, TokenEstimatorConfig,
)


@dataclass(frozen=True)
class AdjacentPackingConfig:
    """Параметры adjacent-section packing.

    Attributes:
        max_sections_per_batch: максимум distinct ``section_id`` в batch.
        per_batch_token_budget: token budget на batch.
        chars_per_token: коэффициент для TokenEstimator.
        allow_table_table_batch: если ``True``, можно объединять два
            table chunk'а в один batch. По умолчанию ``False`` —
            каждая таблица — свой batch.
    """

    max_sections_per_batch: int = 2
    per_batch_token_budget: int = 6000
    chars_per_token: float = 3.5
    allow_table_table_batch: bool = False


def _section_id_for_chunk(chunk: Chunk) -> str:
    return chunk.section_id or ""


def _is_root_chunk(chunk: Chunk) -> bool:
    """``chunk.section_id`` пустой или имеет root-marker.

    Root-marker: пустая строка (``""``) или ``"s_root"``.
    Используется, чтобы preamble blocks (которые принадлежат root,
    но всё равно получили section_id="s_root" или "") шли в отдельный
    batch.
    """
    return not chunk.section_id or chunk.section_id == "s_root"


def pack_chunks_with_adjacent(
    chunks: tuple[Chunk, ...],
    *,
    config: AdjacentPackingConfig | None = None,
) -> list[tuple[str, ...]]:
    """Сгруппировать ``Chunk`` в batches по правилам PLAN §9.

    Возвращает список tuple chunk_ids — порядок execution.

    Алгоритм (greedy, документ-order):

    * Ведём current batch: chunk_ids, section_ids (ordered via
      ``dict.fromkeys`` для детерминизма), tokens, table_flag;
    * Для каждого chunk:
        1. Если chunk is root preamble (no section_id) → закрыть
           current и начать новый (preamble всегда отдельно);
        2. Если chunk is table:
           - если current содержит non-table → закрыть current,
             начать новый (table + non-table не смешивать);
           - если current содержит table и
             ``allow_table_table_batch=False`` → закрыть current,
             начать новый;
           - если current содержит table и
             ``allow_table_table_batch=True`` → можно объединять;
        3. Если adding chunk приведёт к превышению
           ``max_sections_per_batch`` distinct sections → закрыть;
        4. Если adding chunk приведёт к превышению budget → закрыть;
        5. Иначе добавить chunk в current;
    * В конце — закрыть current если не пуст.
    """
    cfg = config or AdjacentPackingConfig()
    estimator = TokenEstimator(
        TokenEstimatorConfig(chars_per_token=cfg.chars_per_token),
    )

    batches: list[tuple[str, ...]] = []

    current_chunk_ids: list[str] = []
    current_section_ids: list[str] = []
    current_tokens = 0
    current_is_table = False

    def _flush() -> None:
        nonlocal current_chunk_ids, current_section_ids, current_tokens
        nonlocal current_is_table
        if current_chunk_ids:
            batches.append(tuple(current_chunk_ids))
        current_chunk_ids = []
        current_section_ids = []
        current_tokens = 0
        current_is_table = False

    for chunk in chunks:
        sec_id = _section_id_for_chunk(chunk)
        is_table = chunk.table_id is not None

        if _is_root_chunk(chunk):
            _flush()
            current_chunk_ids.append(chunk.chunk_id)
            _flush()
            continue

        if is_table and current_chunk_ids and not current_is_table:
            _flush()

        if is_table and current_chunk_ids and current_is_table:
            if not cfg.allow_table_table_batch:
                _flush()

        if not is_table and current_chunk_ids and current_is_table:
            _flush()

        if (
            current_chunk_ids
            and sec_id not in current_section_ids
            and len(current_section_ids) >= cfg.max_sections_per_batch
        ):
            _flush()

        chunk_tokens = estimator.estimate(chunk.text)
        if current_tokens + chunk_tokens > cfg.per_batch_token_budget:
            _flush()

        current_chunk_ids.append(chunk.chunk_id)
        if sec_id and sec_id not in current_section_ids:
            current_section_ids.append(sec_id)
        current_tokens += chunk_tokens
        current_is_table = is_table

    _flush()

    return batches


__all__ = [
    "AdjacentPackingConfig",
    "pack_chunks_with_adjacent",
]