"""Unit-тесты для ``lib/services/column_descriptions.py::ColumnDescriptionsResolver``.

Resolver — generic механизм без знания о доменах. Словарь задаётся
извне (inline или data_file), тесты покрывают оба пути загрузки и
базовые свойства поиска.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lib.services.column_descriptions import ColumnDescriptionsResolver


_SAMPLE: dict[str, list[str]] = {
    "alpha|alef": ["schema.t.col1"],
    "beta|bet": ["schema.t.col2", "schema.t.col3"],
}


class TestInlineSource:
    def test_lookup_returns_match(self) -> None:
        r = ColumnDescriptionsResolver(entries_source=_SAMPLE)
        result = r.lookup("alpha")
        assert len(result) == 1
        assert result[0]["columns"] == ["schema.t.col1"]

    def test_lookup_no_match_returns_empty(self) -> None:
        r = ColumnDescriptionsResolver(entries_source=_SAMPLE)
        assert r.lookup("unknown term") == []

    def test_lookup_empty_term_returns_empty(self) -> None:
        r = ColumnDescriptionsResolver(entries_source=_SAMPLE)
        assert r.lookup("") == []

    def test_all_entries_returns_all(self) -> None:
        r = ColumnDescriptionsResolver(entries_source=_SAMPLE)
        out = r.all_entries()
        assert len(out) == 2

    def test_all_entries_with_max_matches(self) -> None:
        r = ColumnDescriptionsResolver(entries_source=_SAMPLE)
        out = r.all_entries(max_matches=1)
        assert len(out) == 1

    def test_max_matches_at_least_one(self) -> None:
        r = ColumnDescriptionsResolver(entries_source=_SAMPLE)
        out = r.all_entries(max_matches=0)
        assert len(out) >= 1

    def test_max_matches_zero_treated_as_one(self) -> None:
        r = ColumnDescriptionsResolver(entries_source=_SAMPLE)
        # ``max_matches=0`` для ``all_entries`` должно трактоваться как 1
        assert len(r.all_entries(max_matches=0)) == 1


class TestDataFileSource:
    def test_load_from_existing_file(
        self, tmp_path: Path
    ) -> None:
        import json
        path = tmp_path / "d.json"
        path.write_text(json.dumps(_SAMPLE), encoding="utf-8")
        r = ColumnDescriptionsResolver(entries_source=str(path))
        result = r.lookup("alef")
        assert len(result) == 1

    def test_load_from_missing_file_sets_load_error(
        self, tmp_path: Path
    ) -> None:
        r = ColumnDescriptionsResolver(
            entries_source=str(tmp_path / "missing.json")
        )
        assert r.lookup("anything") == []
        assert r.load_error is not None
        assert "не найден" in r.load_error

    def test_load_from_invalid_json_sets_load_error(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        r = ColumnDescriptionsResolver(entries_source=str(path))
        assert r.lookup("anything") == []
        assert r.load_error is not None
        assert "ошибка чтения" in r.load_error

    def test_relative_path_resolved_from_workspace_root(
        self, tmp_path: Path
    ) -> None:
        import json
        path = tmp_path / "d.json"
        path.write_text(json.dumps(_SAMPLE), encoding="utf-8")
        r = ColumnDescriptionsResolver(
            entries_source="d.json",
            workspace_root=tmp_path,
        )
        result = r.lookup("alpha")
        assert len(result) == 1


class TestNoSource:
    def test_none_source_returns_empty(self) -> None:
        r = ColumnDescriptionsResolver(entries_source=None)
        assert r.lookup("x") == []
        assert r.all_entries() == []
        assert r.load_error is None


class TestInvalidateCache:
    def test_invalidate_resets_state(self) -> None:
        r = ColumnDescriptionsResolver(entries_source=_SAMPLE)
        assert len(r.lookup("alpha")) >= 1
        r.invalidate_cache()
        # после invalidate работает так же (source не изменился)
        assert len(r.lookup("alpha")) >= 1


@pytest.mark.parametrize(
    "term,synonyms_key,expected_columns",
    [
        ("alpha", "alpha|alef", ["schema.t.col1"]),
        ("alef", "alpha|alef", ["schema.t.col1"]),
        ("beta", "beta|bet", ["schema.t.col2", "schema.t.col3"]),
        ("bet", "beta|bet", ["schema.t.col2", "schema.t.col3"]),
    ],
)
def test_lookup_parametrized(term, synonyms_key, expected_columns) -> None:
    r = ColumnDescriptionsResolver(entries_source=_SAMPLE)
    result = r.lookup(term)
    assert len(result) == 1
    assert result[0]["columns"] == expected_columns
    assert result[0]["terms"] == synonyms_key.split("|")