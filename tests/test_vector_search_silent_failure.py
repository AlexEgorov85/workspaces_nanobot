"""Регрессия: vector search не должен молчать там, где он сломан.

Оба дефекта найдены сквозным прогоном против живой БД (2026-09-30) и
объединяет их одно свойство: **тихий отказ**. Пользователь видел
``status: success`` и «Документы не найдены» при полностью готовом снимке,
готовых эмбеддингах и доступном эмбеддере. Отличить «ничего не нашлось»
от «поиск не работает» по выводу было невозможно.

1. ``build_faiss_index`` ждал ``list``/``tuple``, а колонка ``embedding``
   в локальном снимке хранится как ``VARCHAR`` (тип ``REAL[]`` из PG не
   переносится в DuckDB напрямую). На строке он возвращал
   ``(None, None)`` — индекс не строился, поиск возвращал пустой список.
2. ``search_vector`` гидрировал строки через ``self._conn`` ПОСЛЕ выхода
   из ``with self._read_conn()``. При схеме «открыть соединение на время
   операции» там уже ``None``, поэтому даже построенный индекс давал
   пустую выборку.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestAsVector:
    """Разбор значения колонки ``embedding`` в вектор чисел."""

    def test_parses_textual_vector(self):
        from lib.utils.duckdb_query import _as_vector

        assert _as_vector("[-0.5, 0.25, 1.0]") == pytest.approx([-0.5, 0.25, 1.0])

    def test_parses_text_without_brackets(self):
        from lib.utils.duckdb_query import _as_vector

        assert _as_vector("0.1, 0.2, 0.3") == pytest.approx([0.1, 0.2, 0.3])

    def test_accepts_list_and_tuple(self):
        from lib.utils.duckdb_query import _as_vector

        assert _as_vector([0.1, 0.2]) == pytest.approx([0.1, 0.2])
        assert _as_vector((0.1, 0.2)) == pytest.approx([0.1, 0.2])

    def test_accepts_bytes(self):
        from lib.utils.duckdb_query import _as_vector

        assert _as_vector(b"[1.0, 2.0]") == pytest.approx([1.0, 2.0])

    @pytest.mark.parametrize("bad", [None, "", "  ", "not-a-vector", "[1.0, oops]"])
    def test_rejects_garbage_without_raising(self, bad):
        """Мусс должен давать ``None``, а не исключение.

        Иначе сломанная строка в данных превращается в traceback вместо
        внятного «индекс не построен».
        """
        from lib.utils.duckdb_query import _as_vector

        assert _as_vector(bad) is None


class TestBuildFaissIndex:
    """Индекс строится из текстовых эмбеддингов, как в реальном снимке."""

    def _records(self, n: int = 4, emb: str = "[0.1, 0.2, 0.3]"):
        return [
            {
                "source": "violations_index",
                "table": "oarb.violations",
                "pk_value": str(i),
                "chunk_index": 0,
                "chunk_count": 1,
                "embedding": emb,
            }
            for i in range(n)
        ]

    def test_builds_index_from_varchar_embeddings(self):
        from lib.utils.duckdb_query import build_faiss_index

        idx, meta = build_faiss_index(self._records(), metric="cosine")
        assert idx is not None, "индекс не построился из строковых эмбеддингов"
        assert idx.ntotal == 4
        assert idx.d == 3
        assert meta["metric"] == "cosine"

    def test_dimension_taken_from_parsed_vector(self):
        """Раньше размерность бралась из ``len(строки)`` — это длина текста,
        а не число измерений, и индекс строился бы неверной размерности."""
        from lib.utils.duckdb_query import build_faiss_index

        idx, _ = build_faiss_index(self._records(emb="[0.5, 0.6]"), metric=None)
        assert idx is not None
        assert idx.d == 2

    def test_mismatched_dimension_returns_none(self):
        from lib.utils.duckdb_query import build_faiss_index

        recs = self._records(2)
        recs[1]["embedding"] = "[0.1, 0.2, 0.3, 0.4]"
        assert build_faiss_index(recs, metric=None) == (None, None)

    def test_empty_records_return_none(self):
        from lib.utils.duckdb_query import build_faiss_index

        assert build_faiss_index([], metric=None) == (None, None)


class TestSearchVectorHydration:
    """Гидратация строк обязана идти внутри блока соединения."""

    def test_source_does_not_read_conn_after_read_block(self):
        """Структурный гард: ``build_raw_items`` получа�� живое соединение.

        Регрессия была не «значение None», а сам паттерн — обращение к
        ``self._conn`` за пределами ``with self._read_conn()``. В режиме
        ``READ_ONLY`` там всегда ``None``, поэтому поиск возвращал пусто
        при готовом индексе.
        """
        import ast
        from pathlib import Path as _P

        src = (_P(__file__).resolve().parent.parent
               / "lib" / "services" / "duckdb_cache_store.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "search_vector"
        )
        # Ищем вызов build_raw_items и проверяем, что аргумент conn — не
        # self._conn: тот умирает вместе с блоком чтения.
        calls = [
            n for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and getattr(n.func, "id", None) == "build_raw_items"
        ]
        assert calls, "build_raw_items не найден в search_vector"
        for call in calls:
            conn_arg = next(
                kw.value for kw in call.keywords if kw.arg == "conn"
            )
            dumped = ast.unparse(conn_arg)
            assert "self._conn" not in dumped, (
                "build_raw_items получает self._conn — он уже закрыт "
                f"по выходе из with self._read_conn(): {dumped}"
            )
