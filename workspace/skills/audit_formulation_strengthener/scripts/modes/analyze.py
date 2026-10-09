"""Режим ``analyze`` — нормализация формулировки отклонения (1 LLM-вызов).

Использует:

* ``prompts.analyze_system`` — system-промпт с шаблоном ``{{VIOLATION_TEXT}}``;
* ``scripts.llm.chat_json`` — LLM-вызов с парсингом JSON и одной повторной
  попыткой при невалидном ответе.

Контракт результата:

```json
{
  "status": "success",
  "data": {
    "raw_text": "<исходная формулировка>",
    "normalized": "<нормализованная формулировка>",
    "key_concepts": ["<концепт 1>", ...],
    "severity": "высокая" | "средняя" | "низкая",
    "suggested_vnd_sections": ["<раздел 1>", ...]
  }
}
```

При ошибках: ``{"status": "error", "data": {"message", "error_type"}}``.
"""

from __future__ import annotations

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


__all__ = ["run"]


_ALLOWED_SEVERITY = frozenset({"высокая", "средняя", "низкая"})


def run(
    *,
    violation: str,
    vnd_paths: list[str] | None = None,
    estimate_only: bool = False,
) -> tuple[dict[str, Any], str | None]:
    """Нормализовать формулировку отклонения.

    Args:
        violation: текст отклонения, сформулированный аудитором.
        vnd_paths: не используется (для совместимости сигнатуры с search/synthesize).
        estimate_only: вернуть оценку без LLM-вызова.

    Returns:
        ``(json_result, report_text_or_None)``.
        ``report_text=None`` — этот режим не формирует человекочитаемый отчёт.
    """
    if not violation or not violation.strip():
        return (
            make_error("Пустой текст отклонения", error_type="empty_violation"),
            None,
        )

    if estimate_only:
        return (
            {
                "status": "success",
                "data": {
                    "estimate": True,
                    "violation_chars": len(violation),
                    "vnd_count": len(vnd_paths) if vnd_paths else 0,
                    "llm_calls_planned": 1,
                },
            },
            None,
        )

    # 1. Загрузить system-промпт.
    try:
        template = load_prompt("analyze_system")
    except FileNotFoundError as exc:
        return (
            make_error(
                f"Не удалось загрузить промпт: {exc}",
                error_type="prompt_missing",
            ),
            None,
        )

    try:
        system = render_prompt(template, VIOLATION_TEXT=violation.strip())
    except PromptUnresolvedVarError as exc:
        return (
            make_error(
                f"Неразрешённые плейсхолдеры в промпте: {exc.unresolved}",
                error_type="prompt_unresolved_var",
            ),
            None,
        )

    # 2. LLM-вызов с парсингом JSON и retry.
    try:
        parsed = chat_json(
            system=system,
            user=violation.strip(),
            operation="analyze",
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

    # 3. Валидация и нормализация.
    data = _validate_and_normalize(parsed, raw_text=violation)
    if data is None:
        return (
            make_error(
                "LLM вернул JSON, не соответствующий ожидаемой схеме",
                error_type="schema_mismatch",
            ),
            None,
        )

    return {"status": "success", "data": data}, None


def _validate_and_normalize(
    parsed: dict[str, Any],
    *,
    raw_text: str,
) -> dict[str, Any] | None:
    """Привести ответ LLM к финальному формату AnalyzeData."""
    if not isinstance(parsed, dict):
        return None

    normalized = parsed.get("normalized")
    if not isinstance(normalized, str) or not normalized.strip():
        return None

    key_concepts_raw = parsed.get("key_concepts") or []
    if not isinstance(key_concepts_raw, list):
        key_concepts_raw = []
    key_concepts = [
        str(c).strip()
        for c in key_concepts_raw
        if isinstance(c, (str, int, float)) and str(c).strip()
    ]

    severity_raw = parsed.get("severity") or "средняя"
    severity = str(severity_raw).strip().lower()
    if severity not in _ALLOWED_SEVERITY:
        severity = "средняя"

    suggested_raw = parsed.get("suggested_vnd_sections") or []
    if not isinstance(suggested_raw, list):
        suggested_raw = []
    suggested = [
        str(s).strip()
        for s in suggested_raw
        if isinstance(s, (str, int, float)) and str(s).strip()
    ]

    return {
        "raw_text": raw_text,
        "normalized": normalized.strip(),
        "key_concepts": key_concepts,
        "severity": severity,
        "suggested_vnd_sections": suggested,
    }
