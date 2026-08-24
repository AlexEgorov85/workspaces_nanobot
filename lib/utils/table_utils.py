"""Утилиты для нормализации имён таблиц и схем (generic, не зависят от skill)."""

from __future__ import annotations

from typing import Any


def normalize_table_names(value: Any) -> list[str]:
    """Привести список таблиц к плоскому списку ``"schema.table"`` строк.

    Допустимые форматы (для совместимости с исторической конфигурацией
    ``db_additional_tables``):

    - ``[["public", "agent_predefined_scripts"], ...]``
    - ``[{"schema": "public", "table": "agent_predefined_scripts"}, ...]``
    - ``["public.agent_predefined_scripts", ...]``

    Возвращает полные имена в формате ``schema.table``. Пустые или
    некорректные элементы пропускаются.
    """
    out: list[str] = []
    if not value:
        return out
    for item in value:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            sch, tbl = item
            if sch and tbl:
                out.append(f"{sch}.{tbl}")
        elif isinstance(item, dict) and item.get("schema") and item.get("table"):
            out.append(f"{item['schema']}.{item['table']}")
        elif isinstance(item, str) and "." in item:
            out.append(item)
    return out


__all__ = ["normalize_table_names"]