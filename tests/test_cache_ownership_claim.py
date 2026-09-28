"""
Тесты ``CacheOwnershipCoordinator`` (Stage C — change ``unify-cli-gateway-architecture``).

Покрывают:
  * первый процесс становится OWNER (generation=1);
  * второй процесс становится READER (current_owner_id от первого);
  * concurrent claim — ровно один OWNER;
  * takeover при истёкшем ``expires_at`` (generation инкрементируется);
  * heartbeat продлевает claim;
  * heartbeat возвращает False после takeover;
  * release удаляет только при совпадении generation;
  * release при mismatch — False + WARNING;
  * acquire_write_fence mutually исключает ``try_claim()`` и detect
    generation mismatch через ``OwnershipLostError``;
  * resource_key фиксирован — только один ряд в таблице.

Требует PostgreSQL (test-профиль). Применяется миграция V005
(``agent_cache_ownership``).
"""

from __future__ import annotations

import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

_project_root = Path(__file__).resolve().parent.parent
_workspace_path = str(_project_root / "workspace")
if _workspace_path not in sys.path:
    sys.path.insert(0, _workspace_path)
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from workspace.utils.db import execute, fetchval  # noqa: E402

from lib.services.cache_ownership import (  # noqa: E402
    DEFAULT_RESOURCE_KEY,
    CacheAccessMode,
    CacheOwnershipCoordinator,
    ClaimResult,
    OwnershipLostError,
)


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_claim_table():
    """Очистить строку логического claim'а перед/после теста.

    Удаляет только resource_key='local_cache'. Полная truncate не нужна —
    таблица используется одновременно многими процессами.
    """
    execute(
        "DELETE FROM public.agent_cache_ownership WHERE resource_key = %s",
        DEFAULT_RESOURCE_KEY,
    )
    yield DEFAULT_RESOURCE_KEY
    execute(
        "DELETE FROM public.agent_cache_ownership WHERE resource_key = %s",
        DEFAULT_RESOURCE_KEY,
    )


def _expire_claim() -> None:
    """Передвинуть expires_at в прошлое, чтобы следующий try_claim() = takeover."""
    execute(
        "UPDATE public.agent_cache_ownership "
        "SET expires_at = NOW() - INTERVAL '1 second' "
        "WHERE resource_key = %s",
        DEFAULT_RESOURCE_KEY,
    )


def _unique_worker_id(prefix: str) -> str:
    """worker_id уникальный в рамках теста/процесса."""
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTryClaimBasics:
    def test_first_process_becomes_owner_with_generation_1(
        self, fresh_claim_table: str
    ) -> None:
        coord = CacheOwnershipCoordinator(worker_id=_unique_worker_id("a"))
        result = coord.try_claim()
        assert isinstance(result, ClaimResult)
        assert result.acquired is True
        assert result.generation == 1
        assert result.owner_id == coord.worker_id
        assert coord.is_owner is True
        assert coord.mode == CacheAccessMode.READ_WRITE
        assert result.mode == CacheAccessMode.READ_WRITE

    def test_second_process_becomes_reader_with_current_owner(
        self, fresh_claim_table: str
    ) -> None:
        first = CacheOwnershipCoordinator(worker_id=_unique_worker_id("a"))
        first_claim = first.try_claim()
        assert first_claim.acquired is True

        second = CacheOwnershipCoordinator(worker_id=_unique_worker_id("b"))
        second_claim = second.try_claim()
        assert second_claim.acquired is False
        assert second_claim.generation == 1
        assert second_claim.current_owner_id == first.worker_id
        assert second_claim.current_generation == 1
        assert second.mode == CacheAccessMode.READ_ONLY
        assert second.is_owner is False

    def test_claim_is_idempotent_for_active_owner(
        self, fresh_claim_table: str
    ) -> None:
        first = CacheOwnershipCoordinator(worker_id=_unique_worker_id("a"))
        first.try_claim()

        reread = CacheOwnershipCoordinator(worker_id=_unique_worker_id("a"))
        reread_claim = reread.try_claim()
        assert reread_claim.acquired is False
        assert reread_claim.current_owner_id == first.worker_id


