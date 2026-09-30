"""Разбиение текстов на чанки.

Порт ``tests/test_text_splitter.py`` из агента. Миграция
``enterprise-mcp-platform``, фаза 3; удаление агентских тестов — фазы 4/5/9.

Тесты закрывают два бага, которые модуль исторически содержал: зацикливание
``_split_by_chars`` на финальном остатке (Баг А) и ``_merge_into_chunks`` на
части длиннее ``chunk_size`` (Баг Б). Оба — на мусорных по смыслу входах
(текст без разделителей, точный граничный остаток), поэтому именно здесь
регрессия выглядела бы как зависший процесс, а не как падающий тест.
"""

from __future__ import annotations

from libs.vectors.text_splitter import build_chunks, split_text


def _make_long_text(total_chars: int, sentence: str = "Арендодатель сдаёт помещение. ") -> str:
    repeats = (total_chars // len(sentence)) + 1
    return (sentence * repeats)[:total_chars]


def test_short_text_returns_single_chunk() -> None:
    text = "Короткий договор аренды."
    assert split_text(text, chunk_size=12000) == [text]


def test_empty_text_returns_empty_list() -> None:
    assert split_text("", chunk_size=12000) == []
    assert split_text("   \n  ", chunk_size=12000) == []


def test_large_text_no_hang_and_within_limit() -> None:
    """Баг А + Б: большой текст завершается и все чанки <= chunk_size."""
    text = _make_long_text(26000)
    chunks = split_text(text, chunk_size=12000, chunk_overlap=1000)
    assert chunks, "split_text вернул пустой результат"
    assert all(len(c) <= 12000 for c in chunks), "чанк превышает chunk_size"


def test_single_huge_paragraph_splits() -> None:
    """Один абзац > chunk_size должен разбиться, а не зациклиться (Баг Б)."""
    para = "Слова " * 5000  # ~25000 символов, один абзац
    text = para + "\n\n" + para
    chunks = split_text(text, chunk_size=5000, chunk_overlap=200)
    assert chunks
    assert all(len(c) <= 5000 for c in chunks)


def test_known_repro_from_summarizer() -> None:
    """Точное воспроизведение бага из legal_summarizer (paragraph*200)."""
    paragraph = "Арендодатель передаёт арендатору помещение во временное владение. " * 200
    text = paragraph + "\n\n" + paragraph
    assert len(text) > 20000
    chunks = split_text(text, chunk_size=12000, chunk_overlap=1000)
    assert chunks
    assert all(len(c) <= 12000 for c in chunks)
    # Разумное число чанков (не по одному символу, не бесконечность)
    assert 2 <= len(chunks) <= 10


def test_overlap_preserved_between_chunks() -> None:
    text = _make_long_text(30000)
    chunks = split_text(text, chunk_size=8000, chunk_overlap=500)
    for i in range(len(chunks) - 1):
        tail = chunks[i][-300:]
        assert tail in chunks[i + 1], f"перекрытие потеряно между чанками {i} и {i+1}"


def test_no_infinite_loop_on_exact_boundary() -> None:
    """Остаток, кратный chunk_size с overlap, не зацикливается."""
    text = "x" * 25000
    chunks = split_text(text, chunk_size=12000, chunk_overlap=1000)
    assert len(chunks) >= 2
    assert all(len(c) <= 12000 for c in chunks)
    joined = chunks[0]
    for c in chunks[1:]:
        joined += c[1000:] if len(c) > 1000 else c
    assert "x" * 25000 in joined or len(joined) >= 25000


def test_build_chunks_still_works() -> None:
    """Регрессия: build_chunks (используется сборщиком векторов) не сломался."""
    row = {"title": "Договор", "full_text": _make_long_text(4000)}
    result = build_chunks(row, ["full_text"], chunk_size=500, chunk_overlap=80)
    assert result
    for r in result:
        assert "search_text" in r
        assert "content_suffix" in r
        assert len(r["search_text"]) > 0


def test_build_chunks_short_columns_single_chunk() -> None:
    row = {"title": "Кратко", "desc": "Описание короткое"}
    result = build_chunks(row, ["title", "desc"], chunk_size=500, chunk_overlap=80)
    assert len(result) == 1
    assert "title" in result[0]["search_text"]
    assert "desc" in result[0]["search_text"]


class TestGarbageInput:
    """Мусорная фикстура: splitter обязан завершиться, а не зациклиться."""

    def test_whitespace_only(self) -> None:
        assert split_text("   \t\n  ", chunk_size=100, chunk_overlap=10) == []

    def test_no_separators_at_all(self) -> None:
        """Текст без единого разделителя идёт по ветке посимвольной разбивки."""
        chunks = split_text("a" * 5000, chunk_size=1000, chunk_overlap=100)
        assert chunks
        assert all(len(c) <= 1000 for c in chunks)

    def test_overlap_larger_than_chunk_does_not_hang(self) -> None:
        """overlap >= chunk_size — вырожденный случай из плохих данных."""
        chunks = split_text(_make_long_text(5000), chunk_size=100, chunk_overlap=200)
        assert chunks
        assert all(len(c) <= 100 for c in chunks)

    def test_zero_overlap(self) -> None:
        chunks = split_text(_make_long_text(3000), chunk_size=1000, chunk_overlap=0)
        assert chunks
        assert all(len(c) <= 1000 for c in chunks)

    def test_chunk_size_larger_than_text(self) -> None:
        assert split_text("мало", chunk_size=100_000) == ["мало"]

    def test_build_chunks_with_no_values(self) -> None:
        assert build_chunks({"a": "", "b": None}, ["a", "b"], chunk_size=100) == []

    def test_build_chunks_with_missing_columns(self) -> None:
        assert build_chunks({"a": "x"}, ["nope"], chunk_size=100) == []

    def test_build_chunks_survives_huge_single_column(self) -> None:
        result = build_chunks(
            {"a": "z" * 5000, "b": "короткое"}, ["a", "b"],
            chunk_size=500, chunk_overlap=50,
        )
        assert result
        assert all(len(r["search_text"]) > 0 for r in result)
