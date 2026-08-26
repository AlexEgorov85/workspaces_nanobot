"""Тесты ``lib/services/runtime_health.py``.

Health / Readiness — operational status. Различает liveness (пульс
процесса) и readiness (готовность к обработке задач с учётом
зависимостей).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


class TestRuntimeHealth:
    """``RuntimeHealth`` — liveness процесса."""

    def test_initial_state_not_alive(self):
        from lib.services.runtime_health import RuntimeHealth

        h = RuntimeHealth()
        assert h.is_alive() is False
        assert h.status() == "DEAD"

    def test_mark_started_makes_alive(self):
        from lib.services.runtime_health import RuntimeHealth

        h = RuntimeHealth()
        h.mark_started()
        assert h.is_alive() is True
        assert h.status() == "ALIVE"

    def test_mark_stopped_makes_dead(self):
        from lib.services.runtime_health import RuntimeHealth

        h = RuntimeHealth()
        h.mark_started()
        h.mark_stopped()
        assert h.is_alive() is False
        assert h.status() == "DEAD"


class TestRuntimeReadiness:
    """``RuntimeReadiness`` — проверка зависимостей."""

    def test_no_checks_is_ready(self):
        from lib.services.runtime_health import RuntimeReadiness

        r = RuntimeReadiness()
        report = r.check()
        assert report.status == "READY"
        assert report.components == ()

    def test_all_up_required_is_ready(self):
        from lib.services.runtime_health import (
            ComponentStatus,
            RuntimeReadiness,
        )

        r = RuntimeReadiness()
        r.register("postgres", lambda: ComponentStatus(
            name="postgres", required=True, status="UP",
        ))
        r.register("duckdb", lambda: ComponentStatus(
            name="duckdb", required=True, status="UP",
        ))
        report = r.check()
        assert report.status == "READY"
        assert len(report.components) == 2

    def test_required_down_is_not_ready(self):
        from lib.services.runtime_health import (
            ComponentStatus,
            RuntimeReadiness,
        )

        r = RuntimeReadiness()
        r.register("postgres", lambda: ComponentStatus(
            name="postgres", required=True, status="DOWN",
            detail="connection refused",
        ))
        report = r.check()
        assert report.status == "NOT_READY"

    def test_optional_down_with_required_up_is_degraded(self):
        from lib.services.runtime_health import (
            ComponentStatus,
            RuntimeReadiness,
        )

        r = RuntimeReadiness()
        r.register("postgres", lambda: ComponentStatus(
            name="postgres", required=True, status="UP",
        ))
        r.register("vector_search", lambda: ComponentStatus(
            name="vector_search", required=False, status="DOWN",
            detail="no faiss index",
        ))
        report = r.check()
        assert report.status == "DEGRADED"

    def test_lambda_returning_none_means_up(self):
        from lib.services.runtime_health import RuntimeReadiness

        r = RuntimeReadiness()
        r.register("postgres", lambda: None)
        report = r.check()
        assert report.status == "READY"
        assert report.components[0].status == "UP"

    def test_check_swallows_exceptions_in_probe(self):
        from lib.services.runtime_health import RuntimeReadiness

        r = RuntimeReadiness()
        r.register("broken", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        report = r.check()
        assert report.status == "NOT_READY"
        assert report.components[0].status == "DOWN"
        assert "boom" in report.components[0].detail

    def test_register_invalid_return_type_raises(self):
        from lib.services.runtime_health import RuntimeReadiness

        r = RuntimeReadiness()
        r.register("bad", lambda: "not a ComponentStatus")
        with pytest.raises(TypeError, match="bad"):
            r.check()

    def test_to_dict_round_trip(self):
        from lib.services.runtime_health import (
            ComponentStatus,
            RuntimeReadiness,
        )

        r = RuntimeReadiness()
        r.register("postgres", lambda: ComponentStatus(
            name="postgres", required=True, status="UP", detail="3 workers",
        ))
        report = r.check()
        d = report.to_dict()
        assert d["status"] == "READY"
        assert d["components"][0]["name"] == "postgres"
        assert d["components"][0]["detail"] == "3 workers"


class TestComputeOverallStatus:
    """``compute_overall_status`` — агрегатор статусов."""

    def test_empty_is_ready(self):
        from lib.services.runtime_health import compute_overall_status

        assert compute_overall_status([]) == "READY"

    def test_only_optional_down_is_degraded(self):
        from lib.services.runtime_health import (
            ComponentStatus,
            compute_overall_status,
        )

        components = [
            ComponentStatus(name="postgres", required=True, status="UP"),
            ComponentStatus(name="vector", required=False, status="DOWN"),
        ]
        assert compute_overall_status(components) == "DEGRADED"

    def test_required_down_takes_priority(self):
        from lib.services.runtime_health import (
            ComponentStatus,
            compute_overall_status,
        )

        components = [
            ComponentStatus(name="postgres", required=True, status="DOWN"),
            ComponentStatus(name="vector", required=False, status="DOWN"),
        ]
        assert compute_overall_status(components) == "NOT_READY"


class TestApplicationContextIntegration:
    """ApplicationContext подключает readiness-проверки."""

    def test_application_context_has_runtime_health(self):
        from lib.core.application_context import ApplicationContext
        from lib.services.runtime_health import RuntimeHealth, RuntimeReadiness

        # Проверяем только что поля определены в классе.
        assert "runtime_health" in ApplicationContext.__annotations__
        assert "runtime_readiness" in ApplicationContext.__annotations__
        assert "RuntimeHealth" in dir(__import__(
            "lib.services.runtime_health", fromlist=["RuntimeHealth"]
        ))
        assert "RuntimeReadiness" in dir(__import__(
            "lib.services.runtime_health", fromlist=["RuntimeReadiness"]
        ))