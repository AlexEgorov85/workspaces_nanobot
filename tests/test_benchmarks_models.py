from __future__ import annotations

from benchmarks.models import BenchItem


class TestBenchItem:
    def test_hash(self):
        item1 = BenchItem(id="i1", name="A", difficulty=1, category="c", type="single")
        item2 = BenchItem(id="i1", name="B", difficulty=2, category="c", type="single")
        item3 = BenchItem(id="i3", name="C", difficulty=3, category="c", type="single")
        assert hash(item1) == hash(item2)
        assert hash(item1) != hash(item3)
        assert len({item1, item2, item3}) == 2