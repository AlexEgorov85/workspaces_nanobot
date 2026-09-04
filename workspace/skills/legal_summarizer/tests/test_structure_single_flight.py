"""Тесты для single-flight (Этап 54 из PLAN.md)."""

from __future__ import annotations

import pytest

from workspace.skills.legal_summarizer.scripts.structure.single_flight import (
    SingleFlightTracker, SingleFlightViolation, assert_single_flight,
)


def test_single_call_passes():
    tracker = SingleFlightTracker()
    with tracker.llm_call():
        pass
    assert tracker.is_safe() is True
    assert tracker.active == 0


def test_sequential_calls_pass():
    tracker = SingleFlightTracker()
    for _ in range(5):
        with tracker.llm_call():
            pass
    assert tracker.is_safe() is True
    assert tracker.violation_count == 0


def test_nested_call_violates():
    tracker = SingleFlightTracker()
    with pytest.raises(SingleFlightViolation):
        with tracker.llm_call():
            with tracker.llm_call():
                pass


def test_active_counter_increments():
    tracker = SingleFlightTracker()
    assert tracker.active == 0

    cm = tracker.llm_call()
    cm.__enter__()
    assert tracker.active == 1
    cm.__exit__(None, None, None)
    assert tracker.active == 0


def test_assert_single_flight_helper():
    result, tracker = assert_single_flight(lambda x: x * 2, 21)
    assert result == 42
    assert tracker.is_safe()


def test_thread_safety_serial():
    import threading
    tracker = SingleFlightTracker()

    def worker():
        with tracker.llm_call():
            pass

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert tracker.is_safe()


def test_violation_count_increments():
    tracker = SingleFlightTracker()
    for _ in range(3):
        try:
            with tracker.llm_call():
                with tracker.llm_call():
                    pass
        except SingleFlightViolation:
            pass
    assert tracker.violation_count == 3


def test_is_safe_initial():
    tracker = SingleFlightTracker()
    assert tracker.is_safe()