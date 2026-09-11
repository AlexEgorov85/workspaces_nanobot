"""Тонкая обёртка для подсказок колонок в LLM system prompt (legacy).

Использовалась только в ``generated_sql_mode.py`` (--mode generated_sql).
Phase 8: ``references/`` удалены, SKILL.md self-contained. Этот модуль
оставлен как transitional fallback для legacy CLI, но SKILL.md
больше не описывает этот режим как активный.

Хранит предкомпилированный словарь «русский термин → колонка»
(например, «объекты проверок» → ``oarb.audits.auditee_entity``).
"""


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

        \n  N. «audited objects» / «objects of audit» / «проверяемые» = \\
        `oarb.audits.auditee_entity` (NOT a separate objects table).
        \n  N+1. «violations» / «нарушения» = `oarb.violations`.

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
