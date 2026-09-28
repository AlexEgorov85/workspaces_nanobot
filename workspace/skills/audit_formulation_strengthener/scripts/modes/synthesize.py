"""Режим ``synthesize`` — финальный отчёт по усилению формулировки (1 LLM-вызов).

Использует:

* ``prompts.synthesize_system`` — system-промпт с шаблонами
  ``{{NORMALIZED_VIOLATION}}, {{SEVERITY}}, {{KEY_CONCEPTS_JSON}}, {{EVIDENCE_JSON}}``;
* ``scripts.llm.chat_json`` — LLM-вызов;
* ``scripts.report.*`` — renderers (.md/.docx/.txt).

**Валидация цитат (пункт 6 ревью):**

* ``evidence_id`` должен быть в реестре находок (F1..FN).
* ``excerpt`` от LLM должен быть подстрокой ``text_excerpt`` находки
  (сравнение после ``.strip()`` обеих сторон, **без whitespace-collapse** —
  иначе ломается гарантия точности).
* ``relation_type`` берётся из находки (не из LLM — LLM возвращает только
  ``relation_explanation``).

Отброшенные цитаты инкрементируют ``citations_dropped`` и попадают в отчёт
строкой «Отклонено проверкой: N цитат не прошли сверку с источником».
Если все отброшены — статус всё равно ``success`` (с предупреждением).

``estimate-only`` делает parse+chunk ВНД ради ``N`` (0 LLM-вызовов,
но I/O происходит). Это ожидаемое поведение — фиксируется в ``contracts.md``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
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

from workspace.skills.audit_formulation_strengthener.scripts.report.docx_render import (
    write_docx as _write_docx,
)
from workspace.skills.audit_formulation_strengthener.scripts.report.markdown import (
    render_markdown as _render_markdown,
)
from workspace.skills.audit_formulation_strengthener.scripts.report.plain import (
    render_plain as _render_plain,
)


__all__ = ["run"]


_ALLOWED_RELATIONS = frozenset({
    "прямое_противоречие",
    "прямое_подтверждение",
    "косвенное_отношение",
    "контекст",
})
_ALLOWED_CATEGORIES = frozenset({"высокая", "средняя", "низкая"})

# Длина excerpt в evidence registry для промпта.
_EVIDENCE_EXCERPT_MAX_CHARS = 1500


def run(
    *,
    violation: str,
    vnd_paths: list[str] | None = None,
    analyze_result: dict[str, Any] | None = None,
    search_result: dict[str, Any] | None = None,
    analyze_result_path: str | None = None,
    search_result_path: str | None = None,
    output_format: str = "md",
    output_path: str | None = None,
    estimate_only: bool = False,
    max_chunks: int | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Синтезировать финальный отчёт.

    Args:
        violation: текст отклонения (fallback, если нет analyze_result).
        vnd_paths: список путей ВНД (для estimate-only: нужно для N).
        analyze_result: in-memory JSON результата analyze.
        search_result: in-memory JSON результата search.
        analyze_result_path: путь к кэшированному JSON результата analyze.
        search_result_path: путь к кэшированному JSON результата search.
        output_format: ``"md" | "txt" | "docx"`` для записи в ``output_path``.
        output_path: путь для сохранения отчёта (None → не сохранять).
        estimate_only: только оценка без LLM.
        max_chunks: override ``execution.max_chunks_for_execution``.

    Returns:
        ``(json_result, report_text_or_None)`` где ``report_text`` —
        markdown-рендер отчёта (или None для estimate-only).
    """
    # Загрузить данные из файлов (если in-memory не передан). Ошибки
    # чтения возвращаем сразу — это не тихая потеря.
    analyze_data, analyze_err = _load_resume(
        inline=analyze_result,
        path=analyze_result_path,
        kind="analyze",
    )
    if analyze_err is not None:
        return analyze_err, None

    search_data, search_err = _load_resume(
        inline=search_result,
        path=search_result_path,
        kind="search",
    )
    if search_err is not None:
        return search_err, None

    # estimate-only: parse+chunk ВНД ради N. Без vnd_paths — no_vnd
    # (нельзя посчитать 1+N+1 без N).
    if estimate_only:
        if not vnd_paths:
            return (
                make_error(
                    "Для estimate-only в режиме synthesize нужны --vnd",
                    error_type="no_vnd",
                ),
                None,
            )
        try:
            bundle = prepare_vnd(vnd_paths=vnd_paths, max_chunks=max_chunks)
        except VndInputError as exc:
            return make_error(exc.message, error_type=exc.error_type), None
        chunks_total = bundle.size_estimate["chunks_total"]
        return (
            {
                "status": "success",
                "data": {
                    "estimate": True,
                    "violation_chars": len(violation),
                    "size_estimate": bundle.size_estimate,
                    "llm_calls_planned": 1 + chunks_total + 1,
                },
            },
            None,
        )

    # Нормализованная формулировка — приоритет у analyze, иначе violation.
    normalized = (
        (analyze_data or {}).get("data", {}).get("normalized")
        if analyze_data
        else None
    )
    if not normalized:
        normalized = violation

    severity = (
        (analyze_data or {}).get("data", {}).get("severity")
        if analyze_data
        else None
    ) or "средняя"
    severity = str(severity).lower().strip()
    if severity not in _ALLOWED_CATEGORIES:
        severity = "средняя"

    key_concepts = (
        (analyze_data or {}).get("data", {}).get("key_concepts", [])
        if analyze_data
        else []
    )
    if not isinstance(key_concepts, list):
        key_concepts = []

    vnd_findings: list[dict[str, Any]] = []
    if search_data and isinstance(search_data, dict):
        vnd_findings = (search_data.get("data") or {}).get("vnd_findings") or []
    if not isinstance(vnd_findings, list):
        vnd_findings = []

    # Загрузить промпт.
    try:
        template = load_prompt("synthesize_system")
    except FileNotFoundError as exc:
        return (
            make_error(
                f"Не удалось загрузить промпт: {exc}",
                error_type="prompt_missing",
            ),
            None,
        )

    # Сформировать evidence registry для промпта.
    evidence_registry = _build_evidence_registry(vnd_findings)

    try:
        system = render_prompt(
            template,
            NORMALIZED_VIOLATION=normalized,
            SEVERITY=severity,
            KEY_CONCEPTS_JSON=json.dumps(key_concepts, ensure_ascii=False, indent=2),
            EVIDENCE_JSON=json.dumps(
                evidence_registry, ensure_ascii=False, indent=2
            ),
        )
    except PromptUnresolvedVarError as exc:
        return (
            make_error(
                f"Неразрешённые плейсхолдеры в промпте: {exc.unresolved}",
                error_type="prompt_unresolved_var",
            ),
            None,
        )

    # LLM-вызов.
    try:
        parsed = chat_json(
            system=system,
            user=f"Отклонение: {normalized}",
            operation="synthesize",
        )
    except JsonParseError as exc:
        return (
            make_error(
                f"LLM вернул невалидный JSON после 2 попыток: {exc}",
                error_type="json_parse_failed",
            ),
            None,
        )
    except Exception as exc:  # noqa: BLE001
        return (
            make_error(f"Ошибка LLM-вызова: {exc!r}", error_type="llm_error"),
            None,
        )

    # Валидация цитат против реестра находок.
    valid_citations, dropped_count = _validate_citations(
        parsed_citations=parsed.get("vnd_citations") if isinstance(parsed, dict) else None,
        evidence_registry=evidence_registry,
    )

    # Сборка финального dict.
    data = _assemble_data(
        parsed=parsed,
        normalized_violation=normalized,
        severity=severity,
        valid_citations=valid_citations,
        source_findings_count=len(evidence_registry),
        citations_dropped=dropped_count,
    )
    if data is None:
        return (
            make_error(
                "LLM вернул JSON, не соответствующий ожидаемой схеме",
                error_type="schema_mismatch",
            ),
            None,
        )

    data["date_iso"] = datetime.now(timezone.utc).isoformat()

    # Рендер markdown (всегда для stdout).
    md_text = _render_markdown(data)

    # Сохранение в файл (если указан output_path).
    saved_to: str | None = None
    if output_path:
        ext = (output_format or "md").lower()
        target = Path(output_path)
        if target.suffix.lstrip(".").lower() != ext:
            target = target.with_suffix(f".{ext}")
        try:
            if ext == "md":
                target.write_text(md_text, encoding="utf-8")
            elif ext == "txt":
                target.write_text(_render_plain(data), encoding="utf-8")
            elif ext == "docx":
                try:
                    _write_docx(data, target)
                except RuntimeError as exc:
                    return (
                        make_error(str(exc), error_type="io_error"),
                        None,
                    )
            else:
                target.write_text(md_text, encoding="utf-8")
            saved_to = str(target)
        except OSError as exc:
            return (
                make_error(
                    f"Не удалось записать отчёт в {target}: {exc}",
                    error_type="io_error",
                ),
                None,
            )

    result: dict[str, Any] = {"status": "success", "data": data}
    if saved_to:
        result["saved_to"] = saved_to

    return result, md_text


