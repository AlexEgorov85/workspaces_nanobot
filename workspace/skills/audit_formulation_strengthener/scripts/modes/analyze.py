"""Режим ``analyze`` — нормализация формулировки отклонения (1 LLM-вызов).

Реализация этапа 4. Использует:

* ``scripts.prompts.load_prompt("analyze_system")`` для загрузки
  system-промпта из ``prompts/analyze_system.md``;
* ``scripts.prompts.render_prompt(...)`` для подстановки ``{{VIOLATION_TEXT}}``;
* ``scripts.llm.call_llm_json(...)`` для выполнения LLM-вызова с
  парсингом JSON и retry (до ``max_retries`` попыток).

Контракт результата:

```json
{
  "status": "success",
  "data": {
    "raw_text": "<исходная формулировка>",
    "normalized": "<нормализованная формулировка>",
    "key_concepts": ["<концепт 1>", ...],
    "severity": "высокая| средняя|низкая",
    "suggested_vnd_sections": ["<раздел 1>", ...],
    "llm_attempts": <int>
  }
}
```

При ошибках возвращается ``{"status": "error", "data": {message, error_type}}``.
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

import output as _output  # type: ignore[import-not-found]  # noqa: E402


__all__ = ["run"]


_ALLOWED_SEVERITY = {"высокая", "средняя", "низкая"}


def run(
    *,
    violation: str,
    vnd_paths: list[str] | None = None,
    estimate_only: bool = False,
) -> tuple[dict[str, Any], str | None]:
    """Нормализовать формулировку отклонения.

    Args:
        violation: текст отклонения, сформулированный аудитором.
        vnd_paths: не используется в этом режиме (нужен для совместимости
            сигнатуры с остальными режимами).
        estimate_only: если True — вернуть оценку без LLM-вызова
            (только число символов и планируемое число вызовов).

    Returns:
        ``(json_result, report_text_or_None)``.
        ``report_text=None`` — этот режим не формирует человекочитаемый отчёт.
    """
    if not violation or not violation.strip():
        return _output.make_error(
            "Пустой текст отклонения",
            error_type="empty_violation",
        ), None

    if estimate_only:
        return (
            {
                "status": "success",
                "data": {
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
        return _output.make_error(
            f"Не удалось загрузить промпт: {exc}",
            error_type="prompt_missing",
        ), None

    system = render_prompt(template, {"VIOLATION_TEXT": violation.strip()})

    # 2. LLM-вызов с парсингом JSON и retry.
    try:
        parsed = call_llm_json(system=system, user=violation.strip(), operation="analyze")
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

    # 3. Валидация ответа и нормализация в финальный dict.
    data = _validate_and_normalize(parsed, raw_text=violation)
    if data is None:
        return _output.make_error(
            "LLM вернул JSON, не соответствующий ожидаемой схеме",
            error_type="schema_mismatch",
        ), None

    return {"status": "success", "data": data}, None


def _validate_and_normalize(
    parsed: dict[str, Any],
    *,
    raw_text: str,
) -> dict[str, Any] | None:
    """Привести ответ LLM к финальному формату и валидировать.

    Ожидаемые поля: ``normalized``, ``key_concepts``, ``severity``,
    ``suggested_vnd_sections``. Допускаем дополнительные поля от LLM
    (например, ``notes``) — кладём в ``extras``.
    """
    if not isinstance(parsed, dict):
        return None

    normalized = parsed.get("normalized")
    if not isinstance(normalized, str) or not normalized.strip():
        return None

    key_concepts = parsed.get("key_concepts") or []
    if not isinstance(key_concepts, list):
        return None
    key_concepts = [str(c) for c in key_concepts if isinstance(c, (str, int, float))]

    severity = parsed.get("severity") or "средняя"
    severity = str(severity).strip().lower()
    if severity not in _ALLOWED_SEVERITY:
        severity = "средняя"

    suggested = parsed.get("suggested_vnd_sections") or []
    if not isinstance(suggested, list):
        suggested = []
    suggested = [str(s) for s in suggested if isinstance(s, (str, int, float))]

    # Дополнительные поля (например, ``notes``, ``assumptions``) — без потерь.
    known = {"normalized", "key_concepts", "severity", "suggested_vnd_sections"}
    extras = {k: v for k, v in parsed.items() if k not in known}

    return {
        "raw_text": raw_text,
        "normalized": normalized.strip(),
        "key_concepts": key_concepts,
        "severity": severity,
        "suggested_vnd_sections": suggested,
        **({"extras": extras} if extras else {}),
    }
