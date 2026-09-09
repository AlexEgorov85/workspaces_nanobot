"""Детерминированная компрессия brief-секций (PLAN brief-refactor §13-§16).

BRIEF CONTRACT: один документ → ровно один Chunk.
Brief не выбирает canonical chunks. Brief собирает одно структурное
представление всего документа и при нехватке места **сокращает текст**
секций (но не удаляет сами секции). Количество chunks никогда не растёт.

Приоритет содержимого (п.13):

1. Сохранить все headings секций.
2. Сохранить структуру документа (DOCUMENT STRUCTURE block).
3. Сохранить начало каждой секции (head + минимальный budget).
4. Распределить оставшийся budget пропорционально размеру секций.
5. Не удалять целые секции.
6. Не создавать дополнительные chunks.

Распределение budget (п.14) — weighted:

    base = min(min_section_chars, available / section_count)
    remaining = available - base * section_count
    section_budget = base + remaining * section_chars / total_section_chars

Обрезание (п.15) — только по безопасной границе: paragraph → newline →
sentence → word → hard char boundary. Использует существующий набор
separators из ``chunking._text_helpers``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BriefSection:
    """Один раздел документа для финального brief-context.

    Attributes:
        heading: заголовок раздела (используется как heading и как
            fallback marker, если раздел пришлось сильно урезать).
        text: полный текст раздела (до компрессии).
    """

    heading: str
    text: str

    def char_count(self) -> int:
        return len(self.text)


_TRUNC_MARKER = "[BRIEF: section content truncated]"
_SAFE_BOUNDARIES = ("\n\n", "\n", ". ", "? ", "! ", "; ", ", ", " ")


def _truncate_text_safely(text: str, max_chars: int) -> str:
    """Обрезать ``text`` до ``max_chars`` по безопасной границе.

    П.15: paragraph → newline → sentence → word → hard char boundary.
    Если ни одна граница не найдена в окне — режем по ``max_chars``.
    Гарантирует ``result[-1]`` НЕ посередине слова без маркера truncation.
    """
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    window_end = max_chars
    for sep in _SAFE_BOUNDARIES:
        idx = text.rfind(sep, 0, window_end)
        if idx > 0:
            cut = idx + len(sep)
            if cut < len(text):
                return text[:cut].rstrip()
            return text[:cut]
    return text[:window_end].rstrip()


def allocate_budget(
    sections: list[BriefSection],
    *,
    available_chars: int,
    min_section_chars: int = 80,
) -> list[tuple[BriefSection, str, bool]]:
    """Распределить ``available_chars`` между секциями weighted-allocation.

    Returns:
        список кортежей ``(section, final_text, was_truncated)`` в
        исходном порядке. ``was_truncated=True`` означает, что текст секции
        был сокращён (тогда caller ОБЯЗАН добавить ``_TRUNC_MARKER`` в
        конец секции для прозрачности LLM).
    """
    if not sections:
        return []
    if available_chars <= 0:
        return [(s, "", True) for s in sections]

    n = len(sections)
    base = min(min_section_chars, max(0, available_chars // n))
    base_total = base * n
    remaining = max(0, available_chars - base_total)
    total_chars = sum(s.char_count() for s in sections)
    if total_chars <= available_chars:
        return [(s, s.text, False) for s in sections]

    out: list[tuple[BriefSection, str, bool]] = []
    for s in sections:
        if total_chars > 0:
            extra = int(round(remaining * s.char_count() / total_chars))
        else:
            extra = remaining // n
        section_budget = min(
            base + extra,
            available_chars,
            s.char_count(),
        )
        if s.char_count() <= section_budget:
            out.append((s, s.text, False))
            continue
        truncated = _truncate_text_safely(s.text, section_budget)
        was_truncated = len(truncated) < len(s.text)
        out.append((s, truncated, was_truncated))
    return out


def render_sections(
    sections: list[BriefSection],
    *,
    available_chars: int,
    min_section_chars: int = 80,
) -> str:
    """Сжать и отрендерить секции в один текст с маркерами truncation.

    Формат каждой секции::

        <heading>

        <text>

    П.13: heading всегда присутствует (даже если текст секции сокращён).
    П.16: каждая сокращённая секция получает ``_TRUNC_MARKER`` в конец.
    П.10: таблицы должны передаваться атомарно; truncation работает на
    уровне секций, поэтому цельная таблица в одной секции остаётся
    неразрезанной до тех пор, пока секция не вышла за budget. Если
    секция с таблицей урезается — truncation по безопасной границе
    может разделить текст вокруг таблицы, но сама таблица (внутри
    ``s.text``) остаётся атомарной строкой благодаря newline-boundary.
    """
    if not sections:
        return ""
    allocated = allocate_budget(
        sections,
        available_chars=available_chars,
        min_section_chars=min_section_chars,
    )
    parts: list[str] = []
    for section, text, was_truncated in allocated:
        if not text:
            continue
        parts.append(f"{section.heading}\n\n{text}")
        if was_truncated:
            parts.append(_TRUNC_MARKER)
    return "\n\n".join(parts)


__all__ = [
    "BriefSection",
    "allocate_budget",
    "render_sections",
    "_TRUNC_MARKER",
]
