"""Обёртка над ``references/sql_guidance.md`` для generated_sql_mode.

Нужен потому, что system prompt в LLM требует обучающих подсказок:
русские термины → конкретные колонки (например, «объекты проверок» →
``oarb.audits.auditee_entity``). Без этих подсказок LLM галлюцинирует
имена колонок. Храним в отдельном модуле, чтобы:

1. ``generated_sql_mode.py`` оставался фокусирован на pipeline-логике
   (whitelist → few-shot → retry). Без «длинных строковых литералов»
   в его исходнике.
2. Подсказки легко менять без затрагивания pipeline.

Источник истины — каталог ``references/`` skill'а (для редактирования
человеком). Этот модуль — тонкая обёртка с предкомпилированным словарём.

Пример использования::

    from column_hints import format_hints_block
    block = format_hints_block()  # '' если словарь пуст
"""

from __future__ import annotations

from pathlib import Path


# Каталог references/ skill'а: workspace/skills/audit_analyzer/references/.
# Этот файл лежит в scripts/, поэтому parents[1] → корень skill'а.
_REFERENCES_DIR = Path(__file__).resolve().parents[1] / "references"


_HINTS: dict[str, list[str]] = {
    # Ключ — русский/английский термин запроса; значение — список колонок
    # (fully qualified, schema.table.column). Несколько терминов через
    # запятую в одном ключе — норма.
    "audited objects|objects of audit|проверяемые|объекты проверок": [
        "oarb.audits.auditee_entity",
    ],
    "violations|нарушения": [
        "oarb.violations",
    ],
}


def format_hints_block() -> str:
    """Вернуть блок подсказок для system prompt или пустую строку.

    Формат — пронумерованные правила, продолжающие нумерацию вызывающего
    system_prompt (после пункта «Always schema-qualify table names»).
    Каждое правило маппит русские/английские термины на колонки, чтобы
    LLM не галлюцинировала имена.

    Пример::

        \\n  N. «audited objects» / «objects of audit» / «проверяемые» = \\
        `oarb.audits.auditee_entity` (NOT a separate objects table).
        \\n  N+1. «violations» / «нарушения» = `oarb.violations`.

    Returns:
        Многострочная строка без trailing whitespace или ``""``.
    """
    if not _HINTS:
        return ""
    lines = [""]
    base_num = 4
    for i, (terms, cols) in enumerate(_HINTS.items()):
        cols_str = ", ".join(f"`{c}`" for c in cols)
        parts = [f"«{t.strip()}»" for t in terms.split("|") if t.strip()]
        terms_str = " / ".join(parts)
        tail = " (NOT a separate objects table)" if "objects" in terms else ""
        lines.append(f"  {base_num + i}. {terms_str} = {cols_str}{tail}.")
    return "\n".join(lines).rstrip()


def references_dir() -> Path:
    """Путь к ``references/`` skill'а (для тестов и диагностики)."""
    return _REFERENCES_DIR
