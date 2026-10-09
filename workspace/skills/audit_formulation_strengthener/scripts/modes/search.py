"""Режим ``search`` — поиск релевантных фрагментов ВНД (map → фильтр → top-K).

Алгоритм:

1. ``prepare_vnd`` — извлечь все чанки через ``extract_text`` + ``split_text``.
2. **Map-фаза:** для каждого чанка — 1 LLM-вызов с промптом
   ``prompts/search_chunk_system.md`` (``{{VIOLATION_TEXT}}, {{VND_FILE}},
   {{CHUNK_TEXT}}``). LLM возвращает
   ``{relation_type, relevance_score, why_matches}``.
3. **Filter:** ``relevance_score < MIN_RELEVANCE_SCORE`` отбрасываются.
4. **Re-rank:** сортировка по score убывание, top-``TOP_K_CANDIDATES``.
5. Сквозные ``evidence_id`` ``"F1..FN"`` по successful+filtered.
6. Возвращаем JSON со списком ``vnd_findings`` + метаданными.

Без секций: цитата = ``(source_file, chunk_index, text_excerpt)``.
SHA-256 ключ по violation+paths не используется.

``--analyze-result`` опционален — если не задан, в LLM-промпт передаётся
сырой текст ``violation`` (без нормализации).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from workspace.skills.audit_formulation_strengthener.scripts.llm import (
    JsonParseError,
    chat_json,
)
from workspace.skills.audit_formulation_strengthener.scripts.output import make_error
from workspace.skills.audit_formulation_strengthener.scripts.prompts import (
    PromptUnresolvedVarError,
    load_prompt,
    render_prompt,
)
from workspace.skills.audit_formulation_strengthener.scripts.vnd_io import (
    VndInputError,
    prepare_vnd,
)


__all__ = ["run"]


# Skill-specific параметры (вне SkillSettings(extra="forbid")).
TOP_K_CANDIDATES: int = 10
MIN_RELEVANCE_SCORE: float = 0.3

# Единый whitelist relation_type для всего скилла (синхронизирован с synthesize).
_ALLOWED_RELATION_TYPES = frozenset({
    "прямое_противоречие",
    "прямое_подтверждение",
    "косвенное_отношение",
    "контекст",
})

# Обрезка чанка в search-промпте (символов). При превышении — хвост с маркером.
_CHUNK_PROMPT_MAX_CHARS = 8000
_EXCERPT_MAX_CHARS = 2000


def run(
    *,
    violation: str,
    vnd_paths: list[str],
    analyze_result: dict[str, Any] | None = None,
    analyze_result_path: str | None = None,
    estimate_only: bool = False,
    max_chunks: int | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Поиск релевантных фрагментов ВНД.

    Args:
        violation: текст отклонения (fallback, если нет analyze_result).
        vnd_paths: список путей к файлам ВНД.
        analyze_result: in-memory результат analyze (приоритет над path).
        analyze_result_path: путь к кэшированному JSON результата analyze.
        estimate_only: только оценка без LLM-вызовов.
        max_chunks: override ``execution.max_chunks_for_execution``.

    Returns:
        ``(json_result, report_text_or_None)``.
    """
    if not vnd_paths:
        return make_error("Не указаны файлы ВНД", error_type="no_vnd"), None

    # Если ни analyze_result, ни analyze_result_path не заданы —
    # fallback на сырой текст violation (без нормализации).
    normalized_violation, analyze_load_error = _resolve_violation_text(
        violation=violation,
        analyze_result=analyze_result,
        analyze_result_path=analyze_result_path,
    )
    if analyze_load_error is not None:
        return analyze_load_error, None
    if not normalized_violation or not normalized_violation.strip():
        normalized_violation = violation

    # Подготовка ВНД.
    try:
        bundle = prepare_vnd(vnd_paths=vnd_paths, max_chunks=max_chunks)
    except VndInputError as exc:
        return make_error(exc.message, error_type=exc.error_type), None

    chunks_total = len(bundle.chunks)

    # Estimate-only — без LLM.
    if estimate_only:
        return (
            {
                "status": "success",
                "data": {
                    "estimate": True,
                    "size_estimate": bundle.size_estimate,
                    "llm_calls_planned": chunks_total,  # только map-вызовы
                },
            },
            None,
        )

    # Загрузить промпт.
    try:
        template = load_prompt("search_chunk_system")
    except FileNotFoundError as exc:
        return (
            make_error(
                f"Не удалось загрузить промпт: {exc}",
                error_type="prompt_missing",
            ),
            None,
        )

    # Map-фаза: по одному LLM-вызову на чанк.
    findings: list[dict[str, Any]] = []
    chunks_processed = 0
    chunks_failed = 0

    for vnd_chunk in bundle.chunks:
        chunks_processed += 1
        chunk_text = _truncate(vnd_chunk.text, max_chars=_CHUNK_PROMPT_MAX_CHARS)
        try:
            system = render_prompt(
                template,
                VIOLATION_TEXT=normalized_violation,
                VND_FILE=vnd_chunk.source_file,
                CHUNK_TEXT=chunk_text,
            )
        except PromptUnresolvedVarError:
            chunks_failed += 1
            continue

        try:
            parsed = chat_json(
                system=system,
                user=f"Отклонение: {normalized_violation}",
                operation="search_map",
            )
            finding = _normalize_finding(parsed, vnd_chunk=vnd_chunk)
        except (JsonParseError, Exception):  # noqa: BLE001
            # Один неудачный чанк не должен ронять весь прогон.
            chunks_failed += 1
            continue

        if finding is None:
            chunks_failed += 1
            continue

        if finding["relevance_score"] < MIN_RELEVANCE_SCORE:
            continue

        findings.append(finding)

    # Если все чанки провалились на LLM — это ошибка llm_error,
    # а не частичный success с пустым списком находок.
    if chunks_processed > 0 and chunks_failed == chunks_processed:
        return (
            make_error(
                f"Все {chunks_processed} чанков ВНД провалились на LLM-вызове",
                error_type="llm_error",
            ),
            None,
        )

    # Filter + Re-rank.
    findings.sort(key=lambda f: f["relevance_score"], reverse=True)
    top_findings = findings[:TOP_K_CANDIDATES]

    # Сквозные evidence_id "F1..FN" по successful+filtered.
    for i, finding in enumerate(top_findings, 1):
        finding["evidence_id"] = f"F{i}"

    return (
        {
            "status": "success",
            "data": {
                "vnd_findings": top_findings,
                "vnd_chunks_total": chunks_total,
                "chunks_processed": chunks_processed,
                "chunks_failed": chunks_failed,
                "chunks_relevant_total": len(findings),
                "top_k_candidates": TOP_K_CANDIDATES,
                "min_relevance_score": MIN_RELEVANCE_SCORE,
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
) -> tuple[str | None, dict[str, Any] | None]:
    """Получить нормализованную формулировку из analyze_result.

    Returns:
        ``(normalized_text_or_None, error_or_None)``.
        Если ничего не задано → ``(None, None)`` — caller использует violation.
    """
    candidate: dict[str, Any] | None = analyze_result
    if candidate is None and analyze_result_path:
        try:
            candidate = json.loads(Path(analyze_result_path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None, make_error(
                f"Файл результата analyze не найден: {analyze_result_path}",
                error_type="analyze_result_unreadable",
            )
        except (OSError, ValueError) as exc:
            return None, make_error(
                f"Не удалось прочитать файл результата analyze "
                f"'{analyze_result_path}': {exc!r}",
                error_type="analyze_result_unreadable",
            )

    if isinstance(candidate, dict):
        data = candidate.get("data") or {}
        normalized = data.get("normalized")
        if isinstance(normalized, str) and normalized.strip():
            return normalized, None

    return None, None  # caller: fallback на violation


def _normalize_finding(
    parsed: Any,
    *,
    vnd_chunk: Any,
) -> dict[str, Any] | None:
    """Привести ответ LLM по одному чанку к финальному формату Finding."""
    if not isinstance(parsed, dict):
        return None

    relation_type = str(parsed.get("relation_type") or "").strip()
    if relation_type not in _ALLOWED_RELATION_TYPES:
        relation_type = "контекст"

    try:
        score = float(parsed.get("relevance_score"))
    except (TypeError, ValueError):
        return None
    score = max(0.0, min(1.0, score))

    why = parsed.get("why_matches") or ""
    if not isinstance(why, str):
        why = str(why)

    return {
        "source_file": vnd_chunk.source_file,
        "chunk_index": vnd_chunk.index,
        "text_excerpt": _truncate(vnd_chunk.text, max_chars=_EXCERPT_MAX_CHARS),
        "relation_type": relation_type,
        "relevance_score": round(score, 3),
        "why_matches": why.strip(),
    }


def _truncate(text: str, *, max_chars: int) -> str:
    """Обрезать текст с маркером, если он длиннее ``max_chars``.

    Маркер единый: ``\\n\\n[ФРАГМЕНТ ОБРЕЗАН: показаны первые <N> символов]``
    (для search-промпта) или ``\\n\\n[TRUNCATED]`` (для excerpt).
    """
    if len(text) <= max_chars:
        return text
    return (
        text[:max_chars]
        + f"\n\n[ФРАГМЕНТ ОБРЕЗАН: показаны первые {max_chars} символов]"
    )
