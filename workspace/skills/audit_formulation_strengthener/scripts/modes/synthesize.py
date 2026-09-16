"""Режим ``synthesize`` — финальный отчёт по усилению формулировки.

Реализация этапа 6: один LLM-вызов с промптом
``prompts/synthesize_system.md`` возвращает структурированный отчёт в
строгом русском юридическом стиле.

Контракт результата:

```json
{
  "status": "success",
  "data": {
    "title": "...",
    "violation_summary": ["...", "..."],
    "established_facts": ["..."],
    "deviation_analysis": ["..."],
    "vnd_citations": [
      {"source_file": "...", "section_title": "...", "excerpt": "...",
       "relation_type": "...", "relation_explanation": "..."}
    ],
    "verdict": {"category": "высокая| средняя|низкая", "verdict_text": ["..."]},
    "recommended_formulation": ["...", "..."],
    "date_iso": "2026-09-16T..."
  },
  "_rendered": {
    "md": "...",
    "txt": "...",
    "docx_path": "..."
  }
}
```

Этот режим возвращает ``report_text`` (markdown-рендер отчёта) и
опционально записывает ``--output`` в файл в выбранном формате.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Добавляем scripts/ skill'а в sys.path.
_SKILL_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from llm import JsonParseError, call_llm_json  # type: ignore[import-not-found]  # noqa: E402
from prompts import load_prompt, render_prompt  # type: ignore[import-not-found]  # noqa: E402

import output as _output  # type: ignore[import-not-found]  # noqa: E402


__all__ = ["run"]


_ALLOWED_RELATIONS = frozenset({
    "прямое_противоречие",
    "прямое_подтверждение",
    "косвенное_отношение",
    "контекст",
    "нерелевантно",
})
_ALLOWED_CATEGORIES = frozenset({"высокая", "средняя", "низкая"})


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
) -> tuple[dict[str, Any], str | None]:
    """Синтезировать финальный отчёт.

    Args:
        violation: текст отклонения (fallback, если нет analyze).
        vnd_paths: список путей ВНД (для traceability в отчёте).
        analyze_result: in-memory JSON результата analyze.
        search_result: in-memory JSON результата search.
        analyze_result_path: путь к кэшированному JSON результата analyze.
        search_result_path: путь к кэшированному JSON результата search.
        output_format: ``"md"`` | ``"txt"`` | ``"docx"``.
        output_path: путь для сохранения (если None — не сохранять).
        estimate_only: только оценка без LLM.

    Returns:
        ``(json_result, report_text_or_None)`` где ``report_text`` —
        markdown-рендер отчёта (для CLI).
    """
    # Загрузить данные из файлов, если in-memory не передан.
    analyze_data = _load_json(analyze_result, analyze_result_path)
    search_data = _load_json(search_result, search_result_path)

    if estimate_only:
        return (
            {
                "status": "success",
                "data": {
                    "mode": "synthesize",
                    "vnd_findings_available": len(
                        (search_data or {}).get("data", {}).get("vnd_findings", [])
                    ),
                    "llm_calls_planned": 1,
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
        (analyze_data or {}).get("data", {}).get("severity") or "средняя"
        if analyze_data
        else "средняя"
    )
    severity = str(severity).lower()
    if severity not in _ALLOWED_CATEGORIES:
        severity = "средняя"

    key_concepts = (
        (analyze_data or {}).get("data", {}).get("key_concepts", [])
        if analyze_data
        else []
    )

    vnd_findings: list[dict[str, Any]] = []
    if search_data and isinstance(search_data, dict):
        vnd_findings = (search_data.get("data") or {}).get("vnd_findings") or []
    if not isinstance(vnd_findings, list):
        vnd_findings = []

    # Загрузить промпт.
    try:
        template = load_prompt("synthesize_system")
    except FileNotFoundError as exc:
        return _output.make_error(
            f"Не удалось загрузить промпт: {exc}",
            error_type="prompt_missing",
        ), None

    system = render_prompt(
        template,
        {
            "NORMALIZED_VIOLATION": normalized,
            "SEVERITY": severity,
            "KEY_CONCEPTS_JSON": json.dumps(
                key_concepts, ensure_ascii=False, indent=2
            ),
            "VND_FINDINGS_JSON": json.dumps(
                _prepare_findings_for_prompt(vnd_findings),
                ensure_ascii=False,
                indent=2,
            ),
        },
    )

    # LLM-вызов.
    try:
        parsed = call_llm_json(
            system=system,
            user=f"Отклонение: {normalized}",
            operation="synthesize",
        )
    except JsonParseError as exc:
        return _output.make_error(
            f"LLM вернул невалидный JSON после всех попыток: {exc}",
            error_type="json_parse_failed",
        ), None
    except Exception as exc:  # noqa: BLE001
        return _output.make_error(
            f"Ошибка LLM-вызова: {exc!r}",
            error_type="llm_error",
        ), None

    # Нормализация ответа.
    data = _normalize_synthesize_payload(
        parsed,
        normalized_violation=normalized,
        severity=severity,
        vnd_findings=vnd_findings,
    )
    if data is None:
        return _output.make_error(
            "LLM вернул JSON, не соответствующий ожидаемой схеме",
            error_type="schema_mismatch",
        ), None

    # Дата отчёта.
    data["date_iso"] = datetime.now(timezone.utc).isoformat()

    # Render markdown (этап 7 — renderers; пока простой inline-рендер).
    md_text = _render_markdown(data, violation=violation, vnd_paths=vnd_paths or [])

    # Сохранение в файл, если указан output_path.
    saved_to: str | None = None
    if output_path:
        ext = (output_format or "md").lower()
        target = Path(output_path)
        # Если output_path без расширения или не совпадает с форматом —
        # добавляем расширение.
        if target.suffix.lstrip(".").lower() != ext:
            target = target.with_suffix(f".{ext}")
        try:
            if ext == "md":
                target.write_text(md_text, encoding="utf-8")
            elif ext == "txt":
                target.write_text(_strip_markdown(md_text), encoding="utf-8")
            elif ext == "docx":
                _write_docx(target, data)
            else:
                target.write_text(md_text, encoding="utf-8")
            saved_to = str(target)
        except OSError as exc:
            return _output.make_error(
                f"Не удалось записать отчёт в {target}: {exc}",
                error_type="io_error",
            ), None

    result: dict[str, Any] = {
        "status": "success",
        "data": data,
    }
    if saved_to:
        result["saved_to"] = saved_to

    return result, md_text


# ============================================================================
# Helpers
# ============================================================================


def _load_json(
    inline: dict[str, Any] | None,
    path: str | None,
) -> dict[str, Any] | None:
    """Загрузить JSON из in-memory или из файла."""
    if inline:
        return inline
    if path:
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
    return None


def _prepare_findings_for_prompt(
    findings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Сократить findings для промпта (без полных текстов чанков)."""
    out: list[dict[str, Any]] = []
    for f in findings:
        if not isinstance(f, dict):
            continue
        out.append(
            {
                "source_file": str(f.get("source_file") or ""),
                "section_title": str(f.get("section_title") or ""),
                "section_path": str(f.get("section_path") or ""),
                "excerpt": str(f.get("text_excerpt") or "")[:1500],
                "relation_type": str(f.get("relation_type") or ""),
                "relevance_score": float(f.get("relevance_score") or 0.0),
                "why_matches": str(f.get("why_matches") or ""),
            }
        )
    return out


