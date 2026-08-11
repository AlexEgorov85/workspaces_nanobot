from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from config import (
    AttrDict,
    _deep_merge,
    _flatten_env,
    _header_to_prefix,
    _parse_value,
    _strip_jsonc_comments,
    load_config_json,
    load_env,
)


class TestAttrDict:
    def test_getattr_existing_key(self):
        d = AttrDict({"a": 1, "b": "hello"})
        assert d.a == 1
        assert d.b == "hello"

    def test_getattr_nested_dict(self):
        d = AttrDict({"a": {"b": 1}})
        assert d.a.b == 1
        assert isinstance(d.a, AttrDict)

    def test_getattr_missing_key(self):
        d = AttrDict({"a": 1})
        with pytest.raises(AttributeError):
            _ = d.b

    def test_setattr(self):
        d = AttrDict()
        d.foo = "bar"
        assert d["foo"] == "bar"

    def test_dict_behavior_preserved(self):
        d = AttrDict({"a": 1, "b": 2})
        assert dict(d) == {"a": 1, "b": 2}
        assert len(d) == 2

    def test_deeply_nested(self):
        d = AttrDict({"a": {"b": {"c": 42}}})
        assert d.a.b.c == 42


class TestParseValue:
    def test_empty_string(self):
        assert _parse_value("") == ""
        assert _parse_value(None) is None

    def test_boolean_true(self):
        for v in ("true", "True", "TRUE", "yes", "Yes"):
            assert _parse_value(v) is True, f"Failed on {v!r}"

    def test_boolean_false(self):
        for v in ("false", "False", "FALSE", "no", "No"):
            assert _parse_value(v) is False, f"Failed on {v!r}"

    def test_integer(self):
        assert _parse_value("42") == 42
        assert _parse_value("-10") == -10
        assert _parse_value("0") == 0

    def test_float(self):
        assert _parse_value("3.14") == 3.14
        assert _parse_value("-0.5") == -0.5

    def test_json_object(self):
        result = _parse_value('{"key": "val"}')
        assert result == {"key": "val"}

    def test_json_array(self):
        result = _parse_value("[1, 2, 3]")
        assert result == [1, 2, 3]

    def test_invalid_json_falls_through(self):
        # starts with { but invalid JSON
        result = _parse_value("{bad json}")
        assert result == "{bad json}"

    def test_comma_list(self):
        result = _parse_value("a, b, c")
        assert result == ["a", "b", "c"]

    def test_single_item_no_comma(self):
        assert _parse_value("hello") == "hello"

    def test_whitespace_stripped(self):
        assert _parse_value("  42  ") == 42


class TestHeaderToPrefix:
    def test_simple_header(self):
        assert _header_to_prefix("# database") == ["database"]

    def test_nested_header(self):
        assert _header_to_prefix("## database:connection") == ["database", "connection"]

    def test_hyphens_to_underscores(self):
        assert _header_to_prefix("# my-section") == ["my_section"]

    def test_no_header_mark(self):
        assert _header_to_prefix("plain") == ["plain"]

    def test_extra_hashes(self):
        assert _header_to_prefix("### logging:level") == ["logging", "level"]

    def test_empty_result(self):
        assert _header_to_prefix("#") == []


class TestDeepMerge:
    def test_simple_override(self):
        base = {"a": 1, "b": 2}
        _deep_merge(base, {"b": 3, "c": 4})
        assert base == {"a": 1, "b": 3, "c": 4}

    def test_nested_merge(self):
        base = {"a": {"x": 1, "y": 2}}
        _deep_merge(base, {"a": {"y": 99, "z": 100}})
        assert base == {"a": {"x": 1, "y": 99, "z": 100}}

    def test_type_replacement(self):
        base = {"a": {"b": 1}}
        _deep_merge(base, {"a": "scalar"})
        assert base == {"a": "scalar"}

    def test_empty_override(self):
        base = {"a": 1}
        _deep_merge(base, {})
        assert base == {"a": 1}

    def test_empty_base(self):
        base = {}
        _deep_merge(base, {"a": 1})
        assert base == {"a": 1}


class TestFlattenEnv:
    def test_flat_dict(self):
        d = {"a": "1", "b": "2"}
        result = _flatten_env(d)
        assert result == {"A": "1", "B": "2"}

    def test_nested_dict(self):
        d = {"database": {"host": "localhost", "port": "5432"}}
        result = _flatten_env(d)
        assert result == {"DATABASE_HOST": "localhost", "DATABASE_PORT": "5432"}

    def test_mixed_types(self):
        d = {"a": {"b": "hello"}, "c": "world"}
        result = _flatten_env(d)
        assert result == {"A_B": "hello", "C": "world"}

    def test_spaces_replaced(self):
        d = {"my key": "val"}
        result = _flatten_env(d)
        assert result == {"MY_KEY": "val"}

    def test_empty_dict(self):
        assert _flatten_env({}) == {}


