"""Тесты для ``lib/utils/table_utils.py`` (нормализация имён таблиц)."""

from __future__ import annotations

import sys
from pathlib import Path

_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from lib.utils.table_utils import normalize_table_names


class TestNormalizeTableNames:
    def test_none_and_empty(self) -> None:
        assert normalize_table_names(None) == []
        assert normalize_table_names([]) == []

    def test_pair_lists(self) -> None:
        assert normalize_table_names([["public", "t1"]]) == ["public.t1"]

    def test_dict_form(self) -> None:
        value = [{"schema": "public", "table": "t1"}]
        assert normalize_table_names(value) == ["public.t1"]

    def test_string_form(self) -> None:
        assert normalize_table_names(["public.t1"]) == ["public.t1"]

    def test_mixed_forms(self) -> None:
        value = [
            ["public", "t1"],
            {"schema": "public", "table": "t2"},
            "public.t3",
        ]
        assert normalize_table_names(value) == [
            "public.t1", "public.t2", "public.t3",
        ]

    def test_invalid_items_skipped(self) -> None:
        value = [["only-schema"], {"table": "no-schema"}, "no-dot", "", 42]
        assert normalize_table_names(value) == []

    def test_no_dedup(self) -> None:
        # Дедупликация — ответственность вызывающего (dict.fromkeys).
        value = ["public.t1", ["public", "t1"]]
        assert normalize_table_names(value) == ["public.t1", "public.t1"]
