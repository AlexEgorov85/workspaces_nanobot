"""LLM-prompt и parser для batch'ей.

Каждый ``list[Chunk]`` → один LLM call.

Output: свободный текст с маркерами ``DOCUMENT CHUNK N: <саммари>``. LLM
не тратит токены на обвязку/идентификаторы (chunk_id, section) — они
известны на нашей стороне. Парсер regex'ом достаёт блоки по порядку и
сопоставляет с chunk_id по позиции (чанк #N → chunk_id с индексом N-1
в списке). Это убирает ChunkResultParseError полностью — LLM не может
«забыть закрыть скобку» или «не экранировать кавычку».

NOTE: legacy импорт ``ContextBatch`` удалён в PLAN §20. Сигнатура
``build_batch_user_message(chunks, chunks_total=...)`` и
``parse_batch_response(chunks, llm_text)`` принимают ``list[Chunk]``
напрямую — canonical-compatible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk


_BATCH_USER_TEMPLATE = """Документ для анализа.

ОБЩАЯ ИНФОРМАЦИЯ:
- Всего чанков в документе: {chunks_total}
- Страницы в этом фрагменте: {page_start}–{page_end}
- Разделы в этом фрагменте: {section_paths}

---

{chunks_block}

ЗАДАНИЕ:
Для КАЖДОГО чанка выше напиши краткое саммари (2–4 предложения). Формат
ответа — строго по одному блоку на чанк, В ТОМ ЖЕ ПОРЯДКЕ, что и чанки выше:

DOCUMENT CHUNK 1: <саммари чанка 1>

DOCUMENT CHUNK 2: <саммари чанка 2>

...

ВАЖНО:
- Один блок на каждый чанк, без пропусков.
- Не смешивай факты между чанками.
- summary — связный текст на русском, юридические термины переписаны на бытовой язык.
- Никакого JSON, никаких markdown-обёрток, никаких номеров чанков в самом тексте саммари.
"""


_CHUNK_BLOCK_TEMPLATE = """DOCUMENT CHUNK {n}
Раздел: {section_path}
Заголовок: {section_heading}
Страницы: {page_start}–{page_end}
Символов: {char_count}

{text}

"""


_CHUNK_MARKER_RE = re.compile(
    r"(?m)^\s*DOC(?:UMENT)? CHUNK\s+(\d+)\s*:\s*(.*?)(?=\n\n|^\s*DOC(?:UMENT)? CHUNK\s+\d+\s*:|\Z)",
    re.DOTALL,
)


class ChunkResultParseError(Exception):
    """Ответ LLM не содержит маркеров DOCUMENT CHUNK N для всех чанков."""


def build_batch_user_message(
    chunks: Sequence[Chunk],
    *,
    chunks_total: int,
) -> str:
    """Построить user_body для LLM-вызова одного батча."""
    chunks_list = list(chunks)
    sections = sorted({c.section_path for c in chunks_list if c.section_path})
    section_paths_str = " ; ".join(sections) if sections else "(root)"

    page_starts = [c.page_start for c in chunks_list if c.page_start is not None]
    page_ends = [c.page_end for c in chunks_list if c.page_end is not None]
    page_start = min(page_starts) if page_starts else None
    page_end = max(page_ends) if page_ends else None

    chunks_block_parts: list[str] = []
    for idx, c in enumerate(chunks_list, start=1):
        cs = c.page_start if c.page_start is not None else "?"
        ce = c.page_end if c.page_end is not None else "?"
        chunks_block_parts.append(
            _CHUNK_BLOCK_TEMPLATE.format(
                n=idx,
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
        page_start=page_start if page_start is not None else "?",
        page_end=page_end if page_end is not None else "?",
        section_paths=section_paths_str,
        chunks_block=chunks_block,
    )


def parse_batch_response(
    chunks: Sequence[Chunk],
    llm_text: str,
) -> dict[str, str]:
    """Распарсить ответ LLM, сопоставляя саммари с chunk_id по позиции.

    LLM пишет блоки вида::

            DOCUMENT CHUNK 1: <саммари>
            DOCUMENT CHUNK 2: <саммари>
            ...

    Чанк #N → chunks[N-1].chunk_id.
    """
    chunks_list = list(chunks)
    expected_count = len(chunks_list)
    raw = llm_text or ""

    found_first: dict[int, str] = {}
    duplicates: list[int] = []
    for m in _CHUNK_MARKER_RE.finditer(raw):
        n_str, body = m.group(1), m.group(2).strip()
        if not body:
            continue
        try:
            n = int(n_str)
        except ValueError:
            continue
        if n in found_first:
            if n not in duplicates:
                duplicates.append(n)
            continue
        found_first[n] = body

    missing = [n for n in range(1, expected_count + 1) if n not in found_first]
    if missing:
        raise ChunkResultParseError(
            f"Номера чанков в ответе LLM неполные: "
            f"missing={missing}, duplicates={duplicates}, "
            f"got_nums={sorted(found_first.keys())}, expected=1..{expected_count}"
        )

    return {chunks_list[n - 1].chunk_id: found_first[n] for n in range(1, expected_count + 1)}


__all__ = [
    "build_batch_user_message",
    "parse_batch_response",
    "ChunkResultParseError",
]