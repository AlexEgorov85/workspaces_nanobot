"""LLM-prompt и parser для batch'ей (Phase 2B).

Каждый ContextBatch (multi-chunk) → один LLM call.
LLM получает явную секционную разметку + page ranges для каждого chunk'а.

Output: строго JSON ``{"chunks":[{"chunk_id":"...","summary":"...","section":"..."}]}``.

См. ``workspace/skills/legal_summarizer/ARCHITECTURE.md`` invariants #7, #9.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from workspace.skills.legal_summarizer.scripts.packing import ContextBatch


_BATCH_USER_TEMPLATE = """Документ для анализа.

ОБЩАЯ ИНФОРМАЦИЯ:
- Всего чанков в документе: {chunks_total}
- Страницы в этом фрагменте: {page_start}–{page_end}
- Разделы в этом фрагменте: {section_paths}

---

{chunks_block}

ФОРМАТ ОТВЕТА (строго JSON без markdown-обёрток):
{{
  "chunks": [
    {{"chunk_id": "001", "summary": "...", "section": "..."}},
    {{"chunk_id": "002", "summary": "...", "section": "..."}}
  ]
}}

ВАЖНО:
- Каждый chunk_id из списка выше должен встретиться в ответе ровно один раз.
- Не смешивай факты между chunks.
- Каждый chunk — самостоятельная единица анализа.
- summary — связный текст на русском, юридические термины переписаны на бытовой язык.
"""


_CHUNK_BLOCK_TEMPLATE = """DOCUMENT CHUNK {chunk_id}
Раздел: {section_path}
Заголовок: {section_heading}
Страницы: {page_start}–{page_end}
Символов: {char_count}

{text}

"""


class ChunkResultParseError(Exception):
    """Ответ LLM не содержит валидный JSON или отсутствуют chunk_id."""


def build_batch_user_message(
    batch: ContextBatch,
    *,
    chunks_total: int,
) -> str:
    """Построить user_body для LLM-вызова одного ContextBatch.

    Args:
        batch: контекст-батч с chunks.
        chunks_total: общее число chunks в документе (для контекста LLM).

    Returns:
        Готовая строка user_body.
    """
    sections = sorted({c.section_path for c in batch.chunks if c.section_path})
    section_paths_str = " ; ".join(sections) if sections else "(root)"

    page_start = batch.page_range[0] if batch.page_range[0] is not None else "?"
    page_end = batch.page_range[1] if batch.page_range[1] is not None else "?"

    chunks_block_parts: list[str] = []
    for c in batch.chunks:
        cs = c.page_start if c.page_start is not None else "?"
        ce = c.page_end if c.page_end is not None else "?"
        chunks_block_parts.append(
            _CHUNK_BLOCK_TEMPLATE.format(
                chunk_id=c.chunk_id,
                section_path=c.section_path or "(root)",
                section_heading=c.section_heading or "",
                page_start=cs,
                page_end=ce,
                char_count=c.char_count,
                text=c.text,
            )
        )
    chunks_block = "\n".join(chunks_block_parts)

    return _BATCH_USER_TEMPLATE.format(
        chunks_total=chunks_total,
        page_start=page_start,
        page_end=page_end,
        section_paths=section_paths_str,
        chunks_block=chunks_block,
    )


_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json_object(text: str) -> str | None:
    s = text.strip()
    try:
        json.loads(s)
        return s
    except Exception:
        pass
    m = _CODE_FENCE_RE.search(text)
    if m:
        try:
            json.loads(m.group(1))
            return m.group(1)
        except Exception:
            pass
    m = _JSON_OBJECT_RE.search(text)
    if m:
        try:
            json.loads(m.group(0))
            return m.group(0)
        except Exception:
            pass
    return None


def parse_batch_response(
    batch: ContextBatch,
    llm_text: str,
) -> dict[str, str]:
    """Распарсить ответ LLM, валидируя наличие всех chunk_id.

    Args:
        batch: контекст-батч (ожидаемые chunk_id).
        llm_text: текст ответа LLM.

    Returns:
        dict[chunk_id, summary].

    Raises:
        ChunkResultParseError: ответ невалиден или отсутствует какой-то chunk_id.
    """
    expected_ids = {c.chunk_id for c in batch.chunks}
    raw = _extract_json_object(llm_text)
    if raw is None:
        raise ChunkResultParseError(
            f"Не удалось извлечь JSON из ответа LLM. text[:200]={llm_text[:200]!r}"
        )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ChunkResultParseError(f"Невалидный JSON: {e}") from e
    if not isinstance(data, dict):
        raise ChunkResultParseError(
            f"Ожидался dict, получен {type(data).__name__}"
        )
    chunks = data.get("chunks")
    if not isinstance(chunks, list):
        raise ChunkResultParseError("Ключ 'chunks' должен быть list")
    out: dict[str, str] = {}
    for entry in chunks:
        if not isinstance(entry, dict):
            continue
        cid = entry.get("chunk_id")
        if not isinstance(cid, str):
            continue
        if cid not in expected_ids:
            continue
        summary = entry.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            continue
        out[cid] = summary.strip()
    missing = expected_ids - set(out.keys())
    if missing:
        raise ChunkResultParseError(
            f"В ответе LLM отсутствуют chunk_id: {sorted(missing)}"
        )
    return out


__all__ = [
    "build_batch_user_message",
    "parse_batch_response",
    "ChunkResultParseError",
]