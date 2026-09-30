"""Сборка FAISS-индекса, разбор вектора и группировка результатов.

Порт векторной части ``tests/test_duckdb_cache_store.py`` и
``tests/test_vector_search_silent_failure.py`` из агента. Миграция
``enterprise-mcp-platform``, фаза 3; удаление агентских тестов — фазы 4/5/9.

Акцент на мусорных фикстурах: в агенте молчание ``build_faiss_index`` на
неразбираемой строке выглядело как «документы не найдены» при полностью
готовом индексе. Здесь каждый такой случай назван явно.
"""

from __future__ import annotations

import json

import pytest

from libs.vectors.grouping import build_raw_items, group_vector_hits
from libs.vectors.indexing import as_vector, build_faiss_index, vector_dimension


def _vec(values: list[float]) -> list[float]:
    return values


def _record(i: int, dim: int = 4) -> dict:
    return {
        "source": "idx_a",
        "table": "public.audits",
        "pk_value": f"pk-{i}",
        "chunk_index": 0,
        "chunk_count": 1,
        "embedding": _vec([0.1 * (i + 1) * (j + 1) for j in range(dim)]),
    }


class TestAsVector:
    def test_parses_list(self) -> None:
        assert as_vector([0.1, 0.2]) == [0.1, 0.2]

    def test_parses_tuple(self) -> None:
        assert as_vector((1, 2)) == [1.0, 2.0]

    def test_parses_varchar_form(self) -> None:
        """Так ``embedding`` реально лежит в снимке: ``VARCHAR`` с JSON-массивом."""
        assert as_vector("[0.1,0.2,0.3]") == [0.1, 0.2, 0.3]

    def test_parses_varchar_with_spaces_and_brackets(self) -> None:
        assert as_vector(" [ 0.5 , 0.25 ] ") == [0.5, 0.25]

    def test_parses_bare_numbers_without_brackets(self) -> None:
        assert as_vector("0.5, 0.25") == [0.5, 0.25]

    def test_parses_bytes(self) -> None:
        assert as_vector(b"[1,2]") == [1.0, 2.0]

    def test_parses_ndarray(self) -> None:
        import numpy as np

        assert as_vector(np.array([1.0, 2.0], dtype=np.float32)) == [1.0, 2.0]

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            "",
            "   ",
            "[]",
            "не число",
            "[1, два]",
            "[1,2",
            b"\xff\xfe\x00",
            {"a": 1},
            object(),
        ],
        ids=[
            "none", "empty", "blank", "empty_list", "not_a_number", "mixed",
            "unclosed", "bad_bytes", "dict", "object",
        ],
    )
    def test_garbage_returns_none(self, raw: object) -> None:
        """Мусор → ``None``, а не исключение: один битый вектор не роняет сборку."""
        assert as_vector(raw) is None

    def test_empty_vector_is_none(self) -> None:
        """Пустой список — не вектор: из него вышел бы индекс размерности 0."""
        assert as_vector([]) is None
        assert as_vector(()) is None
        assert as_vector("[]") is None


class TestVectorDimension:
    def test_dimension_from_list(self) -> None:
        assert vector_dimension([0.1, 0.2, 0.3]) == 3

    def test_dimension_from_varchar(self) -> None:
        assert vector_dimension("[1,2,3,4]") == 4

    @pytest.mark.parametrize("raw", [None, "", "мусор", "[]"], ids=["none", "empty", "junk", "empty_list"])
    def test_garbage_dimension_is_none(self, raw: object) -> None:
        assert vector_dimension(raw) is None