def _load_resume(
    *,
    inline: dict[str, Any] | None,
    path: str | None,
    kind: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Загрузить JSON из in-memory или из файла.

    Returns:
        ``(data_or_None, error_or_None)``.
    """
    if inline:
        return inline, None
    if path:
        try:
            return json.loads(Path(path).read_text(encoding="utf-8")), None
        except FileNotFoundError:
            return None, make_error(
                f"Файл результата {kind} не найден: {path}",
                error_type=f"{kind}_result_unreadable",
            )
        except (OSError, ValueError) as exc:
            return None, make_error(
                f"Не удалось прочитать файл результата {kind} '{path}': {exc!r}",
                error_type=f"{kind}_result_unreadable",
            )
    return None, None


def _build_evidence_registry(
    findings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Сформировать evidence registry F1..FN для промпта.

    Каждая запись содержит ``evidence_id``, ``source_file``, ``chunk_index``,
    ``excerpt`` (≤1500 симв. с маркером) и ``relation_type``.
    """
    out: list[dict[str, Any]] = []
    for i, f in enumerate(findings, 1):
        if not isinstance(f, dict):
            continue
        evidence_id = str(f.get("evidence_id") or f"F{i}")
        source_file = str(f.get("source_file") or "")
        chunk_index = f.get("chunk_index")
        excerpt_raw = str(f.get("text_excerpt") or "")
        excerpt = _truncate_evidence(excerpt_raw, max_chars=_EVIDENCE_EXCERPT_MAX_CHARS)
        relation_type = str(f.get("relation_type") or "контекст")
        if relation_type not in _ALLOWED_RELATIONS:
            relation_type = "контекст"
        out.append({
            "evidence_id": evidence_id,
            "source_file": source_file,
            "chunk_index": chunk_index,
            "excerpt": excerpt,
            "relation_type": relation_type,
        })
    return out


def _truncate_evidence(text: str, *, max_chars: int) -> str:
    """Обрезать excerpt для evidence registry (≤1500 симв. с маркером)."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n\n[ФРАГМЕНТ ОБРЕЗАН: показаны первые {max_chars} символов]"


def _validate_citations(
    *,
    parsed_citations: Any,
    evidence_registry: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Валидировать цитаты LLM против реестра находок.

    Правила:
    1. ``evidence_id`` должен быть в реестре.
    2. ``excerpt`` (после ``.strip()`` обеих сторон, БЕЗ whitespace-collapse)
       должен быть подстрокой excerpt-а находки.
    3. ``relation_type`` берётся из находки (не из LLM).

    Returns:
        ``(valid_citations, dropped_count)``.
    """
    registry_by_id: dict[str, dict[str, Any]] = {
        e["evidence_id"]: e for e in evidence_registry
    }

    valid: list[dict[str, Any]] = []
    dropped = 0

    if not isinstance(parsed_citations, list):
        return [], 0

    for cit in parsed_citations:
        if not isinstance(cit, dict):
            dropped += 1
            continue
        eid = str(cit.get("evidence_id") or "").strip()
        finding = registry_by_id.get(eid)
        if finding is None:
            dropped += 1
            continue
        excerpt_llm = (str(cit.get("excerpt") or "")).strip()
        excerpt_finding = (str(finding.get("excerpt") or "")).strip()
        if not excerpt_llm or excerpt_llm not in excerpt_finding:
            dropped += 1
            continue
        explanation = str(cit.get("relation_explanation") or "").strip()
        valid.append({
            "evidence_id": eid,
            "source_file": finding["source_file"],
            "chunk_index": finding["chunk_index"],
            "excerpt": excerpt_llm,
            "relation_type": finding["relation_type"],
            "relation_explanation": explanation,
        })

    return valid, dropped


def _assemble_data(
    *,
    parsed: Any,
    normalized_violation: str,
    severity: str,
    valid_citations: list[dict[str, Any]],
    source_findings_count: int,
    citations_dropped: int,
) -> dict[str, Any] | None:
    """Собрать финальный dict данных отчёта из ответа LLM."""
    if not isinstance(parsed, dict):
        return None

    title = str(parsed.get("title") or "Анализ отклонения").strip()
    if not title:
        title = "Анализ отклонения"

    violation_summary = _ensure_str_list(parsed.get("violation_summary"))
    established_facts = _ensure_str_list(parsed.get("established_facts"))
    deviation_analysis = _ensure_str_list(parsed.get("deviation_analysis"))
    recommended_formulation = _ensure_str_list(parsed.get("recommended_formulation"))
    verdict = _normalize_verdict(parsed.get("verdict"), fallback_severity=severity)

    return {
        "title": title,
        "violation_summary": violation_summary,
        "established_facts": established_facts,
        "deviation_analysis": deviation_analysis,
        "vnd_citations": valid_citations,
        "citations_dropped": citations_dropped,
        "verdict": verdict,
        "recommended_formulation": recommended_formulation,
        "normalized_violation": normalized_violation,
        "severity": severity,
        "source_findings_count": source_findings_count,
    }


def _normalize_verdict(
    value: Any,
    *,
    fallback_severity: str,
) -> dict[str, Any]:
    """Нормализовать verdict от LLM."""
    if not isinstance(value, dict):
        value = {}
    category = str(value.get("category") or fallback_severity).lower().strip()
    if category not in _ALLOWED_CATEGORIES:
        category = fallback_severity
    return {
        "category": category,
        "verdict_text": _ensure_str_list(value.get("verdict_text")),
    }


def _ensure_str_list(value: Any) -> list[str]:
    """Привести значение к списку непустых строк."""
    if value is None:
        return []
    if isinstance(value, str):
        v = value.strip()
        return [v] if v else []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if isinstance(item, str):
                s = item.strip()
                if s:
                    out.append(s)
            elif item is not None:
                s = str(item).strip()
                if s:
                    out.append(s)
        return out
    s = str(value).strip()
    return [s] if s else []
