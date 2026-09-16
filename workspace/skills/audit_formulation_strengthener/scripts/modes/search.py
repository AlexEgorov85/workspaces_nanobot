"""Режим ``search`` — поиск релевантных фрагментов ВНД.

Реализация этапа 5: чистый LLM map-reduce.

Алгоритм:
1. ``prepare_vnd`` (этап 3) — извлечь все чанки ВНД через
   ``DocumentStructureChunker`` из ``legal_summarizer``.
2. **Map-фаза:** для каждого чанка — 1 LLM-вызов с промптом
   ``prompts/search_chunk_system.md``. LLM возвращает
   ``{relation_type, relevance_score, why_matches}``.
3. **Filter:** отбрасываем чанки с ``relevance_score < MIN_RELEVANCE_SCORE``.
4. **Re-rank:** сортируем по ``relevance_score`` (убывание), берём top-K
   (``TOP_K_CANDIDATES``).
5. Возвращаем JSON со списком ``vnd_findings`` + метаданными.

Single-flight защита — все LLM-вызовы проходят через ``guarded_chat``
из ``legal_summarizer.scripts.llm.single_flight`` (см. ``scripts.llm``),
что исключает параллельные вызовы внутри одного процесса.

Skill-specific параметры (не вынесены в ``project.json::skills.*`` —
не поддерживаются ``SkillSettings(extra="forbid")``):
* ``TOP_K_CANDIDATES`` — сколько top-кандидатов передавать в synthesize;
* ``MIN_RELEVANCE_SCORE`` — порог отсечения слаборелевантных кандидатов.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# Добавляем scripts/ skill'а в sys.path.
_SKILL_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from llm_client import JsonParseError, call_llm_json  # type: ignore[import-not-found]  # noqa: E402
from prompts import load_prompt, render_prompt  # type: ignore[import-not-found]  # noqa: E402
from vnd_io import VndInputError, prepare_vnd  # type: ignore[import-not-found]  # noqa: E402

import output as _output  # type: ignore[import-not-found]  # noqa: E402


__all__ = ["run"]


# Skill-specific параметры (см. docstring).
TOP_K_CANDIDATES: int = 10
MIN_RELEVANCE_SCORE: float = 0.3

_ALLOWED_RELATION_TYPES = frozenset({
    "прямое_противоречие",
    "прямое_подтверждение",
    "косвенное_отношение",
    "контекст",
    "нерелевантно",
})


def run(
    *,
    violation: str,
    vnd_paths: list[str],
    analyze_result_path: str | None = None,
    analyze_result: dict[str, Any] | None = None,
    estimate_only: bool = False,
    confirm: bool = False,
    max_chunks: int | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Поиск релевантных фрагментов ВНД (map-reduce через LLM).

    Args:
        violation: текст отклонения.
        vnd_paths: список путей к файлам ВНД.
        analyze_result_path: путь к кэшированному JSON результата analyze
            (для улучшения формулировки violation перед map-фазой).
        analyze_result: готовый результат analyze (in-memory).
        estimate_only: только оценка без LLM-вызовов.
        confirm: подтверждение для длинных ВНД (пока не используется —
            estimate-only достаточно для UX).
        max_chunks: override safety net на число чанков.

    Returns:
        ``(json_result, report_text_or_None)``.
    """
    # Валидация ввода.
    if not vnd_paths:
        return _output.make_error(
            "Не указаны файлы ВНД", error_type="no_vnd"
        ), None

    # Если передан analyze_result_path — загрузить для нормализованной формулировки.
    normalized_violation = _resolve_violation_text(
        violation=violation,
        analyze_result=analyze_result,
        analyze_result_path=analyze_result_path,
    )
    if not normalized_violation or not normalized_violation.strip():
        normalized_violation = violation

    # Подготовка ВНД.
    try:
        bundle = prepare_vnd(
            vnd_paths=vnd_paths,
            violation=violation,
            max_chunks_per_file=max_chunks,
        )
    except VndInputError as exc:
        return _output.make_error(exc.message, error_type=exc.error_type), None

    # Estimate-only — без LLM.
    if estimate_only:
        return (
            {
                "status": "success",
                "data": {
                    "vnd_files": len(vnd_paths),
                    "vnd_chunks_total": len(bundle.chunks),
                    "cache_key": bundle.cache_key,
                    "size_estimate": bundle.size_estimate,
                    "top_k_candidates": TOP_K_CANDIDATES,
                    "min_relevance_score": MIN_RELEVANCE_SCORE,
                    "map_batches_planned": len(bundle.chunks),
                    "synthesis_llm_calls_planned": 1,
                },
            },
            None,
        )

    # === Map-фаза: по одному LLM-вызову на чанк ===
    try:
        template = load_prompt("search_chunk_system")
    except FileNotFoundError as exc:
        return _output.make_error(
            f"Не удалось загрузить промпт: {exc}",
            error_type="prompt_missing",
        ), None

    findings: list[dict[str, Any]] = []
    chunks_processed = 0
    chunks_failed = 0

    for vnd_chunk in bundle.chunks:
        chunks_processed += 1
        system = render_prompt(
            template,
            {
                "VIOLATION_TEXT": normalized_violation,
                "VND_FILE": vnd_chunk.source_file,
                "VND_SECTION": vnd_chunk.section_title
                or vnd_chunk.section_path
                or "(без заголовка)",
                "CHUNK_TEXT": _truncate(vnd_chunk.text, max_chars=8000),
            },
        )
        try:
            parsed = call_llm_json(
                system=system,
                user=f"Отклонение: {normalized_violation}",
                operation="search_map",
            )
            finding = _normalize_finding(parsed, vnd_chunk=vnd_chunk)
        except (JsonParseError, Exception) as exc:  # noqa: BLE001
            # Один неудачный чанк не должен ронять весь прогон.
            chunks_failed += 1
            continue

        if finding is None:
            chunks_failed += 1
            continue

        # Отбрасываем нерелевантные.
        if finding["relevance_score"] < MIN_RELEVANCE_SCORE:
            continue

        findings.append(finding)

    # === Re-rank: сортировка по score, top-K ===
    findings.sort(key=lambda f: f["relevance_score"], reverse=True)
    top_findings = findings[:TOP_K_CANDIDATES]

    return (
        {
            "status": "success",
            "data": {
                "vnd_findings": top_findings,
                "vnd_chunks_total": len(bundle.chunks),
                "chunks_processed": chunks_processed,
                "chunks_failed": chunks_failed,
                "chunks_relevant_total": len(findings),
                "top_k_candidates": TOP_K_CANDIDATES,
                "min_relevance_score": MIN_RELEVANCE_SCORE,
                "cache_key": bundle.cache_key,
                "normalized_violation_used": normalized_violation,
            },
        },
        None,
    )


