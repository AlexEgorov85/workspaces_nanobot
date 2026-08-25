"""Unit-тесты ``lib/core/project_settings.py``."""

from __future__ import annotations

import pytest

from config import ConfigurationError
from lib.core.project_settings import (
    SkillSettings,
    TableEntry,
    validate_project_settings,
)


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


class TestTableEntry:
    """Pydantic-модель ``TableEntry`` и её использование в ``SkillSettings.tables``.

    Расширение формата ``tables``: помимо строк допускаются
    объекты ``{"name", "label?", "tracking_column?"}`` для задания
    per-table атрибутов. Unknown keys запрещены (``extra="forbid"``).
    """

    def test_table_entry_minimal(self) -> None:
        e = TableEntry.model_validate({"name": "oarb.audits"})
        assert e.name == "oarb.audits"
        assert e.label is None
        assert e.tracking_column is None

    def test_table_entry_full(self) -> None:
        e = TableEntry.model_validate(
            {"name": "public.scripts", "label": "scripts_registry", "tracking_column": "modified_at"}
        )
        assert e.name == "public.scripts"
        assert e.label == "scripts_registry"
        assert e.tracking_column == "modified_at"

    def test_table_entry_extra_forbidden(self) -> None:
        with pytest.raises(Exception) as excinfo:
            TableEntry.model_validate({"name": "x", "bogus": 1})
        assert "extra_forbidden" in str(excinfo.value) or "not permitted" in str(excinfo.value)

    def test_table_entry_missing_name(self) -> None:
        with pytest.raises(Exception):
            TableEntry.model_validate({"label": "x"})

    def test_skill_settings_tables_strings(self) -> None:
        """Плоский список строк (min-контракт)."""
        s = SkillSettings.model_validate({"tables": ["oarb.audits", "oarb.violations"]})
        assert s.tables == ["oarb.audits", "oarb.violations"]

    def test_skill_settings_tables_objects(self) -> None:
        """Список объектов TableEntry."""
        s = SkillSettings.model_validate({
            "tables": [
                {"name": "oarb.audits"},
                {"name": "public.scripts", "label": "scripts_registry"},
                {"name": "oarb.reports", "tracking_column": "modified_at"},
            ],
        })
        assert len(s.tables) == 3
        assert isinstance(s.tables[0], TableEntry)
        assert s.tables[0].name == "oarb.audits"
        assert s.tables[0].label is None
        assert isinstance(s.tables[1], TableEntry)
        assert s.tables[1].name == "public.scripts"
        assert s.tables[1].label == "scripts_registry"
        assert isinstance(s.tables[2], TableEntry)
        assert s.tables[2].tracking_column == "modified_at"

    def test_skill_settings_tables_mixed(self) -> None:
        """Строки и объекты в одном списке."""
        s = SkillSettings.model_validate({
            "tables": ["oarb.audits", {"name": "public.scripts", "label": "scripts_registry"}],
        })
        assert s.tables[0] == "oarb.audits"
        assert isinstance(s.tables[1], TableEntry)
        assert s.tables[1].name == "public.scripts"
        assert s.tables[1].label == "scripts_registry"

    def test_skill_settings_tables_object_unknown_key_rejected(self) -> None:
        """Опечатки в ключах объекта ловятся на старте (fail-fast)."""
        with pytest.raises(Exception) as excinfo:
            SkillSettings.model_validate(
                {"tables": [{"name": "x", "bogus": 1}]}
            )
        msg = str(excinfo.value)
        assert "not permitted" in msg or "extra_forbidden" in msg

    def test_skill_settings_tables_none(self) -> None:
        """Отсутствие ``tables`` остаётся None (не ошибка)."""
        s = SkillSettings.model_validate({})
        assert s.tables is None

    def test_table_entry_in_exports(self) -> None:
        """TableEntry экспортируется из lib.core.project_settings."""
        from lib.core.project_settings import TableEntry as Exported
        assert Exported is TableEntry
