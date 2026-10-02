"""Unit-тесты для ``lib/utils/text_utils.py``.

Функция ``sanitize_value`` удалена вместе с её единственным потребителем
(``audit_analyzer/scripts/output.py`` ушёл на платформу), поэтому и её тесты
удалены. Осталась ``truncate_middle`` — её держит ``history_search_tool``,
и она нужна потому, что upstream режет хвост, а в JSON и CSV хвост несёт данные.
"""

from __future__ import annotations

from lib.utils.text_utils import truncate_middle


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
