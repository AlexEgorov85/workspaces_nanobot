"""Single-flight invariant check (PLAN §54).

PLAN §54: ``max_active_llm_calls == 1``. Нельзя иметь параллельных
LLM-вызовов, даже если pipeline содержит несколько батчей.

Этот модуль предоставляет ``SingleFlightTracker`` — counter активных
LLM-вызов. Используется runtime'ом (через context manager) для
проверки invariant.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SingleFlightTracker:
    """Tracker для ``max_active_llm_calls == 1`` (PLAN §54).

    Использование::

        tracker = SingleFlightTracker()
        with tracker.llm_call():
            ...do work...

    ``violation_count`` инкрементируется, если две ``with`` блока
    вложены или активны одновременно.
    """

    _active: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    violation_count: int = 0

    @contextmanager
    def llm_call(self) -> Any:
        self._lock.acquire()
        try:
            if self._active >= 1:
                self.violation_count += 1
                raise SingleFlightViolation(
                    f"max_active_llm_calls > 1 (current={self._active})",
                )
            self._active += 1
        finally:
            self._lock.release()
        try:
            yield
        finally:
            self._lock.acquire()
            try:
                self._active -= 1
            finally:
                self._lock.release()

    @property
    def active(self) -> int:
        return self._active

    def is_safe(self) -> bool:
        return self.violation_count == 0


class SingleFlightViolation(RuntimeError):
    """Raised when multiple LLM calls overlap."""


def assert_single_flight(
    fn,
    *args,
    tracker: SingleFlightTracker | None = None,
    **kwargs,
) -> tuple[Any, SingleFlightTracker]:
    """Запустить ``fn`` под single-flight guard.

    Returns:
        tuple ``(result, tracker)``.
    """
    t = tracker or SingleFlightTracker()
    with t.llm_call():
        result = fn(*args, **kwargs)
    return result, t


__all__ = [
    "SingleFlightTracker",
    "SingleFlightViolation",
    "assert_single_flight",
]