class TestBuildFaissIndex:
    def test_builds_flat_ip_index(self) -> None:
        index, meta = build_faiss_index([_record(i) for i in range(3)])
        assert index is not None
        assert index.ntotal == 3
        assert index.d == 4
        assert meta == {"metric": None}

    def test_cosine_normalizes_vectors(self) -> None:
        """cosine = IP по нормализованным векторам; метрика попадает в meta."""
        import numpy as np

        records = [_record(0), _record(1)]
        index, meta = build_faiss_index(records, metric="cosine")
        assert meta["metric"] == "cosine"
        assert np.isclose(np.linalg.norm(index.reconstruct(0)), 1.0, atol=1e-5)

    def test_empty_records_yield_no_index(self) -> None:
        assert build_faiss_index([]) == (None, None)

    def test_unparsable_first_embedding_yields_no_index(self) -> None:
        assert build_faiss_index([_record(0) | {"embedding": "мусор"}]) == (None, None)

    def test_dimension_mismatch_yields_no_index(self) -> None:
        """Разные размерности в одном индексе — не «индекс поменьше», а отказ."""
        records = [_record(0, dim=4), _record(1, dim=3)]
        assert build_faiss_index(records) == (None, None)

    def test_garbage_embedding_in_the_middle_yields_no_index(self) -> None:
        records = [_record(0), _record(1), _record(2) | {"embedding": None}]
        assert build_faiss_index(records) == (None, None)

    def test_varchar_records_build(self) -> None:
        """Снимок хранит эмбеддинг строкой — такой путь штатный, а не аварийный."""
        records = [_record(i) | {"embedding": "[0.1,0.2,0.3,0.4]"} for i in range(2)]
        index, _ = build_faiss_index(records)
        assert index is not None and index.ntotal == 2

    def test_search_finds_self(self) -> None:
        import numpy as np

        records = [_record(i) for i in range(3)]
        index, _ = build_faiss_index(records)
        vec = np.array([records[2]["embedding"]], dtype=np.float32)
        scores, ids = index.search(vec, 1)
        assert ids[0][0] == 2
        assert scores[0][0] > 0


class TestBuildRawItems:
    def test_hydrates_through_fetch_fn(self) -> None:
        scores = [[0.9, 0.5]]
        ids = [[0, 1]]
        meta_items = {
            "0": {"source": "idx_a", "table": "t", "pk_value": "p0",
                  "chunk_index": 0, "chunk_count": 1},
            "1": {"source": "idx_a", "table": "t", "pk_value": "p1",
                  "chunk_index": 0, "chunk_count": 1},
        }

        def fetch(source: str, pk: str, chunk: int) -> dict:
            return {"content": f"текст {pk}", "search_text": "", "row": {"id": pk}}

        raw = build_raw_items(
            meta_items, scores, ids, "idx_a", None, fetch_fn=fetch
        )
        assert [r["content"] for r in raw] == ["текст p0", "текст p1"]
        assert raw[0]["row"] == {"id": "p0"}
        assert raw[0]["score"] == pytest.approx(0.9)

    def test_threshold_filters_hits(self) -> None:
        raw = build_raw_items(
            {"0": {"pk_value": "p0"}}, [[0.4, 0.9]], [[0, 1]], "idx_a", 0.5
        )
        assert [r["pk_value"] for r in raw] == [1]

    def test_negative_ids_skipped(self) -> None:
        """FAISS отдаёт ``-1`` на нехватке результатов — это не документ."""
        raw = build_raw_items({}, [[0.0, 0.0]], [[0, -1]], "idx_a", None)
        assert len(raw) == 1

    def test_without_fetch_fn_payload_is_empty(self) -> None:
        raw = build_raw_items({"0": {"pk_value": "p0"}}, [[0.9]], [[0]], "idx_a", None)
        assert raw[0]["content"] == ""
        assert raw[0]["row"] == {}

    def test_failing_fetch_fn_does_not_kill_the_hit(self) -> None:
        """Сбой чтения payload не должен обнулять весь результат поиска."""

        def fetch(source: str, pk: str, chunk: int) -> dict:
            raise RuntimeError("снимок недоступен")

        raw = build_raw_items(
            {"0": {"pk_value": "p0"}}, [[0.9]], [[0]], "idx_a", None, fetch_fn=fetch
        )
        assert len(raw) == 1
        assert raw[0]["content"] == ""

    def test_missing_chunk_yields_empty_content(self) -> None:
        raw = build_raw_items(
            {"0": {"pk_value": "p0"}}, [[0.9]], [[0]], "idx_a", None,
            fetch_fn=lambda *a: {},
        )
        assert raw[0]["content"] == ""

    def test_row_data_json_string_is_parsed(self) -> None:
        raw = build_raw_items(
            {"0": {"pk_value": "p0"}}, [[0.9]], [[0]], "idx_a", None,
            fetch_fn=lambda *a: {"content": "t", "row": json.dumps({"id": 7})},
        )
        assert raw[0]["row"] == {"id": 7}

    def test_row_data_garbage_is_empty_dict(self) -> None:
        raw = build_raw_items(
            {"0": {"pk_value": "p0"}}, [[0.9]], [[0]], "idx_a", None,
            fetch_fn=lambda *a: {"content": "t", "row": "{не json"},
        )
        assert raw[0]["row"] == {}

    def test_row_data_non_dict_json_is_empty_dict(self) -> None:
        raw = build_raw_items(
            {"0": {"pk_value": "p0"}}, [[0.9]], [[0]], "idx_a", None,
            fetch_fn=lambda *a: {"content": "t", "row": "[1,2,3]"},
        )
        assert raw[0]["row"] == {}

    def test_content_falls_back_to_search_text(self) -> None:
        raw = build_raw_items(
            {"0": {"pk_value": "p0"}}, [[0.9]], [[0]], "idx_a", None,
            fetch_fn=lambda *a: {"content": "", "search_text": "запасной"},
        )
        assert raw[0]["content"] == "запасной"

    def test_chunk_label_only_for_multichunk(self) -> None:
        meta_items = {
            "0": {"pk_value": "p0", "chunk_index": 1, "chunk_count": 4},
            "1": {"pk_value": "p1", "chunk_index": 0, "chunk_count": 1},
        }
        raw = build_raw_items(meta_items, [[0.9, 0.8]], [[0, 1]], "idx_a", None)
        assert raw[0]["chunk"] == "2/4"
        assert raw[1]["chunk"] == ""

    def test_unknown_meta_falls_back_to_doc_id(self) -> None:
        raw = build_raw_items({}, [[0.9]], [[7]], "idx_a", None)
        assert raw[0]["pk_value"] == 7
        assert raw[0]["source"] == "idx_a"


