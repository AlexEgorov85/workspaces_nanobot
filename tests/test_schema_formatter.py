"""Тесты для internal service ``SchemaFormatter``.

SchemaFormatter — не tool, а shared helper. Тесты проверяют:
  * корректное чтение whitelist'а из TableRegistry;
  * трансляцию разных форматов ``table_names`` в ``[schema.table, ...]``;
  * truncate по ``max_chars``;
  * кеширование и его сброс через ``invalidate_cache``.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from lib.services.schema_formatter import SchemaFormatter
from lib.services.table_registry import (
    SkillRegistration,
    TableResource,
    table_registry,
)


@pytest.fixture(autouse=True)
def _reset_registry() -> None:
    table_registry.clear()
    yield
    table_registry.clear()


class TestListTables:
    def test_returns_empty_when_no_registrations(self) -> None:
        sf = SchemaFormatter()
        assert sf.list_tables() == []

    def test_returns_registered_skill_tables(self) -> None:
        table_registry.register(SkillRegistration(
            name="demo",
            resources=(
                TableResource(name="test.audits"),
                TableResource(name="test.violations"),
            ),
        ))
        sf = SchemaFormatter()
        names = sf.list_tables()
        assert "test.audits" in names
        assert "test.violations" in names

    def test_skips_disabled_skill_tables(self) -> None:
        table_registry.register(SkillRegistration(
            name="demo",
            enabled=False,
            resources=(TableResource(name="test.audits"),),
        ))
        sf = SchemaFormatter()
        assert sf.list_tables() == []


class TestListSchemaNames:
    def test_returns_main_when_no_tables(self) -> None:
        sf = SchemaFormatter()
        assert sf.list_schema_names() == ["main"]

    def test_extracts_schema_prefix(self) -> None:
        table_registry.register(SkillRegistration(
            name="demo",
            resources=(
                TableResource(name="oarb.audits"),
                TableResource(name="other.events"),
            ),
        ))
        sf = SchemaFormatter()
        assert sf.list_schema_names() == ["oarb", "other"]


class TestNormalizeTableNames:
    def test_none_returns_none(self) -> None:
        assert SchemaFormatter._normalize_table_names(None) is None

    def test_empty_returns_none(self) -> None:
        assert SchemaFormatter._normalize_table_names([]) is None

    def test_qualified_strings_passed_through(self) -> None:
        out = SchemaFormatter._normalize_table_names(["a.b", "c.d"])
        assert out == ["a.b", "c.d"]

    def test_pairs_are_joined(self) -> None:
        out = SchemaFormatter._normalize_table_names([["a", "b"], ("c", "d")])
        assert out == ["a.b", "c.d"]

    def test_mixed_forms(self) -> None:
        out = SchemaFormatter._normalize_table_names(["a.b", ["c", "d"]])
        assert out == ["a.b", "c.d"]

    def test_invalid_entries_skipped(self) -> None:
        out = SchemaFormatter._normalize_table_names(["a.b", "", None, []])
        assert out == ["a.b"]


class TestTruncate:
    def test_short_text_unchanged(self) -> None:
        assert SchemaFormatter._truncate("hello", 100) == "hello"

    def test_long_text_truncated(self) -> None:
        text = "a" * 5000
        out = SchemaFormatter._truncate(text, 100)
        assert len(out) <= 100
        assert out.endswith("[truncated]")

    def test_truncate_at_newline(self) -> None:
        text = "line1\n" + ("x" * 200)
        out = SchemaFormatter._truncate(text, 60)
        assert out.endswith("[truncated]")
        assert out.startswith("line1")

    def test_no_negative_budget(self) -> None:
        out = SchemaFormatter._truncate("hello world this is a long text", 5)
        assert len(out) == 5
        assert out == "\n[tru"


class TestFormatForLLM:
    def test_provider_missing_returns_empty(self) -> None:
        sf = SchemaFormatter(cache_ttl_sec=0)
        assert sf.format_for_llm() == ""

    def test_invalid_max_chars(self) -> None:
        sf = SchemaFormatter()
        with pytest.raises(ValueError):
            sf.format_for_llm(max_chars=0)
        with pytest.raises(ValueError):
            sf.format_for_llm(max_chars=-5)

    def test_uses_schema_name_fallback(self) -> None:
        sf = SchemaFormatter()
        with patch.object(sf, "_open_cache_provider", return_value=None):
            text = sf.format_for_llm()
        assert text == ""

    def test_invokes_provider_get_schema(self) -> None:
        captured: dict = {}

        class FakeProvider:
            def get_schema(self, *, schema_name, table_names):
                captured["schema_name"] = schema_name
                captured["table_names"] = table_names
                return {"schema": schema_name, "tables": {}}

            def close(self) -> None:
                pass

        sf = SchemaFormatter(cache_ttl_sec=0)
        with patch.object(sf, "_open_cache_provider", return_value=FakeProvider()):
            text = sf.format_for_llm(
                schema_name="oarb",
                table_names=["oarb.audits"],
            )

        assert captured["schema_name"] == "oarb"
        assert captured["table_names"] == ["oarb.audits"]
        assert "Schema: oarb" in text


class TestCache:
    def test_cache_hit_within_ttl(self) -> None:
        sf = SchemaFormatter(cache_ttl_sec=60)

        call_count = {"n": 0}

        class FakeProvider:
            def get_schema(self, *, schema_name, table_names):
                call_count["n"] += 1
                return {"schema": schema_name, "tables": {}}

            def close(self) -> None:
                pass

        with patch.object(sf, "_open_cache_provider", return_value=FakeProvider()):
            text1 = sf.format_for_llm(schema_name="oarb")
            text2 = sf.format_for_llm(schema_name="oarb")

        assert text1 == text2
        assert call_count["n"] == 1

    def test_cache_disabled_when_ttl_zero(self) -> None:
        sf = SchemaFormatter(cache_ttl_sec=0)

        call_count = {"n": 0}

        class FakeProvider:
            def get_schema(self, *, schema_name, table_names):
                call_count["n"] += 1
                return {"schema": schema_name, "tables": {}}

            def close(self) -> None:
                pass

        with patch.object(sf, "_open_cache_provider", return_value=FakeProvider()):
            sf.format_for_llm(schema_name="oarb")
            sf.format_for_llm(schema_name="oarb")

        assert call_count["n"] == 2

    def test_invalidate_clears_cache(self) -> None:
        sf = SchemaFormatter(cache_ttl_sec=60)

        call_count = {"n": 0}

        class FakeProvider:
            def get_schema(self, *, schema_name, table_names):
                call_count["n"] += 1
                return {"schema": schema_name, "tables": {}}

            def close(self) -> None:
                pass

        with patch.object(sf, "_open_cache_provider", return_value=FakeProvider()):
            sf.format_for_llm(schema_name="oarb")
            sf.invalidate_cache()
            sf.format_for_llm(schema_name="oarb")

        assert call_count["n"] == 2

    def test_cache_ttl_negative_raises(self) -> None:
        with pytest.raises(ValueError):
            SchemaFormatter(cache_ttl_sec=-1)


class TestProviderFailureIsolated:
    def test_provider_exception_returns_empty(self) -> None:
        class BrokenProvider:
            def get_schema(self, *, schema_name, table_names):
                raise RuntimeError("boom")

            def close(self) -> None:
                pass

        sf = SchemaFormatter(cache_ttl_sec=0)
        with patch.object(sf, "_open_cache_provider", return_value=BrokenProvider()):
            assert sf.format_for_llm() == ""

    def test_provider_close_failure_ignored(self) -> None:
        class BrokenCloseProvider:
            def get_schema(self, *, schema_name, table_names):
                return {"schema": schema_name, "tables": {}}

            def close(self) -> None:
                raise RuntimeError("close failed")

        sf = SchemaFormatter(cache_ttl_sec=0)
        with patch.object(
            sf, "_open_cache_provider", return_value=BrokenCloseProvider()
        ):
            text = sf.format_for_llm()
        assert "Schema:" in text
