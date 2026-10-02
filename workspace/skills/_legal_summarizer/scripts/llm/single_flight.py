"""Single-flight invariant enforcement.

``max_active_llm_calls == 1``. Нельзя иметь параллельных
LLM-вызовов, даже если pipeline содержит несколько батчей.

Единая реализация cross-thread single-flight boundary:

* ``LLM_FLIGHT_LOCK`` — ``threading.Lock``, общий для всех
  ``llm_batch`` / ``llm_section_reduce`` / ``llm_document_reduce``
  / ``chat_locked`` вызовов (и для map-phase ``process_context_batch``).
  Все потоки, входящие в LLM, сериализуются через этот lock.
* ``SingleFlightTracker`` — counter активных вызовов для тестов инварианта.

Раньше существовали два независимых lock'а —
``llm/calls.py::_CHAT_LOCK`` и ``execution/pipeline.py::_LLM_FLIGHT_LOCK``.
После consolidation оба указывают на ``LLM_FLIGHT_LOCK`` из этого модуля.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SingleFlightTracker:
    """Tracker для ``max_active_llm_calls == 1``.

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


# ---------------------------------------------------------------------------
# Canonical cross-thread LLM lock (single source of truth).
# ---------------------------------------------------------------------------
# Раньше: ``llm/calls.py::_CHAT_LOCK`` и ``execution/pipeline.py::_LLM_FLIGHT_LOCK``.
# Теперь: единый ``LLM_FLIGHT_LOCK``. Все подсистемы импортируют
# ``LLM_FLIGHT_LOCK`` из этого модуля.
LLM_FLIGHT_LOCK = threading.Lock()


def guarded_chat(callable_, *args, **kwargs):
    """Сериализованный LLM-вызов через единый single-flight boundary.

    Это **public API** для всех потребителей LLM boundary:
    ``llm.calls``, ``execution.pipeline`` и любые будущие подсистемы.
    Вместо прямого импорта ``threading.Lock`` / ``LLM_FLIGHT_LOCK``
    подсистемы вызывают ``guarded_chat(llm.chat, ...)``.

    Single-flight invariant (``max_active_llm_calls == 1``)
    обеспечивается через единый ``LLM_FLIGHT_LOCK`` и сохраняется
    между любыми двумя одновременными ``guarded_chat`` вызовами в
    разных потоках / event loops.

    Раньше ``execution.pipeline`` напрямую использовал
    ``with LLM_FLIGHT_LOCK:``, что нарушало архитектурную границу
    ``execution не знает о llm``. ``guarded_chat`` переносит эту
    ответственность в ``llm.single_flight``.
    """
    with LLM_FLIGHT_LOCK:
        return callable_(*args, **kwargs)


# Back-compat alias — модули и тесты, которые импортировали старый
# ``_CHAT_LOCK`` из ``llm.calls``, продолжают работать.
# То же для ``execution.pipeline._LLM_FLIGHT_LOCK``.
__all__ = [
    "SingleFlightTracker",
    "SingleFlightViolation",
    "assert_single_flight",
    "LLM_FLIGHT_LOCK",
    "guarded_chat",
]
