"""Сборка индекса по требованию: одна на процесс, состояние наблюдаемо.

Здесь проверяется владелец индексов (``libs/vectors/owner.py``), а не момент
сборки: сборку на старте зовёт сервер (``server.py::_prepare_capabilities``), и
её порядок закреплён в ``tests/test_server_bootstrap.py::TestStartupPreparation``.
Ниже — свойства самой сборки, одинаковые при любом вызывающем:

* индекс не собран, пока его не попросили (``ensure_index``);
* N параллельных обращений дают **ровно одну** сборку (single-flight) — прогрев
  на старте идёт тем же методом, поэтому ровно одна сборка достаётся и ему;
* ``list_indexes`` / ``index_stats`` отвечают, не поднимая индекс;
* состояние наблюдаемо и различает ``missing``/``building``/``ready``/``error``.

Прежняя шапка файла утверждала, что «до первого ``vector_search`` индекс не
собран» и что подъём MCP-сервера не должен зависеть от размера снимка. Второе
утверждение снято: старт линеен по размеру снимка намеренно, в обмен на то,
что первый поиск не платит за чужую подготовку. Первое осталось верным и
проверяется — но относится к владельцу, а не к старту сервера.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from libs.enterprise_common.errors import InfrastructureError, NotFoundError
from libs.enterprise_data.snapshot.contracts import IndexIntegrityError
from libs.vectors.owner import (
    STATE_BUILDING,
    STATE_ERROR,
    STATE_MISSING,
    STATE_READY,
    VectorIndexOwner,
)

DIM = 4


class FakeSnapshot:
    """Снимок без DuckDB: владелец индексов не знает, откуда пришли строки."""

    def __init__(self, *, rows: int = 3, dim: int = DIM, sources: tuple[str, ...] = ("idx_a",)):
        self.rows = rows
        self.dim = dim
        self.sources = sources
        self.fetch_calls = 0
        self.payload_calls = 0

    def vector_source_stats(self) -> list[dict[str, Any]]:
        return [
            {
                "source": s,
                "vector_count": self.rows,
                "embedding_sample": "[" + ",".join(["0.5"] * self.dim) + "]",
            }
            for s in self.sources
        ]

    def fetch_source_vectors(self, source: str) -> list[dict[str, Any]]:
        self.fetch_calls += 1
        if source not in self.sources:
            return []
        return [
            {
                "source": source,
                "table": "public.audits",
                "pk_value": f"pk-{i}",
                "chunk_index": 0,
                "chunk_count": 1,
                "embedding": [0.1 * (i + 1) * (j + 1) for j in range(self.dim)],
            }
            for i in range(self.rows)
        ]

    def fetch_chunk_payload(self, source: str, pk_value: Any, chunk_index: int) -> dict[str, Any]:
        self.payload_calls += 1
        return {"content": f"текст {pk_value}", "search_text": "", "row": {"pk": pk_value}}


def _embed(text: str) -> list[float]:
    return [0.1 * (len(text) % 3 + 1)] * DIM


def _owner(snapshot: FakeSnapshot | None = None, **kwargs: Any) -> VectorIndexOwner:
    return VectorIndexOwner(
        snapshot=snapshot or FakeSnapshot(),
        embed=_embed,
        current_index_config={"embedding_model": "mxbai", "embedding_dimension": DIM},
        **kwargs,
    )


class TestNoBuildBeforeFirstSearch:
    def test_constructor_does_not_touch_the_snapshot(self) -> None:
        """Подъём сервера не должен читать эмбеддинги."""
        snapshot = FakeSnapshot()
        VectorIndexOwner(snapshot=snapshot, embed=_embed)
        assert snapshot.fetch_calls == 0

    def test_state_is_missing_before_first_search(self) -> None:
        assert _owner().state("idx_a") == STATE_MISSING

    def test_list_indexes_does_not_build(self) -> None:
        snapshot = FakeSnapshot()
        owner = _owner(snapshot)
        listed = owner.list_indexes()
        assert snapshot.fetch_calls == 0
        assert owner.build_count("idx_a") == 0
        assert listed[0]["state"] == STATE_MISSING

    def test_index_stats_does_not_build(self) -> None:
        snapshot = FakeSnapshot()
        owner = _owner(snapshot)
        stats = owner.index_stats("idx_a")
        assert snapshot.fetch_calls == 0
        assert owner.build_count("idx_a") == 0
        assert stats["state"] == STATE_MISSING
        assert stats["last_built_at"] is None

    def test_index_stats_reports_dimension_without_faiss(self) -> None:
        stats = _owner().index_stats("idx_a")
        assert stats["dim"] == DIM
        assert stats["vectors"] == 3
        assert stats["queries"] == 0

    def test_first_search_builds_exactly_once(self) -> None:
        snapshot = FakeSnapshot()
        owner = _owner(snapshot)
        owner.search("запрос", "idx_a", top_k=2)
        assert owner.build_count("idx_a") == 1
        assert owner.state("idx_a") == STATE_READY
        assert snapshot.fetch_calls == 1

    def test_repeated_searches_do_not_rebuild(self) -> None:
        snapshot = FakeSnapshot()
        owner = _owner(snapshot)
        for _ in range(5):
            owner.search("запрос", "idx_a", top_k=2)
        assert owner.build_count("idx_a") == 1
        assert snapshot.fetch_calls == 1

    def test_index_stats_after_search_shows_build_metadata(self) -> None:
        owner = _owner()
        owner.search("запрос", "idx_a", top_k=2)
        stats = owner.index_stats("idx_a")
        assert stats["state"] == STATE_READY
        assert stats["last_built_at"] is not None
        assert stats["queries"] == 1
        assert stats["builds"] == 1

    def test_query_counter_grows(self) -> None:
        owner = _owner()
        owner.search("а", "idx_a", top_k=1)
        owner.search("б", "idx_a", top_k=1)
        assert owner.index_stats("idx_a")["queries"] == 2


class TestSingleFlightBuild:
    def test_parallel_searches_build_once(self) -> None:
        """N параллельных запросов = одна сборка, остальные ждут её."""
        snapshot = FakeSnapshot()
        owner = _owner(snapshot)
        threads_count = 12
        results: list[Any] = []
        errors: list[Exception] = []
        barrier = threading.Barrier(threads_count)

        def worker() -> None:
            try:
                barrier.wait(timeout=10)
                results.append(owner.search("запрос", "idx_a", top_k=3))
            except Exception as exc:  # noqa: BLE001 - тест фиксирует провал
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(threads_count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, errors
        assert len(results) == threads_count
        assert owner.build_count("idx_a") == 1, "индекс собран больше одного раза"
        assert snapshot.fetch_calls == 1

    def test_parallel_searches_all_return_results(self) -> None:
        snapshot = FakeSnapshot()
        owner = _owner(snapshot)
        out: list[list[Any]] = []
        lock = threading.Lock()
        start = threading.Barrier(6)

        def worker() -> None:
            start.wait(timeout=10)
            res = owner.search("запрос", "idx_a", top_k=3)
            with lock:
                out.append(res)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert len(out) == 6
        assert all(len(r) == 3 for r in out)
        assert owner.build_count("idx_a") == 1

    def test_building_state_is_observable(self) -> None:
        """Пока идёт сборка, состояние видно как ``building``, а не как «пусто»."""
        snapshot = FakeSnapshot()
        owner = _owner(snapshot)
        seen: list[str] = []
        gate = threading.Event()

        def worker() -> None:
            owner.search("запрос", "idx_a", top_k=1)
            gate.set()

        thread = threading.Thread(target=worker)
        thread.start()
        # Ждём, пока сборка начнётся: fetch делается первым действием _build.
        for _ in range(200):
            if owner.state("idx_a") == STATE_BUILDING:
                break
            gate.wait(0.01)
        seen.append(owner.state("idx_a"))
        thread.join(timeout=30)
        assert seen[0] in (STATE_BUILDING, STATE_READY)
        assert owner.state("idx_a") == STATE_READY


class TestSearchResults:
    def test_returns_search_results(self) -> None:
        from libs.enterprise_data.snapshot.contracts import SearchResult

        results = _owner().search("запрос", "idx_a", top_k=2)
        assert len(results) == 2
        assert all(isinstance(r, SearchResult) for r in results)
        assert results[0].content.startswith("текст ")

    def test_top_k_is_respected(self) -> None:
        assert len(_owner().search("запрос", "idx_a", top_k=1)) == 1

    def test_unknown_index_raises_not_found(self) -> None:
        with pytest.raises(NotFoundError):
            _owner().search("запрос", "нет_такого", top_k=1)

    def test_unknown_index_code(self) -> None:
        with pytest.raises(NotFoundError) as excinfo:
            _owner().search("запрос", "нет_такого", top_k=1)
        assert excinfo.value.code == "not_found"

    def test_unknown_index_via_list_is_not_listed(self) -> None:
        assert "нет_такого" not in [i["index_name"] for i in _owner().list_indexes()]

    def test_unknown_index_stats_raises(self) -> None:
        with pytest.raises(NotFoundError):
            _owner().index_stats("нет_такого")

    def test_embedder_failure_is_infrastructure_error(self) -> None:
        """Эмбеддер не ответил — это сбой, а не «ничего не нашлось»."""
        owner = VectorIndexOwner(
            snapshot=FakeSnapshot(),
            embed=lambda _t: None,
            current_index_config={"embedding_model": "mxbai"},
        )
        with pytest.raises(InfrastructureError) as excinfo:
            owner.search("запрос", "idx_a", top_k=1)
        assert excinfo.value.code == "embedding_unavailable"


class TestErrorState:
    def test_broken_vectors_leave_error_state(self) -> None:
        class BrokenSnapshot(FakeSnapshot):
            def fetch_source_vectors(self, source: str) -> list[dict[str, Any]]:
                return [
                    {"source": source, "embedding": "мусор"},
                    {"source": source, "embedding": "ещё мусор"},
                ]

        owner = _owner(BrokenSnapshot())
        with pytest.raises(InfrastructureError) as excinfo:
            owner.search("запрос", "idx_a", top_k=1)
        assert excinfo.value.code == "invalid_index"
        assert owner.state("idx_a") == STATE_ERROR
        assert owner.index_stats("idx_a")["error_code"] == "invalid_index"

    def test_empty_source_is_not_found(self) -> None:
        """Объявлен, но в снимке пуст — «индекс не загружен», не «сломан»."""
        owner = _owner(FakeSnapshot(sources=("другой",)))
        with pytest.raises(NotFoundError):
            owner.search("запрос", "idx_a", top_k=1)

    def test_error_state_is_retried_on_next_request(self) -> None:
        """Сбой не залипает: следующий запрос пробует снова."""
        class FlakySnapshot(FakeSnapshot):
            attempts = 0

            def fetch_source_vectors(self, source: str) -> list[dict[str, Any]]:
                FlakySnapshot.attempts += 1
                if FlakySnapshot.attempts == 1:
                    return [{"source": source, "embedding": "мусор"}]
                return super().fetch_source_vectors(source)

        owner = _owner(FlakySnapshot())
        with pytest.raises(InfrastructureError):
            owner.search("запрос", "idx_a", top_k=1)
        results = owner.search("запрос", "idx_a", top_k=1)
        assert len(results) == 1
        assert owner.state("idx_a") == STATE_READY


class TestSignatureGate:
    def test_stale_signature_blocks_search(self) -> None:
        """STALE — отдельный код, а не молчаливая выдача по устаревшему индексу."""
        owner = _owner()
        slot = owner.ensure_index("idx_a")
        slot.meta["signature"] = "b" * 64
        with pytest.raises(IndexIntegrityError) as excinfo:
            owner.search("запрос", "idx_a", top_k=1)
        assert excinfo.value.code == "stale_index"

    def test_invalid_signature_blocks_search(self) -> None:
        owner = _owner()
        slot = owner.ensure_index("idx_a")
        slot.meta["signature"] = "не-хеш"
        with pytest.raises(IndexIntegrityError) as excinfo:
            owner.search("запрос", "idx_a", top_k=1)
        assert excinfo.value.code == "invalid_index"

    def test_current_signature_passes(self) -> None:
        assert _owner().search("запрос", "idx_a", top_k=1)


class TestPreload:
    def test_preload_builds_all_known_indexes(self) -> None:
        owner = _owner(FakeSnapshot(sources=("idx_a", "idx_b")))
        loaded = owner.preload()
        assert {item["index_name"] for item in loaded} == {"idx_a", "idx_b"}
        assert all(item["vectors"] == 3 for item in loaded)
        assert all(item["signature_status"] == "CURRENT" for item in loaded)

    def test_preload_is_idempotent(self) -> None:
        owner = _owner()
        owner.preload()
        owner.preload()
        assert owner.build_count("idx_a") == 1

    def test_preload_survives_a_broken_index(self) -> None:
        """Сводка прогрева не должна падать из-за одного плохого индекса."""
        class MixedSnapshot(FakeSnapshot):
            def fetch_source_vectors(self, source: str) -> list[dict[str, Any]]:
                if source == "idx_b":
                    return [{"source": source, "embedding": "мусор"}]
                return super().fetch_source_vectors(source)

        owner = _owner(MixedSnapshot(sources=("idx_a", "idx_b")))
        loaded = owner.preload()
        assert [item["index_name"] for item in loaded] == ["idx_a"]
        assert owner.state("idx_b") == STATE_ERROR
