"""Тесты для ``lib/services/runtime_inventory.py``."""
from __future__ import annotations

import pytest


class TestCanonical:
    def test_framework_hooks_required(self) -> None:
        from lib.services.runtime_inventory import canonical_framework_hooks

        names = {h.name for h in canonical_framework_hooks()}
        assert "ToolAuditHook" in names
        assert "TerminalToolPrintHook" in names
        for h in canonical_framework_hooks():
            assert h.required, h

    def test_plugin_hooks_include_required(self) -> None:
        from lib.services.runtime_inventory import canonical_plugin_hooks

        names = {h.name for h in canonical_plugin_hooks()}
        assert "SessionFileRedirectHook" in names
        assert "RecentFilesHook" in names
        required = [h for h in canonical_plugin_hooks() if h.required]
        assert {h.name for h in required} >= {
            "SessionFileRedirectHook",
            "RecentFilesHook",
        }

    def test_plugin_hooks_have_optional_diag(self) -> None:
        from lib.services.runtime_inventory import canonical_plugin_hooks

        diag = [h for h in canonical_plugin_hooks() if "Diag" in h.name]
        assert diag
        assert not all(h.required for h in diag)

    def test_project_tools_required(self) -> None:
        from lib.services.runtime_inventory import canonical_project_tools

        required = {t.name for t in canonical_project_tools() if t.required}
        assert required == {"compact_context", "history_search", "legal_summarizer_query"}

    def test_example_tool_is_optional(self) -> None:
        from lib.services.runtime_inventory import canonical_project_tools

        ex = [t for t in canonical_project_tools() if t.name == "ExampleTool"]
        assert ex and not ex[0].required

    def test_runtime_patches_covers_known(self) -> None:
        from lib.services.runtime_inventory import canonical_runtime_patches

        names = {p.name for p in canonical_runtime_patches()}
        for required_name in (
            "assemble_outbound",
            "subagent_logging",
            "save_turn",
            "context_governor",
        ):
            assert required_name in names, required_name


class TestDiffHooks:
    def test_actual_matches_canonical(self) -> None:
        from lib.services.runtime_inventory import diff_hooks

        actual = [
            "StreamDiagnosisHook",
            "RecentFilesHook",
            "SessionFileRedirectHook",
            "ToolAuditHook",
            "TerminalToolPrintHook",
        ]
        diff = diff_hooks(actual, actual_factory_count=1)
        assert diff["missing_required"] == []
        assert diff["missing_optional"] == []
        assert diff["unexpected"] == []
        assert diff["missing_factory"] == []

    def test_missing_required(self) -> None:
        from lib.services.runtime_inventory import diff_hooks

        diff = diff_hooks(
            ["ToolAuditHook", "TerminalToolPrintHook"],
            actual_factory_count=0,
        )
        assert "SessionFileRedirectHook" in diff["missing_required"]
        assert "RecentFilesHook" in diff["missing_required"]
        assert "DatabaseLoggingHook" in diff["missing_factory"]
        assert "StreamDiagnosisHook" in diff["missing_optional"]

    def test_unexpected(self) -> None:
        from lib.services.runtime_inventory import diff_hooks

        diff = diff_hooks(
            ["ToolAuditHook", "TerminalToolPrintHook", "SomeRandomHook"],
            actual_factory_count=1,
        )
        assert "SomeRandomHook" in diff["unexpected"]


class TestDiffProjectTools:
    def test_actual_matches_canonical(self) -> None:
        from lib.services.runtime_inventory import diff_project_tools

        diff = diff_project_tools(
            registered=["compact_context", "history_search", "legal_summarizer_query"],
            skipped_disabled=["ExampleTool"],
        )
        assert diff["missing_required"] == []
        assert diff["failed"] == []
        assert diff["unexpected"] == []

    def test_missing_required(self) -> None:
        from lib.services.runtime_inventory import diff_project_tools

        diff = diff_project_tools(
            registered=["history_search"],
            skipped_disabled=["ExampleTool", "legal_summarizer_query"],
        )
        assert "compact_context" in diff["missing_required"]
        assert "legal_summarizer_query" in diff["disabled_required"]

    def test_failed_listed(self) -> None:
        from lib.services.runtime_inventory import diff_project_tools

        diff = diff_project_tools(
            registered=["compact_context", "history_search"],
            skipped_disabled=["ExampleTool"],
            failed=["legal_summarizer_query"],
        )
        assert "legal_summarizer_query" in diff["missing_required"]
        assert "legal_summarizer_query" in diff["failed"]


class TestParseProjectToolsDetail:
    def test_user_log(self) -> None:
        from lib.services.runtime_inventory import parse_project_tools_detail

        parsed = parse_project_tools_detail(
            "3 project tools registered: compact_context, history_search, "
            "legal_summarizer_query; 1 disabled by config: ExampleTool"
        )
        assert parsed["registered"] == [
            "compact_context",
            "history_search",
            "legal_summarizer_query",
        ]
        assert parsed["disabled"] == ["ExampleTool"]
        assert parsed["failed"] == []

    def test_internal_failed(self) -> None:
        from lib.services.runtime_inventory import parse_project_tools_detail

        parsed = parse_project_tools_detail(
            "[INTERNAL_FAILED] 2 project tools registered: compact_context, "
            "history_search; 1 disabled by config: ExampleTool; "
            "1 failed: legal_summarizer_query"
        )
        assert parsed["registered"] == ["compact_context", "history_search"]
        assert parsed["failed"] == ["legal_summarizer_query"]

    def test_skip_message(self) -> None:
        from lib.services.runtime_inventory import parse_project_tools_detail

        parsed = parse_project_tools_detail("workspace/tools not found — skip")
        assert parsed["registered"] == []
        assert parsed["disabled"] == []
        assert parsed["failed"] == []


class TestDiffRuntimePatches:
    def test_applied_matches_canonical(self) -> None:
        from lib.services.runtime_inventory import (
            canonical_runtime_patches, diff_runtime_patches,
        )

        # Все required-патчи должны быть либо applied, либо skipped.
        required = {
            p.name
            for p in canonical_runtime_patches()
            if p.required
        }
        applied = ["assemble_outbound", "async_save", "subagent_logging", "project_tools"]
        skipped = [
            (p, "x") for p in required if p not in applied
        ]
        diff = diff_runtime_patches(
            applied=applied,
            skipped=skipped,
            failed=[],
        )
        assert diff["missing_required"] == []
        assert diff["failed_required"] == []

    def test_failed_required_detected(self) -> None:
        from lib.services.runtime_inventory import (
            canonical_runtime_patches, diff_runtime_patches,
        )

        required = {
            p.name
            for p in canonical_runtime_patches()
            if p.name != "subagent_logging"
        }
        diff = diff_runtime_patches(
            applied=["assemble_outbound"],
            skipped=[(p, "x") for p in required if p != "assemble_outbound"],
            failed=[("subagent_logging", "import failed: foo")],
        )
        assert "subagent_logging" in diff["failed_required"]
        assert diff["missing_required"] == []

    def test_optional_failed_not_critical(self) -> None:
        from lib.services.runtime_inventory import (
            canonical_runtime_patches, diff_runtime_patches,
        )

        required = {
            p.name
            for p in canonical_runtime_patches()
            if p.required
        }
        diff = diff_runtime_patches(
            applied=sorted(required),
            skipped=[],
            failed=[("exec_timeout_cap", "shell module not loaded")],
        )
        assert diff["missing_required"] == []
        assert diff["failed_required"] == []
