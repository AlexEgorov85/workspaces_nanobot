"""ChunkResultParseError + smart retry (PLAN §30, Этап 30).

Сейчас ошибка JSON в ответе LLM приводит к повторной отправке
**всего batch'а** — это дорого (PLAN §30).

Smart retry:

1. Local parse/repair (без LLM-вызова) — попытка извлечь valid JSON.
2. Repair prompt с **только проблемными chunk_ids** — не весь batch.

Этот модуль определяет:

* ``ChunkResultParseError`` — тип ошибки.
* ``parse_batch_response_local`` — попытка local repair.
* ``repair_failed_chunk_ids`` — какие chunk_ids требуют repair prompt.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


class ChunkResultParseError(Exception):
    """Raised when batch response cannot be parsed to valid JSON.

    Attributes:
        chunk_ids: проблемные chunk_ids (если удалось извлечь из response).
        raw_response: оригинальный текст от LLM.
    """

    def __init__(self, message: str, *, chunk_ids: tuple[str, ...] = (),
                 raw_response: str = "") -> None:
        super().__init__(message)
        self.chunk_ids = chunk_ids
        self.raw_response = raw_response


@dataclass(frozen=True)
class ParsedBatchResult:
    """Результат parsing batch response."""

    chunk_ids: tuple[str, ...]
    summaries: dict[str, str]
    failed_chunk_ids: tuple[str, ...] = ()
    raw: str = ""


_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)
_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _strip_code_fence(text: str) -> str:
    m = _FENCE_RE.search(text)
    if m:
        return m.group(1).strip()
    return text.strip()


def _extract_first_json_object(text: str) -> dict[str, Any] | None:
    cleaned = _strip_code_fence(text)
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        pass
    m = _OBJECT_RE.search(cleaned)
    if m:
        try:
            return json.loads(m.group(0))
        except (json.JSONDecodeError, ValueError):
            pass
    return None


def parse_batch_response_local(
    response_text: str,
    expected_chunk_ids: tuple[str, ...],
) -> ParsedBatchResult:
    """Попытаться распарсить response без LLM-вызова.

    Если JSON валидный и содержит summaries для всех expected_chunk_ids —
    возвращает ``ParsedBatchResult`` без failed.

    Если JSON невалидный или отсутствуют chunk_ids — возвращает с
    ``failed_chunk_ids``, чтобы caller мог отправить repair prompt
    только для них (PLAN §30).
    """
    obj = _extract_first_json_object(response_text)
    if obj is None:
        return ParsedBatchResult(
            chunk_ids=(),
            summaries={},
            failed_chunk_ids=expected_chunk_ids,
            raw=response_text,
        )

    summaries_raw = obj.get("summaries") or obj.get("chunks") or {}
    if not isinstance(summaries_raw, dict):
        return ParsedBatchResult(
            chunk_ids=(),
            summaries={},
            failed_chunk_ids=expected_chunk_ids,
            raw=response_text,
        )

    summaries: dict[str, str] = {}
    failed: list[str] = []
    for cid in expected_chunk_ids:
        if cid in summaries_raw and summaries_raw[cid]:
            val = summaries_raw[cid]
            if isinstance(val, dict):
                val = val.get("summary", "")
            if isinstance(val, str) and val.strip():
                summaries[cid] = val.strip()
            else:
                failed.append(cid)
        else:
            failed.append(cid)

    return ParsedBatchResult(
        chunk_ids=expected_chunk_ids,
        summaries=summaries,
        failed_chunk_ids=tuple(failed),
        raw=response_text,
    )


def build_repair_prompt(
    original_response: str,
    failed_chunk_ids: tuple[str, ...],
) -> str:
    """Создать **точечный** repair prompt только для failed chunks.

    Это решает проблему PLAN §30: вместо повторной отправки всего batch'а
    мы просим LLM дать только недостающие chunk summaries.
    """
    return (
        "Ваш предыдущий ответ не содержал валидный JSON или "
        f"пропустил summaries для chunk_ids: {list(failed_chunk_ids)}.\n\n"
        "Оригинальный ответ:\n"
        f"{original_response[:2000]}\n\n"
        "Пожалуйста, верните ТОЛЬКО JSON с полем `summaries`, где "
        f"ключи — это chunk_ids: {list(failed_chunk_ids)}.\n"
        "Формат: {\"summaries\": {\"<chunk_id>\": \"<summary>\", ...}}\n"
    )


__all__ = [
    "ChunkResultParseError",
    "ParsedBatchResult",
    "parse_batch_response_local",
    "build_repair_prompt",
]