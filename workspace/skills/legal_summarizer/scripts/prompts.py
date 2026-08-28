"""LLM-prompt и parser для batch'ей (Phase 2B).

Каждый ContextBatch (multi-chunk) → один LLM call.

Output: свободный текст с маркерами ``DOCUMENT CHUNK N: <саммари>`` — никакого
JSON. LLM не тратит токены на обвязку/идентификаторы (chunk_id, section) —
они и так известны на нашей стороне. Парсер regex'ом вытаскивает блоки
по порядку и сопоставляет с chunk_id батча по позиции (чанк #N → chunk_id
с индексом N-1 в батче). Это убирает ChunkResultParseError полностью —
LLM не может «забыть закрыть скобку» или «не экранировать кавычку».
"""

from __future__ import annotations

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


# Маркер для LLM: "DOCUMENT CHUNK N: ..." (допускается и краткое "DOC CHUNK").
# N — порядковый номер чанка в батче (1..K). Парсер регексом достаёт все
# вхождения и маппит на chunk_id по позиции. Lookahead ``^DOC(?:UMENT)? CHUNK``
# (с MULTILINE) ловит только маркер в начале
# строки — болтовня между блоками (через пустые строки) не попадает в body.
_CHUNK_MARKER_RE = re.compile(
    r"(?m)^\s*DOC(?:UMENT)? CHUNK\s+(\d+)\s*:\s*(.*?)(?=\n\n|^\s*DOC(?:UMENT)? CHUNK\s+\d+\s*:|\Z)",
    re.DOTALL,
)


class ChunkResultParseError(Exception):
    """Ответ LLM не содержит маркеров DOCUMENT CHUNK N для всех чанков батча.

    Оставлено для обратной совместимости (retry-логика в map-фазе).
    На текстовом формате практически недостижим — модель печатает блоки
    свободно. Но сеть/timeout/streaming-truncation всё ещё возможны.
    """


def build_batch_user_message(
    batch: ContextBatch,
    *,
    chunks_total: int,
) -> str:
    """Построить user_body для LLM-вызова одного ContextBatch.

    Чанки нумеруются 1..K (порядок в батче). LLM вернёт саммари в том же
    порядке с маркерами ``DOC CHUNK N: ...``.

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
    for idx, c in enumerate(batch.chunks, start=1):
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
        page_start=page_start,
        page_end=page_end,
        section_paths=section_paths_str,
        chunks_block=chunks_block,
    )


def parse_batch_response(
    batch: ContextBatch,
    llm_text: str,
) -> dict[str, str]:
    """Распарсить ответ LLM, сопоставляя саммари с chunk_id по позиции.

    LLM пишет блоки вида::

        DOCUMENT CHUNK 1: <саммари>
        DOCUMENT CHUNK 2: <саммари>
        ...

    Парсер regex'ом достаёт пары (n, summary), где n — порядковый номер
    чанка в батче (1..len(batch.chunks)). chunk_id определяется ПОЗИЦИЕЙ:
    чанк #N → batch.chunks[N-1].].chunk_id. Если каких-то номеров нет или
    они дублируются — ChunkResultParseError.

    Args:
        batch: контекст-батч (ожидаемые chunk_id, в порядке).
        llm_text: текст ответа LLM.

    Returns:
        dict[chunk_id, summary].

    Raises:
        ChunkResultParseError: отсутствуют или дублируются номера чанков.
    """
    expected_count = len(batch.chunks)
    raw = llm_text or ""

    # Достаём пары (n, text). Регекс ловит: до следующего маркера или до конца.
    # Берём ПЕРВОЕ непустое вхождение каждого номера; дубликаты маркеров
    # (без пропусков) не фатальны — модель может повторить маркер внутри
    # тела или при перечислении, особенно в одночанковых батчах. Важно
    # наличие саммари всех ожидаемых чанков, а не уникальность маркеров.
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

    if duplicates:
        try:
            from loguru import logger

            logger.warning(
                "parse_batch_response: дубликаты маркеров %s для batch %s — "
                "взято первое вхождение каждого номера",
                duplicates,
                batch.batch_id,
            )
        except Exception:
            pass

    # Сопоставляем позицию N → batch.chunks[N-1].chunk_id.
    return {batch.chunks[n - 1].chunk_id: found_first[n] for n in range(1, expected_count + 1)}


__all__ = [
    "build_batch_user_message",
    "parse_batch_response",
    "ChunkResultParseError",
]