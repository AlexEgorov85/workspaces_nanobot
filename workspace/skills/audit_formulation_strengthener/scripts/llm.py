"""LLM-вызовы с single-flight защитой.

Skill использует одну точку вызова LLM — ``call_llm_json`` (или
``call_llm_text``), которая проходит через ``guarded_chat`` из
``legal_summarizer.scripts.llm.single_flight``. Это защищает от
параллельных вызовов в одном процессе и даёт единое место для retry
при невалидном JSON.

Не дублируем single-flight логику — импортируем из legal_summarizer
(это публичный паттерн, см. ``docs/skill-tool-architecture.md`` §1 —
skill и skill могут использовать общую инфраструктуру).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# Добавляем корень репо и scripts/legal_summarizer в sys.path, чтобы
# импортировать single_flight без выставленного PYTHONPATH.
_SKILL_ROOT = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _SKILL_ROOT.parents[1]
_LS_SCRIPTS = Path(_PROJECT_ROOT) / "workspace" / "skills" / "legal_summarizer" / "scripts"
for _p in (str(_PROJECT_ROOT), str(_LS_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from llm.single_flight import guarded_chat  # type: ignore[import-not-found]  # noqa: E402
from lib.services.llm_client import call_llm as _raw_call_llm  # type: ignore[import-not-found]  # noqa: E402

from skill_config import get_llm_config, get_max_retries  # type: ignore[import-not-found]  # noqa: E402


__all__ = ["call_llm_json", "call_llm_text", "JsonParseError"]


class JsonParseError(Exception):
    """LLM вернул ответ, который не удалось распарсить как JSON."""

    def __init__(self, message: str, raw_text: str, attempt: int) -> None:
        super().__init__(message)
        self.raw_text = raw_text
        self.attempt = attempt


def call_llm_text(
    *,
    system: str,
    user: str,
    operation: str = "afs",
) -> str:
    """Один LLM-вызов, возвращает текст ответа.

    Args:
        system: system-промпт.
        user: user-payload.
        operation: короткий идентификатор операции (для логов).

    Returns:
        Текст ответа LLM.
    """
    cfg = get_llm_config()
    return guarded_chat(
        _raw_call_llm,
        system=system,
        user=user,
        max_tokens=int(cfg.get("max_tokens", 4096)),
        temperature=float(cfg.get("temperature", 0.1)),
        operation=operation,
    )


def call_llm_json(
    *,
    system: str,
    user: str,
    operation: str = "afs",
) -> dict[str, Any]:
    """LLM-вызов с парсингом JSON-ответа и retry.

    До ``max_retries`` попыток. На каждой итерации LLM получает
    подсказку: «предыдущий ответ невалиден, верни корректный JSON».

    Args:
        system: system-промпт.
        user: user-payload.
        operation: короткий идентификатор операции (для логов).

    Returns:
        Распарсенный JSON (dict).

    Raises:
        JsonParseError: если все попытки исчерпаны.
    """
    max_attempts = max(1, get_max_retries())
    last_error: Exception | None = None
    last_text: str = ""
    current_user = user

    for attempt in range(1, max_attempts + 1):
        text = call_llm_text(system=system, user=current_user, operation=operation)
        last_text = text
        try:
            return _parse_json(text)
        except json.JSONDecodeError as exc:
            last_error = exc
            if attempt >= max_attempts:
                break
            current_user = (
                f"{user}\n\n---\nПредыдущий ответ невалиден как JSON: {exc!r}. "
                f"Верни только корректный JSON без пояснений."
            )

    raise JsonParseError(
        f"Не удалось распарсить JSON после {max_attempts} попыток: {last_error}",
        raw_text=last_text,
        attempt=max_attempts,
    )


def _parse_json(text: str) -> dict[str, Any]:
    """Извлечь первый валидный JSON-объект из текста.

    LLM может оборачивать JSON в `````json ... ```; также может
    предварять пояснением. Ищем первый ``{`` и парсим до баланса.
    """
    text = text.strip()
    if text.startswith("```"):
        # снять markdown fence
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    # Найти первый {
    start = text.find("{")
    if start < 0:
        raise json.JSONDecodeError("No '{' found in LLM response", text, 0)

    # Сбалансировать скобки
    depth = 0
    in_str = False
    escape = False
    end = -1
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if in_str:
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    if end < 0:
        raise json.JSONDecodeError("Unbalanced JSON braces", text, start)

    return json.loads(text[start:end])
