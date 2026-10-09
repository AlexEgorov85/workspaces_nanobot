"""Markdown-рендер финального отчёта (6 секций, единые с SKILL.md)."""

from __future__ import annotations

from typing import Any


__all__ = ["render_markdown"]


_EMPTY_SECTION = "*(секция не заполнена: {reason})*"


def render_markdown(data: dict[str, Any]) -> str:
    """Собрать markdown-отчёт из синтезированных данных.

    Args:
        data: dict, возвращённый ``modes.synthesize.run()``.

    Returns:
        Готовый markdown-текст.
    """
    title = str(data.get("title") or "Анализ отклонения").strip() or "Анализ отклонения"
    date_iso = str(data.get("date_iso") or "")
    severity = str(data.get("severity") or "средняя")
    citations = data.get("vnd_citations") or []
    findings_count = int(data.get("source_findings_count") or len(citations))
    dropped = int(data.get("citations_dropped") or 0)
    normalized = str(data.get("normalized_violation") or "").strip()

    lines: list[str] = []
    lines.append(f"# {title}")
    lines.append("")
    if date_iso:
        lines.append(f"**Дата:** {date_iso}  ")
    lines.append(f"**Категория тяжести:** {severity}  ")
    lines.append(
        f"**Валидированных доказательств:** {len(citations)} "
        f"(из {findings_count} кандидатов)"
    )
    if dropped:
        lines.append(
            f"**Отклонено проверкой:** {dropped} "
            f"цитат не прошли сверку с источником"
        )
    if normalized:
        lines.append("")
        lines.append(f"> **Нормализованная формулировка:** {normalized}")
    lines.append("")

    # 1. Краткое изложение отклонения
    lines.append("## 1. Краткое изложение отклонения")
    lines.append("")
    summary = data.get("violation_summary") or []
    if summary:
        for p in summary:
            lines.append(str(p))
            lines.append("")
    else:
        lines.append(_EMPTY_SECTION.format(reason="LLM не вернул violation_summary"))
        lines.append("")

    # 2. Установленные факты (по ВНД)
    lines.append("## 2. Установленные факты (по ВНД)")
    lines.append("")
    facts = data.get("established_facts") or []
    if facts:
        for p in facts:
            lines.append(str(p))
            lines.append("")
    else:
        lines.append(_EMPTY_SECTION.format(reason="релевантных фрагментов ВНД не найдено"))
        lines.append("")

    # 3. Анализ отклонения
    lines.append("## 3. Анализ отклонения")
    lines.append("")
    analysis = data.get("deviation_analysis") or []
    if analysis:
        for p in analysis:
            lines.append(str(p))
            lines.append("")
    else:
        lines.append(_EMPTY_SECTION.format(reason="недостаточно данных для анализа"))
        lines.append("")

    # 4. Релевантные фрагменты ВНД (валидированные цитаты)
    lines.append("## 4. Релевантные фрагменты ВНД (валидированные цитаты)")
    lines.append("")
    if citations:
        for i, c in enumerate(citations, 1):
            evidence_id = str(c.get("evidence_id") or f"F{i}")
            source = str(c.get("source_file") or "(неизвестный источник)")
            chunk_index = c.get("chunk_index")
            relation = str(c.get("relation_type") or "контекст")
            excerpt = str(c.get("excerpt") or "").strip()
            explanation = str(c.get("relation_explanation") or "").strip()

            heading = f"### {i}. [{evidence_id}] {source}"
            if chunk_index is not None:
                heading += f" — чанк {chunk_index}"
            lines.append(heading)
            lines.append("")
            lines.append(f"**Тип соотнесения:** {relation}  ")
            lines.append("")
            if excerpt:
                for ex_line in excerpt.splitlines() or [excerpt]:
                    lines.append(f"> {ex_line}")
                lines.append("")
            if explanation:
                lines.append(explanation)
                lines.append("")
    else:
        if findings_count == 0:
            lines.append(_EMPTY_SECTION.format(
                reason="релевантных фрагментов ВНД не найдено"
            ))
        else:
            lines.append(_EMPTY_SECTION.format(
                reason=f"все {findings_count} цитат отклонены проверкой"
            ))
        lines.append("")

    # 5. Итоговая классификация
    lines.append("## 5. Итоговая классификация")
    lines.append("")
    verdict = data.get("verdict") or {}
    category = str(verdict.get("category") or severity or "средняя")
    lines.append(f"**Категория:** {category}  ")
    lines.append("")
    verdict_text = verdict.get("verdict_text") or []
    if verdict_text:
        for p in verdict_text:
            lines.append(str(p))
            lines.append("")
    else:
        lines.append(_EMPTY_SECTION.format(reason="LLM не вернул verdict_text"))
        lines.append("")

    # 6. Рекомендуемая усиленная формулировка
    lines.append("## 6. Рекомендуемая усиленная формулировка")
    lines.append("")
    formulation = data.get("recommended_formulation") or []
    if formulation:
        for p in formulation:
            lines.append(str(p))
            lines.append("")
    else:
        lines.append(_EMPTY_SECTION.format(
            reason="LLM не вернул recommended_formulation"
        ))
        lines.append("")

    lines.append("---")
    lines.append("_Отчёт сгенерирован skill'ом `audit_formulation_strengthener`._")
    return "\n".join(lines)