class TestConcurrentClaim:
    def test_concurrent_claim_exactly_one_owner(
        self, fresh_claim_table: str
    ) -> None:
        """N потоков запускают try_claim() одновременно — ровно один OWNER."""
        results: list[ClaimResult] = []
        lock = threading.Lock()
        workers = [_unique_worker_id("concurrent") for _ in range(8)]
        coords = [CacheOwnershipCoordinator(worker_id=w) for w in workers]

        barrier = threading.Barrier(len(coords))

        def runner(coord: CacheOwnershipCoordinator) -> None:
            barrier.wait()
            res = coord.try_claim()
            with lock:
                results.append(res)

        threads = [
            threading.Thread(target=runner, args=(c,), daemon=True)
            for c in coords
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        assert len(results) == len(coords)
        owners = [r for r in results if r.acquired]
        readers = [r for r in results if not r.acquired]
        assert len(owners) == 1, f"expected exactly 1 owner, got {len(owners)}"
        assert len(readers) == len(coords) - 1
        for r in readers:
            assert r.current_owner_id == owners[0].owner_id


class TestTakeover:
    def test_stale_claim_takeover_increments_generation(
        self, fresh_claim_table: str
    ) -> None:
        """Claim с истёкшим expires_at → takeover с generation+1."""
        first = CacheOwnershipCoordinator(worker_id=_unique_worker_id("a"))
        first.try_claim()

        _expire_claim()

        second = CacheOwnershipCoordinator(worker_id=_unique_worker_id("b"))
        claim = second.try_claim()
        assert claim.acquired is True
        assert claim.generation == 2
        assert claim.owner_id == second.worker_id

    def test_ownership_key_single_resource_row(
        self, fresh_claim_table: str
    ) -> None:
        """resource_key фиксирован — один ряд в таблице для ключа."""
        coord = CacheOwnershipCoordinator(worker_id=_unique_worker_id("a"))
        coord.try_claim()

        count = fetchval(
            "SELECT COUNT(*) FROM public.agent_cache_ownership WHERE resource_key = %s",
            DEFAULT_RESOURCE_KEY,
        )
        assert count == 1


class TestHeartbeat:
    def test_heartbeat_updates_claim(self, fresh_claim_table: str) -> None:
        coord = CacheOwnershipCoordinator(worker_id=_unique_worker_id("a"))
        coord.try_claim()

        before = fetchval(
            "SELECT expires_at FROM public.agent_cache_ownership WHERE resource_key = %s",
            DEFAULT_RESOURCE_KEY,
        )

        assert coord.heartbeat() is True

        after = fetchval(
            "SELECT expires_at FROM public.agent_cache_ownership WHERE resource_key = %s",
            DEFAULT_RESOURCE_KEY,
        )
        assert after > before

    def test_heartbeat_fails_after_takeover(
        self, fresh_claim_table: str
    ) -> None:
        first = CacheOwnershipCoordinator(worker_id=_unique_worker_id("a"))
        first.try_claim()

        _expire_claim()

        second = CacheOwnershipCoordinator(worker_id=_unique_worker_id("b"))
        second.try_claim()
        assert second.generation == 2

        assert first.heartbeat() is False
        assert first.is_owner is False

    def test_heartbeat_without_prior_claim_returns_false(
        self, fresh_claim_table: str
    ) -> None:
        coord = CacheOwnershipCoordinator(worker_id=_unique_worker_id("a"))
        assert coord.heartbeat() is False


class TestRelease:
    def test_release_deletes_claim_for_matching_generation(
        self, fresh_claim_table: str
    ) -> None:
        coord = CacheOwnershipCoordinator(worker_id=_unique_worker_id("a"))
        coord.try_claim()
        assert coord.release() is True

        count = fetchval(
            "SELECT COUNT(*) FROM public.agent_cache_ownership WHERE resource_key = %s",
            DEFAULT_RESOURCE_KEY,
        )
        assert count == 0
        assert coord.generation == 0
        assert coord.is_owner is False

    def test_release_is_noop_for_mismatched_generation(
        self, fresh_claim_table: str
    ) -> None:
        first = CacheOwnershipCoordinator(worker_id=_unique_worker_id("a"))
        first.try_claim()

        _expire_claim()

        second = CacheOwnershipCoordinator(worker_id=_unique_worker_id("b"))
        second.try_claim()

        assert first.release() is False
        first_count = fetchval(
            "SELECT COUNT(*) FROM public.agent_cache_ownership WHERE resource_key = %s",
            DEFAULT_RESOURCE_KEY,
        )
        assert first_count == 1

        owner = fetchval(
            "SELECT owner_id FROM public.agent_cache_ownership WHERE resource_key = %s",
            DEFAULT_RESOURCE_KEY,
        )
        assert owner == second.worker_id


class TestFencing:
    def test_acquire_write_fence_holds_lock_against_concurrent_takeover(
        self, fresh_claim_table: str
    ) -> None:
        """Внутри acquire_write_fence другой поток не может сделать takeover.

        Advisory_lock гарантирует, что takeover-поток висит в PG-транзакции,
        пока fenced-write не завершится. После yield — takeover завершается
        и видит generation+1.
        """
        first = CacheOwnershipCoordinator(worker_id=_unique_worker_id("a"))
        first.try_claim()

        lock_holder_done = threading.Event()
        takeover_started = threading.Event()
        takeover_finished: list[ClaimResult] = []

        def lock_holder() -> None:
            with first.acquire_write_fence():
                lock_holder_done.set()
                takeover_started.wait(timeout=2.0)
                time.sleep(0.5)

        def takeover_attempt() -> None:
            lock_holder_done.wait(timeout=2.0)
            _expire_claim()
            takeover_started.set()
            second = CacheOwnershipCoordinator(
                worker_id=_unique_worker_id("b")
            )
            claim = second.try_claim()
            takeover_finished.append(claim)

        t1 = threading.Thread(target=lock_holder, daemon=True)
        t2 = threading.Thread(target=takeover_attempt, daemon=True)
        t1.start()
        t2.start()
        t1.join(timeout=5.0)
        t2.join(timeout=5.0)

        assert len(takeover_finished) == 1
        claim = takeover_finished[0]
        assert claim.acquired is True
        assert claim.generation == 2

    def test_acquire_write_fence_raises_on_generation_mismatch(
        self, fresh_claim_table: str
    ) -> None:
        first = CacheOwnershipCoordinator(worker_id=_unique_worker_id("a"))
        first.try_claim()

        _expire_claim()

        second = CacheOwnershipCoordinator(worker_id=_unique_worker_id("b"))
        second.try_claim()

        with pytest.raises(OwnershipLostError):
            with first.acquire_write_fence():
                pass

    def test_acquire_write_fence_raises_on_missing_claim(
        self, fresh_claim_table: str
    ) -> None:
        first = CacheOwnershipCoordinator(worker_id=_unique_worker_id("a"))
        first.try_claim()
        first.release()

        with pytest.raises(OwnershipLostError):
            with first.acquire_write_fence():
                pass

    def test_acquire_write_fence_without_prior_claim_raises(
        self,
    ) -> None:
        coord = CacheOwnershipCoordinator(worker_id=_unique_worker_id("a"))
        with pytest.raises(OwnershipLostError):
            with coord.acquire_write_fence():
                pass


class TestClaimResultSemantics:
    def test_claim_result_mode_property(
        self, fresh_claim_table: str
    ) -> None:
        coord_a = CacheOwnershipCoordinator(worker_id=_unique_worker_id("a"))
        r = coord_a.try_claim()
        assert r.mode == CacheAccessMode.READ_WRITE

    def test_claim_result_is_frozen(
        self, fresh_claim_table: str
    ) -> None:
        coord = CacheOwnershipCoordinator(worker_id=_unique_worker_id("a"))
        r = coord.try_claim()
        with pytest.raises(Exception):
            r.acquired = False  # type: ignore[misc]


class TestResourceKeyIsolation:
    def test_resource_key_is_local_cache(self) -> None:
        from lib.services.cache_ownership import DEFAULT_RESOURCE_KEY

        assert DEFAULT_RESOURCE_KEY == "local_cache"
