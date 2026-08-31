"""Тесты для tool ``column_descriptions`` и сервиса
``lib.services.column_descriptions.ColumnDescriptionsResolver``.

Покрывает:
  * lookup по термину (русский/английский) через tool.execute();
  * match_all возвращает все entries;
  * fallback на inline entries из settings;
  * fallback на data_file;
  * disabled-режим;
  * in-process ``tool.lookup()`` (тонкая обёртка над resolver);
  * unit-тесты механизма resolver (tokenize/synonyms/normalize) — теперь
    живут в ``lib/services/column_descriptions.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib.services.column_descriptions import ColumnDescriptionsResolver
from workspace.tools.column_descriptions import (
    ColumnDescriptionsTool,
    ColumnDescriptionsToolConfig,
)


def _make_tool(
    *,
    data_file: str | None = None,
    inline_entries: dict | None = None,
) -> ColumnDescriptionsTool:
    config = ColumnDescriptionsToolConfig(data_file=data_file)
    tool = ColumnDescriptionsTool(config=config)
    tool._entries_override = inline_entries
    return tool


_SAMPLE_ENTRIES: dict[str, list[str]] = {
    "audited objects|objects of audit|проверяемые|объекты проверок": [
        "oarb.audits.auditee_entity",
    ],
    "violations|нарушения": ["oarb.violations"],
    "audits|аудиты|проверки": ["oarb.audits"],
}


class TestLookup:
    @pytest.mark.asyncio
    async def test_russian_term_finds_match(self) -> None:
        tool = _make_tool(inline_entries=_SAMPLE_ENTRIES)
        out = await tool.execute(term="объекты проверок")
        data = json.loads(out)
        assert data["status"] == "success"
        assert data["count"] == 1
        match = data["matches"][0]
        assert "объекты проверок" in match["terms"]
        assert "oarb.audits.auditee_entity" in match["columns"]

    @pytest.mark.asyncio
    async def test_english_term_finds_match(self) -> None:
        tool = _make_tool(inline_entries=_SAMPLE_ENTRIES)
        out = await tool.execute(term="violations")
        data = json.loads(out)
        assert data["count"] == 1
        assert "oarb.violations" in data["matches"][0]["columns"]

    @pytest.mark.asyncio
    async def test_unknown_term_returns_empty(self) -> None:
        tool = _make_tool(inline_entries=_SAMPLE_ENTRIES)
        out = await tool.execute(term="unknown xyzzy")
        data = json.loads(out)
        assert data["count"] == 0
        assert data["matches"] == []

    @pytest.mark.asyncio
    async def test_match_all_returns_all_entries(self) -> None:
        tool = _make_tool(inline_entries=_SAMPLE_ENTRIES)
        out = await tool.execute(match_all=True)
        data = json.loads(out)
        assert data["count"] == 3

    @pytest.mark.asyncio
    async def test_max_matches_limits_results(self) -> None:
        tool = _make_tool(inline_entries=_SAMPLE_ENTRIES)
        out = await tool.execute(match_all=True, max_matches=2)
        data = json.loads(out)
        assert data["count"] == 2

    @pytest.mark.asyncio
    async def test_no_term_with_match_all(self) -> None:
        tool = _make_tool(inline_entries=_SAMPLE_ENTRIES)
        out = await tool.execute()
        data = json.loads(out)
        assert data["term"] == ""
        assert data["count"] == 3

    @pytest.mark.asyncio
    async def test_too_large_response(self) -> None:
        tool = _make_tool(inline_entries=_SAMPLE_ENTRIES)
        tool.config.max_result_chars = 5
        out = await tool.execute(match_all=True)
        data = json.loads(out)
        assert data["status"] == "error"
        assert data["error_type"] == "result_too_large"


class TestDataFile:
    @pytest.mark.asyncio
    async def test_load_from_data_file(self, tmp_path: Path) -> None:
        path = tmp_path / "descriptions.json"
        path.write_text(
            json.dumps(_SAMPLE_ENTRIES, ensure_ascii=False),
            encoding="utf-8",
        )
        tool = _make_tool(data_file=str(path))
        out = await tool.execute(term="violations")
        data = json.loads(out)
        assert data["count"] == 1

    @pytest.mark.asyncio
    async def test_data_file_missing_returns_error(
        self, tmp_path: Path
    ) -> None:
        tool = _make_tool(data_file=str(tmp_path / "nope.json"))
        out = await tool.execute(term="x")
        data = json.loads(out)
        assert data["status"] == "error"
        assert "не найден" in data["message"]

    @pytest.mark.asyncio
    async def test_data_file_invalid_json_returns_error(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        tool = _make_tool(data_file=str(path))
        out = await tool.execute(term="x")
        data = json.loads(out)
        assert data["status"] == "error"
        assert "ошибка чтения" in data["message"]

    @pytest.mark.asyncio
    async def test_relative_path_resolved_from_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "descriptions.json"
        path.write_text(json.dumps(_SAMPLE_ENTRIES), encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        tool = _make_tool(data_file="descriptions.json")
        out = await tool.execute(term="violations")
        data = json.loads(out)
        assert data["count"] == 1


class TestEmptyConfig:
    @pytest.mark.asyncio
    async def test_no_entries_match_all_returns_empty(self) -> None:
        tool = _make_tool()
        out = await tool.execute(match_all=True)
        data = json.loads(out)
        assert data["count"] == 0


class TestLookupMethod:
    def test_lookup_returns_dict_list(self) -> None:
        tool = _make_tool(inline_entries=_SAMPLE_ENTRIES)
        result = tool.lookup("объекты проверок")
        assert isinstance(result, list)
        assert len(result) == 1
        assert "oarb.audits.auditee_entity" in result[0]["columns"]

    def test_lookup_no_matches(self) -> None:
        tool = _make_tool(inline_entries=_SAMPLE_ENTRIES)
        assert tool.lookup("unknown") == []

    def test_lookup_empty_term(self) -> None:
        tool = _make_tool(inline_entries=_SAMPLE_ENTRIES)
        assert tool.lookup("") == []


class TestResolverNormalizeEntries:
    def test_list_values_kept(self) -> None:
        raw = {"a|b": ["x.y", "z.w"]}
        out = ColumnDescriptionsResolver._normalize(raw)
        assert out == {"a|b": ["x.y", "z.w"]}

    def test_string_value_wrapped_in_list(self) -> None:
        raw = {"a": "x.y"}
        out = ColumnDescriptionsResolver._normalize(raw)
        assert out == {"a": ["x.y"]}

    def test_empty_columns_dropped(self) -> None:
        raw = {"a": [], "b": ["x"]}
        out = ColumnDescriptionsResolver._normalize(raw)
        assert out == {"b": ["x"]}

    def test_empty_string_keys_dropped(self) -> None:
        raw = {"": ["x"], "a": ["y"]}
        out = ColumnDescriptionsResolver._normalize(raw)
        assert out == {"a": ["y"]}

    def test_non_dict_returns_empty(self) -> None:
        assert ColumnDescriptionsResolver._normalize("not a dict") == {}
        assert ColumnDescriptionsResolver._normalize(None) == {}


class TestEnableDisable:
    def test_enabled_default_true(self) -> None:
        section = ColumnDescriptionsTool._read_settings_section(
            _FakeCtx({"tools": {}})
        )
        assert ColumnDescriptionsTool.enabled(_FakeCtx({"tools": {}})) is True

    def test_disabled_when_section_false(self) -> None:
        assert ColumnDescriptionsTool.enabled(
            _FakeCtx({"tools": {"column_descriptions": {"enable": False}}})
        ) is False


class TestCreate:
    def test_create_with_section(self) -> None:
        ctx = _FakeCtx({
            "tools": {
                "column_descriptions": {
                    "enable": True,
                    "data_file": "data/x.json",
                    "max_result_chars": 8000,
                }
            }
        })
        instance = ColumnDescriptionsTool.create(ctx)
        assert isinstance(instance, ColumnDescriptionsTool)
        assert instance.config.data_file == "data/x.json"
        assert instance.config.max_result_chars == 8000

    def test_create_without_ctx_settings_uses_defaults(self) -> None:
        instance = ColumnDescriptionsTool.create(_FakeCtx({}))
        assert instance.config.enable is True
        assert instance.config.data_file is None


class TestResolverTokenize:
    def test_lowercase_and_filter_short(self) -> None:
        toks = ColumnDescriptionsResolver._tokenize("Hello World! a bb ccc")
        assert "hello" in toks
        assert "world" in toks
        assert "ccc" in toks
        assert "a" not in toks
        assert "bb" not in toks


class TestResolverSynonymsSplit:
    def test_split_on_pipe(self) -> None:
        assert ColumnDescriptionsResolver._split_synonyms("a|b|c") == ["a", "b", "c"]

    def test_strip_whitespace(self) -> None:
        assert ColumnDescriptionsResolver._split_synonyms(" a | b ") == ["a", "b"]

    def test_drop_empty(self) -> None:
        assert ColumnDescriptionsResolver._split_synonyms("|a||b|") == ["a", "b"]


class _FakeCtx:
    """Минимальный ctx для тестов settings_reading."""

    def __init__(self, settings: dict) -> None:
        from types import SimpleNamespace

        def to_ns(d):
            if isinstance(d, dict):
                return SimpleNamespace(**{k: to_ns(v) for k, v in d.items()})
            return d

        self._settings_ref = SimpleNamespace(tools=to_ns(settings.get("tools", {})))
