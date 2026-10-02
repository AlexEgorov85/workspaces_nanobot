"""Text-хелперы для инструментов и скиллов.

Осталась одна функция: ``truncate_middle``.

``sanitize_value`` (рекурсивная приведение к JSON-совместимому виду) удалена: её
единственный production-импортёр был ``workspace/skills/audit_analyzer/
scripts/output.py``, который ушёл на платформу вместе со скиллом, а её собственные
тесты — единственные оставшиеся вызывающие. Мёртвый код в общем модуле вводит в
заблуждение: следующий читатель решит, что кто-то приводит значения к JSON-safe,
и будет полагаться на это там, где вызова нет.

``truncate_middle`` живёт потому, что upstream режет **хвост**
(``nanobot/utils/helpers.py:371``, ``_TRUNCATED_SUFFIX``), а хвост у JSON и CSV
несёт данные. Единственный production-импортёр —
``workspace/tools/history_search_tool.py:84``.
"""

from __future__ import annotations

__all__ = ["truncate_middle"]


def truncate_middle(text: str, max_chars: int) -> str:
    """Обрезать ``text`` до ``max_chars`` символов, сохранив head и tail.

    Если длина ``text`` не превышает ``max_chars``, возвращается как есть.
    Иначе берётся половина ``max_chars`` с начала и столько же с конца,
    между ними вставляется маркер с числом пропущенных символов.

    Args:
        text: Исходная строка.
        max_chars: Жёсткий потолок длины результата (>= 4).

    Returns:
        Усечённая строка с маркером ``... (N chars truncated) ...``.
    """
    if max_chars < 4:
        raise ValueError("max_chars must be >= 4")
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    omitted = len(text) - max_chars
    return (
        text[:half]
        + f"\n\n... ({omitted:,} chars truncated) ...\n\n"
        + text[-half:]
    )
