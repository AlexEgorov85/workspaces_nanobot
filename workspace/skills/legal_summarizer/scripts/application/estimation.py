"""Estimation: верхняя граница LLM-вызовов и времени для конкретного запуска.

Использует те же константы, что и ``execution/hierarchical.py``
(``MID_REDUCE_GROUP_SIZE``, ``MAX_REDUCE_ROUNDS``), чтобы
``actual_llm_calls <= estimated_llm_calls`` оставалось гарантированным.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chunking.chunks import Chunk
from document.structure import DocumentStructure
from execution.config import (
    MAX_REDUCE_ROUNDS,
    MID_REDUCE_GROUP_SIZE,
)
from llm.config import get_chunking_config, get_execution_config
from planning.plan import ExecutionPlan


def _service_mod():
    """Lazy lookup ``service.get_execution_config`` (для monkeypatch).

    Тесты делают ``monkeypatch.setattr(service, "get_execution_config", mock)``
    и ожидают, что ``estimate_for_run`` прочитает mock. Чтобы patch
    работал, читаем через module attribute, а не через локальный импорт.
    """
    import application.service as _svc
    return _svc


_QUICK_SAMPLE_PAGES = 10
_CHARS_OVERESTIMATE = 1.3
_BATCH_OVERESTIMATE_RATIO = 1.0


@dataclass(frozen=True)
class Estimate:
    chunks_count: int
    context_batches: int
    estimated_llm_calls: int
    estimated_duration_min_sec: float
    estimated_duration_max_sec: float
    confirmation_threshold_sec: float


def needs_confirmation(est: Estimate) -> bool:
    return est.estimated_duration_max_sec > est.confirmation_threshold_sec


def count_execution_calls(
    plan: ExecutionPlan | None,
    chunks: tuple[Chunk, ...],
    *,
    strategy: str,
    structure: DocumentStructure | None,
) -> int:
    """Верхняя граница (upper bound) числа LLM-вызовов для запуска.

    * ``direct`` / ``plan is None`` → 1 (один прямой вызов).
    * ``map_flat`` → ``len(batches)`` (map) + 1 (document reduce).
    * ``map_hierarchical`` → ``len(batches)`` (map) + ``S`` (section reduce,
      по числу всех sections) + ``D`` (document-level reduce-вызовов, включая
      финальный). ``S + D`` — детерминированная симуляция
      ``reduce_chunks_hierarchical`` (см. ``_hierarchical_reduce_calls``);
      это настоящий upper bound, а не эвристика.
    """
    if plan is None or strategy == "direct":
        return 1
    batches = len(plan.batches)
    if strategy == "map_hierarchical" and structure is not None:
        s = len(list(structure.iter_sections()))
        return batches + s + _hierarchical_reduce_calls(s)
    return batches + 1


def _hierarchical_reduce_calls(sections: int) -> int:
    """Верхняя граница числа document-level LLM-вызовов в
    ``reduce_chunks_hierarchical`` (reduce-фаза после section reduce).

    Симулирует ``reduce_sections_to_document``:

    * ``sections`` section-summaries на входе (в реальности это
      ``len(section_summaries) <= sections`` — только non-empty, поэтому
      оценка по ``sections`` — консервативный upper bound);
    * каждый ``round`` разбивает ``current`` на группы по
      ``MID_REDUCE_GROUP_SIZE`` и вызывает LLM **по разу на каждую группу**
      (не по разу на round!);
    * не более ``MAX_REDUCE_ROUNDS`` таких раундов;
    * если после ``MAX_REDUCE_ROUNDS`` осталось больше одной группы — один
      финальный LLM-вызов объединяет всё оставшееся.

    Возвращает **суммарное** число вызовов по всем раундам + финал.
    """
    if sections <= 1:
        return 0
    current = sections
    calls = 0
    gs = MID_REDUCE_GROUP_SIZE
    rounds = 0
    while current > 1 and rounds < MAX_REDUCE_ROUNDS:
        rounds += 1
        calls += -(-current // gs)
        current = -(-current // gs)
    if current > 1:
        calls += 1
    return calls


def estimate_for_run(insp, ctx) -> Estimate:
    """Estimate для конкретного run-context (run-level).

    ``estimated_llm_calls`` — верхняя граница (upper bound) фактического
    числа LLM-вызовов: гарантированно ``actual <= estimate`` для любой
    стратегии (direct / map_flat / map_hierarchical).
    """
    cfg = _service_mod().get_execution_config()
    chunk_dur = float(cfg["estimated_chunk_duration_sec"])
    threshold = float(cfg["confirmation_threshold_sec"])
    if ctx.plan is None:
        batches = 1
    else:
        batches = len(ctx.plan.batches)
    llm_calls = count_execution_calls(
        ctx.plan, ctx.chunks,
        strategy=ctx.strategy, structure=insp.structure,
    )
    avg = batches * chunk_dur
    return Estimate(
        chunks_count=len(ctx.chunks),
        context_batches=batches,
        estimated_llm_calls=llm_calls,
        estimated_duration_min_sec=round(avg * 0.8, 1),
        estimated_duration_max_sec=round(avg * 1.2, 1),
        confirmation_threshold_sec=threshold,
    )


def quick_estimate(path: Path | str) -> dict[str, Any]:
    """Быстрая оценка размера документа БЕЗ полного извлечения текста."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(str(p))
    ext = p.suffix.lower()
    cfg = _service_mod().get_execution_config()
    chunk_dur = float(cfg["estimated_chunk_duration_sec"])
    threshold = float(cfg["confirmation_threshold_sec"])
    chunk_size = int(get_chunking_config().get("chunk_size", 100000))

    chars_in_est = 0
    if ext == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(p), strict=False)
        page_count = len(reader.pages)
        sample_n = min(_QUICK_SAMPLE_PAGES, page_count)
        sample_chars = 0
        for i in range(sample_n):
            try:
                sample_chars += len(reader.pages[i].extract_text() or "")
            except Exception:
                pass
        if sample_n > 0 and sample_chars > 0:
            avg_per_page = sample_chars / sample_n
            chars_in_est = int(avg_per_page * page_count * _CHARS_OVERESTIMATE)
        else:
            chars_in_est = int(p.stat().st_size * 0.5)
    elif ext == ".txt":
        chars_in_est = int(p.stat().st_size * 0.95)
    elif ext == ".docx":
        from docx import Document as _Docx
        doc = _Docx(str(p))
        n_par = len(doc.paragraphs)
        sample_n = min(50, n_par)
        sample_chars = sum(len(par.text) for par in doc.paragraphs[:sample_n])
        if sample_n > 0:
            avg = sample_chars / sample_n
            chars_in_est = int(avg * n_par * _CHARS_OVERESTIMATE)
        else:
            chars_in_est = int(p.stat().st_size * 0.3)
    else:
        chars_in_est = int(p.stat().st_size)

    chunks_count_est = max(1, -(-chars_in_est // max(1, chunk_size)))
    context_batches_est = max(1, int(chunks_count_est * _BATCH_OVERESTIMATE_RATIO))
    estimated_llm_calls_est = context_batches_est + 1
    avg_sec = context_batches_est * chunk_dur

    return {
        "chars_in": chars_in_est,
        "estimate": Estimate(
            chunks_count=chunks_count_est,
            context_batches=context_batches_est,
            estimated_llm_calls=estimated_llm_calls_est,
            estimated_duration_min_sec=round(avg_sec * 0.8, 1),
            estimated_duration_max_sec=round(avg_sec * 1.2, 1),
            confirmation_threshold_sec=threshold,
        ),
    }


__all__ = [
    "Estimate",
    "needs_confirmation",
    "count_execution_calls",
    "estimate_for_run",
    "quick_estimate",
]
