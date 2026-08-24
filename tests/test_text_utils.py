"""Unit-тесты для ``lib/utils/text_utils.py``."""

from __future__ import annotations

import datetime
import decimal
import math
import uuid

from lib.utils.text_utils import sanitize_value, truncate_middle


class TestSanitizeValue:
    def test_none(self) -> None:
        assert sanitize_value(None) is None

    def test_datetime(self) -> None:
        assert sanitize_value(datetime.datetime(2024, 1, 15, 10, 30)) == "2024-01-15T10:30:00"

    def test_date(self) -> None:
        assert sanitize_value(datetime.date(2024, 1, 15)) == "2024-01-15"

    def test_time(self) -> None:
        assert sanitize_value(datetime.time(10, 30)) == "10:30:00"

    def test_timedelta(self) -> None:
        assert sanitize_value(datetime.timedelta(seconds=90)) == "0:01:30"

    def test_decimal_integral(self) -> None:
        assert sanitize_value(decimal.Decimal("5")) == 5
        assert isinstance(sanitize_value(decimal.Decimal("5")), int)

    def test_decimal_fractional(self) -> None:
        assert sanitize_value(decimal.Decimal("1.23")) == 1.23
        assert isinstance(sanitize_value(decimal.Decimal("1.23")), float)

    def test_uuid(self) -> None:
        u = uuid.UUID("12345678-1234-5678-1234-567812345678")
        assert sanitize_value(u) == "12345678-1234-5678-1234-567812345678"

    def test_bytes(self) -> None:
        assert sanitize_value(b"hello") == "hello"
        assert sanitize_value(b"\xff\xfe") == "\ufffd\ufffd"

    def test_nan(self) -> None:
        assert sanitize_value(float("nan")) is None

    def test_inf(self) -> None:
        assert sanitize_value(float("inf")) is None
        assert sanitize_value(float("-inf")) is None

    def test_finite_float(self) -> None:
        assert sanitize_value(3.14) == 3.14
        assert not math.isnan(sanitize_value(3.14))

    def test_int_str_bool_unchanged(self) -> None:
        assert sanitize_value(42) == 42
        assert sanitize_value("text") == "text"
        assert sanitize_value(True) is True
        assert sanitize_value(False) is False

    def test_list(self) -> None:
        assert sanitize_value([1, 2.5, "x"]) == [1, 2.5, "x"]

    def test_dict_keys_stringified(self) -> None:
        out = sanitize_value({1: "a", 2: "b"})
        assert out == {"1": "a", "2": "b"}

    def test_nested(self) -> None:
        out = sanitize_value({"k": [datetime.date(2024, 1, 1), None]})
        assert out == {"k": ["2024-01-01", None]}

    def test_unknown_with_isoformat(self) -> None:
        class X:
            def isoformat(self) -> str:
                return "x-iso"

        assert sanitize_value(X()) == "x-iso"

    def test_unknown_without_isoformat(self) -> None:
        class Y:
            def __str__(self) -> str:
                return "y-str"

        assert sanitize_value(Y()) == "y-str"

    def test_unknown_str_fails_uses_repr(self) -> None:
        class Z:
            def __str__(self) -> str:
                raise RuntimeError("nope")

            def __repr__(self) -> str:
                return "<Z>"

        assert sanitize_value(Z()) == "<Z>"


class TestTruncateMiddle:
    def test_no_truncation_needed(self) -> None:
        assert truncate_middle("hello", 100) == "hello"

    def test_exact_length(self) -> None:
        assert truncate_middle("abcde", 5) == "abcde"

    def test_truncation_marker(self) -> None:
        text = "x" * 200
        out = truncate_middle(text, 20)
        assert "chars truncated" in out
        assert len(out) <= 200

    def test_preserves_head_and_tail(self) -> None:
        text = ("HEAD" + "x" * 1000 + "TAIL")
        out = truncate_middle(text, 40)
        assert out.startswith("HEAD")
        assert out.endswith("TAIL")

    def test_too_small_max_chars(self) -> None:
        import pytest

        with pytest.raises(ValueError):
            truncate_middle("hello", 3)