def _normalize_synthesize_payload(
    parsed: Any,
    *,
    normalized_violation: str,
    severity: str,
    vnd_findings: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Привести ответ LLM к финальному формату отчёта."""
    if not isinstance(parsed, dict):
        return None

    title = str(parsed.get("title") or "Анализ отклонения").strip()
    if not title:
        title = "Анализ отклонения"

    out: dict[str, Any] = {
        "title": title,
        "violation_summary": _ensure_str_list(parsed.get("violation_summary")),
        "established_facts": _ensure_str_list(parsed.get("established_facts")),
        "deviation_analysis": _ensure_str_list(parsed.get("deviation_analysis")),
        "vnd_citations": _normalize_citations(parsed.get("vnd_citations")),
        "verdict": _normalize_verdict(parsed.get("verdict"), fallback_severity=severity),
        "recommended_formulation": _ensure_str_list(parsed.get("recommended_formulation")),
        "normalized_violation": normalized_violation,
        "severity": severity,
        "source_findings_count": len(vnd_findings),
    }
    return out


def _ensure_str_list(value: Any) -> list[str]:
    """Привести значение к списку непустых строк."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                out.append(item)
            elif item is not None:
                s = str(item).strip()
                if s:
                    out.append(s)
        return out
    return [str(value)] if str(value).strip() else []


def _normalize_citations(value: Any) -> list[dict[str, Any]]:
    """Нормализовать citations от LLM."""
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for c in value:
        if not isinstance(c, dict):
            continue
        relation_type = str(c.get("relation_type") or "").strip()
        if relation_type not in _ALLOWED_RELATIONS:
            relation_type = "контекст"
        out.append(
            {
                "source_file": str(c.get("source_file") or ""),
                "section_title": str(c.get("section_title") or ""),
                "section_path": str(c.get("section_path") or ""),
                "excerpt": str(c.get("excerpt") or ""),
                "relation_type": relation_type,
                "relation_explanation": str(c.get("relation_explanation") or ""),
            }
        )
    return out


def _normalize_verdict(
    value: Any,
    *,
    fallback_severity: str,
) -> dict[str, Any]:
    """Нормализовать verdict."""
    if not isinstance(value, dict):
        value = {}
    category = str(value.get("category") or fallback_severity).lower()
    if category not in _ALLOWED_CATEGORIES:
        category = fallback_severity
    return {
        "category": category,
        "verdict_text": _ensure_str_list(value.get("verdict_text")),
    }


# ============================================================================
# Inline renderers (этап 7 будет вынесен в scripts/report/*)
# ============================================================================


def _render_markdown(
    data: dict[str, Any],
    *,
    violation: str,
    vnd_paths: list[str],
) -> str:
    """Собрать markdown-отчёт (этап 7 добавит docx/txt)."""
    lines: list[str] = []
    lines.append(f"# {data.get('title', 'Анализ отклонения')}")
    lines.append("")
    lines.append(f"**Дата:** {data.get('date_iso', '')}  ")
    lines.append(f"**Категория тяжести:** {data.get('severity', 'средняя')}  ")
    lines.append("")

    if vnd_paths:
        lines.append("**Источники ВНД:**")
        for p in vnd_paths:
            lines.append(f"- `{p}`")
        lines.append("")

    lines.append("## 1. Краткое изложение отклонения")
    lines.append("")
    for p in data.get("violation_summary", []):
        lines.append(p)
    lines.append("")

    lines.append("## 2. Установленные факты (по ВНД)")
    lines.append("")
    if data.get("established_facts"):
        for p in data["established_facts"]:
            lines.append(p)
            lines.append("")
    else:
        lines.append("_Не установлено релевантных фрагментов ВНД._")
        lines.append("")

    lines.append("## 3. Анализ отклонения")
    lines.append("")
    if data.get("deviation_analysis"):
        for p in data["deviation_analysis"]:
            lines.append(p)
            lines.append("")
    else:
        lines.append("_Недостаточно данных для анализа._")
        lines.append("")

    citations = data.get("vnd_citations", [])
    if citations:
        lines.append("## 4. Релевантные фрагменты ВНД")
        lines.append("")
        for i, c in enumerate(citations, 1):
            section = c.get("section_title") or "(без заголовка)"
            source = c.get("source_file") or "(неизвестный источник)"
            rel = c.get("relation_type") or "контекст"
            lines.append(f"### {i}. {section}")
            lines.append("")
            lines.append(f"**Источник:** `{source}`  ")
            lines.append(f"**Тип соотнесения:** {rel}  ")
            if c.get("section_path"):
                lines.append(f"**Путь по дереву:** {c['section_path']}  ")
            lines.append("")
            if c.get("excerpt"):
                lines.append(f"> {c['excerpt']}")
                lines.append("")
            if c.get("relation_explanation"):
                lines.append(c["relation_explanation"])
                lines.append("")

    verdict = data.get("verdict") or {}
    lines.append("## 5. Итоговая классификация")
    lines.append("")
    lines.append(f"**Категория:** {verdict.get('category', 'средняя')}  ")
    lines.append("")
    for p in verdict.get("verdict_text", []):
        lines.append(p)
        lines.append("")

    lines.append("## 6. Рекомендуемая усиленная формулировка")
    lines.append("")
    if data.get("recommended_formulation"):
        for p in data["recommended_formulation"]:
            lines.append(p)
            lines.append("")
    else:
        lines.append("_Не удалось сформулировать рекомендацию._")
        lines.append("")

    lines.append("---")
    lines.append(f"_Отчёт сгенерирован skill'ом `audit_formulation_strengthener`._")
    return "\n".join(lines)


def _strip_markdown(md: str) -> str:
    """Грубый markdown → plain text (для .txt)."""
    out: list[str] = []
    for line in md.splitlines():
        s = line.strip()
        if not s:
            out.append("")
            continue
        # Убираем заголовки #, ##, ###, ####
        while s.startswith("#"):
            s = s.lstrip("#").lstrip()
        # Убираем **bold**, *italic*
        s = s.replace("**", "").replace("__", "").replace("*", "")
        # Blockquote
        if s.startswith(">"):
            s = s.lstrip(">").lstrip()
        # Inline code
        s = s.replace("`", "")
        out.append(s)
    return "\n".join(out)


def _write_docx(target: Path, data: dict[str, Any]) -> None:
    """Сохранить отчёт в .docx (использует python-docx)."""
    try:
        from docx import Document  # type: ignore[import-not-found]
    except ImportError as exc:
        raise OSError(
            "python-docx не установлен; нельзя сохранить в .docx"
        ) from exc

    doc = Document()
    doc.add_heading(data.get("title", "Анализ отклонения"), level=1)

    p = doc.add_paragraph()
    p.add_run(f"Дата: {data.get('date_iso', '')}").bold = True
    p.add_run(f"  |  Категория тяжести: {data.get('severity', 'средняя')}")

    doc.add_heading("1. Краткое изложение отклонения", level=2)
    for line in data.get("violation_summary", []):
        doc.add_paragraph(line)

    doc.add_heading("2. Установленные факты (по ВНД)", level=2)
    if data.get("established_facts"):
        for line in data["established_facts"]:
            doc.add_paragraph(line)
    else:
        doc.add_paragraph("Не установлено релевантных фрагментов ВНД.")

    doc.add_heading("3. Анализ отклонения", level=2)
    if data.get("deviation_analysis"):
        for line in data["deviation_analysis"]:
            doc.add_paragraph(line)
    else:
        doc.add_paragraph("Недостаточно данных для анализа.")

    citations = data.get("vnd_citations", [])
    if citations:
        doc.add_heading("4. Релевантные фрагменты ВНД", level=2)
        for i, c in enumerate(citations, 1):
            doc.add_heading(f"{i}. {c.get('section_title') or '(без заголовка)'}", level=3)
            meta = doc.add_paragraph()
            meta.add_run("Источник: ").bold = True
            meta.add_run(str(c.get("source_file") or "(неизвестный источник)"))
            meta.add_run("  |  Тип соотнесения: ").bold = True
            meta.add_run(str(c.get("relation_type") or "контекст"))
            if c.get("excerpt"):
                doc.add_paragraph(f"«{c['excerpt']}»", style="Intense Quote")
            if c.get("relation_explanation"):
                doc.add_paragraph(c["relation_explanation"])

    verdict = data.get("verdict") or {}
    doc.add_heading("5. Итоговая классификация", level=2)
    p = doc.add_paragraph()
    p.add_run("Категория: ").bold = True
    p.add_run(str(verdict.get("category", "средняя")))
    for line in verdict.get("verdict_text", []):
        doc.add_paragraph(line)

    doc.add_heading("6. Рекомендуемая усиленная формулировка", level=2)
    if data.get("recommended_formulation"):
        for line in data["recommended_formulation"]:
            doc.add_paragraph(line)
    else:
        doc.add_paragraph("Не удалось сформулировать рекомендацию.")

    doc.save(str(target))