def _resolve_violation_text(
    *,
    violation: str,
    analyze_result: dict[str, Any] | None,
    analyze_result_path: str | None,
) -> str:
    """Получить нормализованную формулировку из analyze_result (если есть).

    Приоритет:
    1. ``analyze_result`` (in-memory, передан из `_run_all`).
    2. ``analyze_result_path`` (загрузить из файла).
    3. Исходный ``violation`` (fallback).
    """
    candidate: dict[str, Any] | None = analyze_result
    if candidate is None and analyze_result_path:
        try:
            import json as _json

            candidate = _json.loads(
                Path(analyze_result_path).read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            candidate = None

    if isinstance(candidate, dict):
        data = candidate.get("data") or {}
        normalized = data.get("normalized")
        if isinstance(normalized, str) and normalized.strip():
            return normalized
    return violation


def _normalize_finding(
    parsed: dict[str, Any],
    *,
    vnd_chunk: Any,
) -> dict[str, Any] | None:
    """Привести ответ LLM по одному чанку к финальному формату."""
    if not isinstance(parsed, dict):
        return None

    relation_type = str(parsed.get("relation_type") or "").strip()
    if relation_type not in _ALLOWED_RELATION_TYPES:
        # Не валидный тип — попробуем угадать по score.
        relation_type = "контекст"

    try:
        score = float(parsed.get("relevance_score"))
    except (TypeError, ValueError):
        return None
    # clamp 0..1
    score = max(0.0, min(1.0, score))

    why = parsed.get("why_matches") or ""
    if not isinstance(why, str):
        why = str(why)

    return {
        "source_file": vnd_chunk.source_file,
        "chunk_index": vnd_chunk.index,
        "section_title": vnd_chunk.section_title,
        "section_path": vnd_chunk.section_path,
        "text_excerpt": _truncate(vnd_chunk.text, max_chars=2000),
        "relation_type": relation_type,
        "relevance_score": round(score, 3),
        "why_matches": why.strip(),
    }


def _truncate(text: str, *, max_chars: int) -> str:
    """Обрезать текст с маркером ``[TRUNCATED]``, если он длиннее ``max_chars``."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n\n[TRUNCATED]"
