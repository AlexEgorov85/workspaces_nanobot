"""Тесты lib.services.text_splitter (после fix бесконечных циклов).

Покрывают:
    * Баг А — _split_by_chars зацикливался на финальном остатке;
    * Баг Б — _merge_into_chunks зацикливался на части > chunk_size;
    * контракт: все чанки <= chunk_size (кроме неразбиваемых);
    * перекрытие между соседними чанками сохраняется;
    * build_chunks (используется в build_vectors) не сломался.

Запуск:
    python -m pytest tests/test_text_splitter.py -v
"""

from __future__ import annotations

from lib.services.text_splitter import build_chunks, split_text


def _make_long_text(total_chars: int, sentence: str = "Арендодатель сдаёт помещение. ") -> str:
    repeats = (total_chars // len(sentence)) + 1
    return (sentence * repeats)[:total_chars]


def test_short_text_returns_single_chunk():
    text = "Короткий договор аренды."
    assert split_text(text, chunk_size=12000) == [text]


def test_empty_text_returns_empty_list():
    assert split_text("", chunk_size=12000) == []
    assert split_text("   \n  ", chunk_size=12000) == []


def test_large_text_no_hang_and_within_limit():
    """Баг А + Б: большой текст завершается и все чанки <= chunk_size."""
    text = _make_long_text(26000)
    chunks = split_text(text, chunk_size=12000, chunk_overlap=1000)
    assert chunks, "split_text вернул пустой результат"
    assert all(len(c) <= 12000 for c in chunks), "чанк превышает chunk_size"


def test_single_huge_paragraph_splits():
    """Один абзац > chunk_size должен разбиться, а не зациклиться (Баг Б)."""
    para = "Слова " * 5000  # ~25000 символов, один абзац
    text = para + "\n\n" + para
    chunks = split_text(text, chunk_size=5000, chunk_overlap=200)
    assert chunks
    assert all(len(c) <= 5000 for c in chunks)


def test_known_repro_from_summarizer():
    """Точное воспроизведение бага из legal_summarizer (paragraph*200)."""
    paragraph = "Арендодатель передаёт арендатору помещение во временное владение. " * 200
    text = paragraph + "\n\n" + paragraph
    assert len(text) > 20000
    chunks = split_text(text, chunk_size=12000, chunk_overlap=1000)
    assert chunks
    assert all(len(c) <= 12000 for c in chunks)
    # Разумное число чанков (не по одному символу, не бесконечность)
    assert 2 <= len(chunks) <= 10


def test_overlap_preserved_between_chunks():
    text = _make_long_text(30000)
    chunks = split_text(text, chunk_size=8000, chunk_overlap=500)
    for i in range(len(chunks) - 1):
        # Хвост предыдущего чанка (до 500 симв.) должен появиться в следующем
        tail = chunks[i][-300:]
        assert tail in chunks[i + 1], f"перекрытие потеряно между чанками {i} и {i+1}"


def test_no_infinite_loop_on_exact_boundary():
    """Остаток, кратный chunk_size с overlap, не зацикливается."""
    text = "x" * 25000
    chunks = split_text(text, chunk_size=12000, chunk_overlap=1000)
    assert len(chunks) >= 2
    assert all(len(c) <= 12000 for c in chunks)
    # Восстановленный текст (без перекрытий) должен содержать исходник
    joined = chunks[0]
    for c in chunks[1:]:
        # убираем известный хвост-перекрытие (до 1000) и добавляем остаток
        joined += c[1000:] if len(c) > 1000 else c
    assert "x" * 25000 in joined or len(joined) >= 25000


def test_build_chunks_still_works():
    """Регрессия: build_chunks (используется в build_vectors) не сломался."""
    row = {"title": "Договор", "full_text": _make_long_text(4000)}
    result = build_chunks(row, ["full_text"], chunk_size=500, chunk_overlap=80)
    assert result
    for r in result:
        assert "search_text" in r
        assert "content_suffix" in r
        assert len(r["search_text"]) > 0


def test_build_chunks_short_columns_single_chunk():
    row = {"title": "Кратко", "desc": "Описание короткое"}
    result = build_chunks(row, ["title", "desc"], chunk_size=500, chunk_overlap=80)
    assert len(result) == 1
    assert "title" in result[0]["search_text"]
    assert "desc" in result[0]["search_text"]


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