class TestGroupVectorHits:
    def test_one_document_one_slot(self) -> None:
        raw = [
            {"source": "s", "table": "t", "pk_value": "p", "score": 0.9,
             "chunk_index": 0, "chunk_total": 2, "chunk": "1/2", "content": "a",
             "row": {}},
            {"source": "s", "table": "t", "pk_value": "p", "score": 0.7,
             "chunk_index": 1, "chunk_total": 2, "chunk": "2/2", "content": "a",
             "row": {}},
        ]
        results = group_vector_hits(raw, top_k=5)
        assert len(results) == 1
        assert results[0]["matched_chunks"] == 2
        assert results[0]["score"] == pytest.approx(0.9)

    def test_sorted_by_score_descending(self) -> None:
        raw = [
            {"source": "s", "table": "t", "pk_value": "a", "score": 0.1,
             "chunk_index": 0, "chunk_total": 1, "content": "", "row": {}},
            {"source": "s", "table": "t", "pk_value": "b", "score": 0.9,
             "chunk_index": 0, "chunk_total": 1, "content": "", "row": {}},
        ]
        results = group_vector_hits(raw, top_k=5)
        assert [r["pk_value"] for r in results] == ["b", "a"]

    def test_top_k_limits_documents(self) -> None:
        raw = [
            {"source": "s", "table": "t", "pk_value": str(i), "score": 0.1 * i,
             "chunk_index": 0, "chunk_total": 1, "content": "", "row": {}}
            for i in range(1, 6)
        ]
        assert len(group_vector_hits(raw, top_k=2)) == 2

    def test_active_threshold_disables_top_k_cut(self) -> None:
        """С порогом режутся сами себе выдачу, top_k не нужен."""
        raw = [
            {"source": "s", "table": "t", "pk_value": str(i), "score": 0.1 * i,
             "chunk_index": 0, "chunk_total": 1, "content": "", "row": {}}
            for i in range(1, 6)
        ]
        assert len(group_vector_hits(raw, top_k=1, threshold=0.05)) == 5

    def test_zero_threshold_does_not_disable_top_k(self) -> None:
        raw = [
            {"source": "s", "table": "t", "pk_value": str(i), "score": 0.1 * i,
             "chunk_index": 0, "chunk_total": 1, "content": "", "row": {}}
            for i in range(1, 6)
        ]
        assert len(group_vector_hits(raw, top_k=2, threshold=0.0)) == 2

    def test_internal_keys_are_dropped(self) -> None:
        raw = [
            {"source": "s", "table": "t", "pk_value": "a", "score": 0.5,
             "chunk_index": 0, "chunk_total": 1, "content": "", "row": {}},
        ]
        result = group_vector_hits(raw, top_k=1)[0]
        assert "chunk_index" not in result
        assert "chunk_total" not in result

    def test_empty_input(self) -> None:
        assert group_vector_hits([], top_k=5) == []
