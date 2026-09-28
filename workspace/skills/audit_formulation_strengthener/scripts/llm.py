"""LLM-вызовы: тонкая обёртка над ``lib.services.llm_client`` + строгий JSON-retry.

* ``chat(messages, **kwargs) -> str`` — свободный текст. Параметры LLM
  (cfg / max_retries / timeout) берутся из ``scripts.skill_config``.
* ``chat_json(*, system, user, operation) -> dict`` — оборачивает ``chat``
  парсингом JSON и одной повторной попыткой при невалидном ответе.

Локальный ``JsonParseError`` намеренно лежит в скилле (не в lib/) — это
контракт-исключение потребителя, а не инфраструктура транспорта.
"""

from __future__ import annotations

import json
import re
from typing import Any

from lib.services.llm_client import call_llm

from workspace.skills.audit_formulation_strengthener.scripts.skill_config import (
    get_cli_config,
    get_llm_config,
    get_max_retries,
)


__all__ = ["chat", "chat_json", "JsonParseError"]


class JsonParseError(Exception):
    """LLM вернул ответ, который не удалось распарсить как JSON после 2 попыток.

    Attributes:
        raw_text: последний текст ответа LLM.
        attempt: номер неуспешной попытки (всегда 2 — именно столько делаем).
    """

    def __init__(self, message: str, raw_text: str, attempt: int) -> None:
        super().__init__(message)
        self.raw_text = raw_text
        self.attempt = attempt


def chat(
    messages: list[dict[str, Any]],
    *,
    context: list[dict[str, Any]] | None = None,
    **kwargs: Any,
) -> str:
    """Отправить messages в LLM и получить текстовый ответ.

    Параметры ``model`` / ``max_tokens`` / ``temperature`` берутся из
    ``skill_config.get_llm_config()``; значения из ``kwargs`` имеют приоритет.
    ``max_retries`` и ``timeout`` — из ``skill_config.get_max_retries()``
    и ``skill_config.get_cli_config()["timeout_sec"]``.
    """
    cfg = get_llm_config()
    cli = get_cli_config()
    return call_llm(
        messages,
        cfg=cfg,
        context=context,
        model=kwargs.get("model"),
        max_tokens=kwargs.get("max_tokens") or cfg.get("max_tokens"),
        temperature=(
            kwargs.get("temperature")
            if kwargs.get("temperature") is not None
            else cfg.get("temperature", 0.1)
        ),
        max_retries=get_max_retries(),
        timeout=float(cli.get("timeout_sec", 60)),
    )


def chat_json(
    *,
    system: str,
    user: str,
    operation: str = "afs",
) -> dict[str, Any]:
    """LLM-вызов со строгим парсингом JSON.

    До 2 попыток:

    1. Первая попытка с ``system + user``.
    2. Если ответ не парсится как JSON — повтор с user + «Предыдущий ответ
       невалиден как JSON: <err>. Верни только корректный JSON без пояснений».

    Args:
        system: system-промпт.
        user: user-payload.
        operation: короткий идентификатор операции (для логов и трассировки).

    Returns:
        Распарсенный JSON (dict).

    Raises:
        JsonParseError: если обе попытки провалились.
    """
    last_error: Exception | None = None
    last_text: str = ""

    for attempt in (1, 2):
        if attempt == 1:
            user_msg = user
        else:
            user_msg = (
                f"{user}\n\n---\n"
                f"Предыдущий ответ невалиден как JSON: {last_error!r}. "
                f"Верни только корректный JSON без пояснений."
            )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ]
        text = chat(messages)
        last_text = text
        try:
            return _parse_json(text)
        except (json.JSONDecodeError, ValueError) as exc:
            last_error = exc

    raise JsonParseError(
        f"Не удалось распарсить JSON после 2 попыток: {last_error}",
        raw_text=last_text,
        attempt=2,
    )


_FENCE_LINE_RE = re.compile(r"^```")


def _parse_json(text: str) -> dict[str, Any]:
    """Извлечь JSON-объект из ответа LLM.

    Снимает ````-fence и парсит через ``json.JSONDecoder.raw_decode``,
    который корректно обрабатывает вложенные скобки (в отличие от
    жадного regex). Это важно для ответов вида
    ``{"k": {"nested": 1}} и потом prose с } в тексте``.

    Raises:
        json.JSONDecodeError: если валидный JSON-объект верхнего уровня
            не найден во всём тексте.
        ValueError: если найден JSON, но верхнего уровня — не dict
            (например, массив или скаляр).
    """
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and _FENCE_LINE_RE.match(lines[0]):
            lines = lines[1:]
        if lines and _FENCE_LINE_RE.match(lines[-1]):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    decoder = json.JSONDecoder()
    # Перебираем все позиции `{` и пытаемся распарсить JSON-объект с этой
    # позиции. raw_decode учитывает баланс скобок и возвращает конец объекта.
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise json.JSONDecodeError(
        "No JSON object found in LLM response", text, 0
    )
