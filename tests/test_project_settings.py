"""Unit-тесты ``lib/core/project_settings.py``."""

from __future__ import annotations

import pytest

from config import ConfigurationError
from lib.core.project_settings import validate_project_settings


class TestValidateProjectSettings:
    def test_empty_settings_pass(self) -> None:
        result = validate_project_settings({})
        assert result.channels is None
        assert result.gateway is None

    def test_valid_full_settings(self) -> None:
        settings = {
            "version": "2.5.0",
            "channels": {
                "postgres": {
                    "worker_id": "w1",
                    "claim_strategy": "worker_pool",
                    "poll_interval": 2.0,
                    "lease_interval": 30,
                }
            },
            "gateway": {
                "print_llm_calls": False,
                "compact": {"enabled": True, "notify_in_history": True},
                "duckdb_query": {"max_rows": 500, "query_timeout_sec": 10},
                "vector_search": {"default_top_k": 5, "default_threshold": 0.7},
            },
            "cli": {"show_context_window": True, "max_iterations": 200},
            "streamlit": {"enabled": False, "error_window_sec": 600},
        }
        result = validate_project_settings(settings)
        assert result.version == "2.5.0"
        assert result.channels.postgres.claim_strategy == "worker_pool"
        assert result.gateway.duckdb_query.max_rows == 500
        assert result.cli.max_iterations == 200

    def test_unknown_keys_allowed(self) -> None:
        result = validate_project_settings(
            {"skills": {"audit_analyzer": {"db_tables": ["t1"]}}, "benchmark": {"x": 1}}
        )
        assert result.channels is None

    def test_wrong_type_bool_key(self) -> None:
        with pytest.raises(ConfigurationError) as excinfo:
            validate_project_settings({"gateway": {"print_llm_calls": "yes-please"}})
        assert "gateway.print_llm_calls" in str(excinfo.value)

    def test_wrong_claim_strategy_value(self) -> None:
        with pytest.raises(ConfigurationError) as excinfo:
            validate_project_settings(
                {"channels": {"postgres": {"claim_strategy": "both"}}}
            )
        assert "claim_strategy" in str(excinfo.value)

    def test_negative_poll_interval_rejected(self) -> None:
        with pytest.raises(ConfigurationError):
            validate_project_settings(
                {"channels": {"postgres": {"poll_interval": -1}}}
            )

    def test_threshold_out_of_range_rejected(self) -> None:
        with pytest.raises(ConfigurationError):
            validate_project_settings(
                {"gateway": {"vector_search": {"default_threshold": 1.5}}}
            )

    def test_all_problems_listed_at_once(self) -> None:
        with pytest.raises(ConfigurationError) as excinfo:
            validate_project_settings({
                "channels": {"postgres": {"poll_interval": 0}},
                "cli": {"max_iterations": -5},
            })
        msg = str(excinfo.value)
        assert "poll_interval" in msg
        assert "max_iterations" in msg

    def test_none_values_treated_as_absent(self) -> None:
        result = validate_project_settings({"gateway": {"compact": None}})
        assert result.gateway.compact is None