class TestLoadEnv:
    def test_file_not_found_returns_empty(self):
        result = load_env("/nonexistent/path/.env")
        assert result == {}

    def test_basic_env_file(self):
        content = "KEY=value\nNUMBER=42\nFLAG=true"
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False, suffix=".env") as f:
            f.write(content)
            tmp = f.name
        try:
            result = load_env(tmp)
            assert result.KEY == "value"
            assert result.NUMBER == 42
            assert result.FLAG is True
        finally:
            Path(tmp).unlink(missing_ok=True)

    def test_section_headers(self):
        content = (
            "# database\n"
            "HOST=localhost\n"
            "PORT=5432\n"
            "# logging:level\n"
            "VERBOSE=true\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False, suffix=".env") as f:
            f.write(content)
            tmp = f.name
        try:
            result = load_env(tmp)
            assert result.database.HOST == "localhost"
            assert result.database.PORT == 5432
            assert result.logging.level.VERBOSE is True
        finally:
            Path(tmp).unlink(missing_ok=True)

    def test_comment_lines_ignored(self):
        # lines starting with # and no = are treated as headers (so they set prefix)
        # lines with # and = are treated as comments and skipped
        content = "# this is a section header\nKEY=val\n#=another comment\n"
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False, suffix=".env") as f:
            f.write(content)
            tmp = f.name
        try:
            result = load_env(tmp)
            # "this is a section header" becomes a single key (no colon to split on)
            assert result["this is a section header"]["KEY"] == "val"
        finally:
            Path(tmp).unlink(missing_ok=True)

    def test_lines_without_equals_ignored(self):
        content = "KEY=val\njust a line\nOTHER=thing\n"
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False, suffix=".env") as f:
            f.write(content)
            tmp = f.name
        try:
            result = load_env(tmp)
            assert result.KEY == "val"
            assert result.OTHER == "thing"
            assert "just a line" not in result
        finally:
            Path(tmp).unlink(missing_ok=True)

    def test_double_underscore_creates_nesting(self):
        content = "DATABASE__HOST=localhost\nDATABASE__PORT=5432\n"
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False, suffix=".env") as f:
            f.write(content)
            tmp = f.name
        try:
            result = load_env(tmp)
            assert result.DATABASE.HOST == "localhost"
            assert result.DATABASE.PORT == 5432
        finally:
            Path(tmp).unlink(missing_ok=True)


class TestStripJsoncComments:
    def test_line_comments_removed(self):
        text = '{\n  // ведущий комментарий\n  "a": 1 // хвостовой комментарий\n}'
        assert _strip_jsonc_comments(text) == '{\n  \n  "a": 1 \n}'

    def test_block_comments_removed(self):
        text = '{ /* блок */ "a": /* внутри */ 1 }'
        assert _strip_jsonc_comments(text) == '{  "a":  1 }'

    def test_url_inside_string_kept(self):
        text = '{"url": "https://example.com/x", "dsn": "postgresql://u:p@host/db"}'
        assert _strip_jsonc_comments(text) == text

    def test_escaped_quote_inside_string(self):
        text = '{"path": "a/\\"b\\"//c", "x": 1 // comment\n}'
        out = _strip_jsonc_comments(text)
        assert 'a/\\"b\\"//c' in out
        assert "// comment" not in out

    def test_multiline_block_comment(self):
        text = '{\n/* строка 1\n   строка 2 */\n"a": 1\n}'
        assert _strip_jsonc_comments(text) == '{\n\n"a": 1\n}'

    def test_string_with_url_scheme_plus_comment_after(self):
        text = '{"base": "http://localhost:11434/api/embed", "x": 1} // trailing'
        out = _strip_jsonc_comments(text)
        assert "http://localhost:11434/api/embed" in out
        assert "trailing" not in out


class TestLoadConfigJson:
    def test_file_not_found_returns_empty(self):
        result = load_config_json("/nonexistent/config.json")
        assert result == {}

    def test_plain_json(self):
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False, suffix=".json") as f:
            f.write('{"a": 1, "b": {"c": 2}}')
            tmp = f.name
        try:
            result = load_config_json(tmp)
            assert result.a == 1
            assert result.b.c == 2
        finally:
            Path(tmp).unlink(missing_ok=True)

    def test_jsonc_with_comments(self):
        content = (
            '{\n'
            '  // секция с комментариями\n'
            '  "dsn": "postgresql://user:pass@localhost/db", /* блок */\n'
            '  "enabled": true,   // хвостовой комментарий\n'
            '  "url": "https://example.com/api" // URL внутри строки цел\n'
            '}\n'
        )
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False, suffix=".json") as f:
            f.write(content)
            tmp = f.name
        try:
            result = load_config_json(tmp)
            assert result.enabled is True
            assert result.dsn == "postgresql://user:pass@localhost/db"
            assert result.url == "https://example.com/api"
        finally:
            Path(tmp).unlink(missing_ok=True)

    def test_broken_json_returns_empty(self):
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False, suffix=".json") as f:
            f.write('{ not valid json')
            tmp = f.name
        try:
            result = load_config_json(tmp)
            assert result == {}
        finally:
            Path(tmp).unlink(missing_ok=True)


class TestSettingsModule:
    """Smoke test: the SETTINGS global loads without crashing."""

    def test_settings_is_attrdict(self):
        from config import SETTINGS
        assert isinstance(SETTINGS, AttrDict)

    def test_env_vars_set(self):
        from config import SETTINGS
        for key, val in _flatten_env(SETTINGS).items():
            assert os.environ.get(key) == val
