"""Тесты для ``tools/diagnose_startup.py``."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL = REPO_ROOT / "tools" / "diagnose_startup.py"


# Лог боевого старта ПОСЛЕ фазы 6 (``enterprise-mcp-platform``): в нём
# есть четвёртый project tool ``document_read`` (нативная замена патча
# ``document_text_threshold``) и фреймворковый хук
# ``ToolResultArchiveHook`` (нативная замена патча ``save_turn``) — оба
# обязательные элементы канона, поэтому «дофазовая» фикстура давала бы
# CRITICAL вместо проверяемого здесь DRIFT. Намеренный дрейф остался
# один: ``async_save``/``document_text_threshold`` в applied — их больше
# нет в каноне, это и есть материал для exit code 2.
USER_LOG = """
\u2713 Hooks connected: StreamDiagnosisHook, RecentFilesHook, SessionFileRedirectHook, ToolResultArchiveHook, ToolAuditHook, TerminalToolPrintHook, McpIdentityHook, 1 hook factory (per-turn)
Registered 22 tools: ['apply_patch', 'run_cli_app', 'create_goal', 'edit_file', 'exec_session', 'exec', 'find_files', 'grep', 'list_dir', 'list_exec_sessions', 'list_sessions', 'message', 'my', 'read_file', 'read_session', 'search_sessions', 'send_session_message', 'spawn', 'update_goal', 'web_fetch', 'web_search', 'write_file']
Custom (project) tools: 5 project tools registered: audit_analyzer_query, compact_context, history_search, legal_summarizer_query, document_read; 1 disabled by config: ExampleTool
Runtime patches
----------------
\u2713 assemble_outbound
\u2713 async_save
\u2713 subagent_logging
\u2713 document_text_threshold
\u26a0 save_turn skipped: persist_threshold <= 0
\u26a0 context_governor skipped: persist_threshold <= 0
\u2717 exec_timeout_cap failed: shell module not loaded
1 runtime patch(es) failed: ['exec_timeout_cap']
"""


CRITICAL_LOG = """
\u2713 Hooks connected: ToolAuditHook, TerminalToolPrintHook
Registered 22 tools: ['apply_patch', 'exec']
Custom (project) tools: 5 project tools registered: audit_analyzer_query, compact_context, history_search, legal_summarizer_query, document_read; 1 disabled by config: ExampleTool
Runtime patches
----------------
\u2713 assemble_outbound
\u2717 subagent_logging failed: import failed: foo
1 runtime patch(es) failed: ['subagent_logging']
"""


EMPTY_LOG = """
Some unrelated text
"""


#: Форма настоящего startup-лога gateway, а не синтетики: блок патчей печатает
#: rich, и сразу за ним идут статусные строки старта — тоже с «✓». Блока,
#: закрытого пустой строкой, в реальном выводе нет, поэтому парсер обязан
#: сам определять конец блока по первой непохожей строке. Иначе «✓ DB pool»
#: и «✓ PostgreSQL channel enabled» засчитывались как патчи.
REAL_SHAPED_LOG = (
    "✓ Hooks connected: StreamDiagnosisHook, RecentFilesHook, "
    "SessionFileRedirectHook, ToolResultArchiveHook, ToolAuditHook, "
    "TerminalToolPrintHook, 1 hook factory (per-turn)\n"
    "Registered 22 tools: ['apply_patch', 'exec', 'read_file']\n"
    "Custom (project) tools: 5 project tools registered: audit_analyzer_query, "
    "compact_context, document_read, history_search, legal_summarizer_query "
    "— workspace=...\\workspace\n"
    "Runtime patches\n"
    "----------------\n"
    "✓ context_governor\n"
    "    (управлять окном контекста)\n"
    "✓ assemble_outbound\n"
    "    (внедрить tool_audit и recent_files в финальный outbound)\n"
    "✓ subagent_logging\n"
    "🐈 Starting nanobot gateway · project v2.5.2 (nanobot 0.3.5) · profile=test...\n"
    "✓ DB pool: workers 3/1 (max 4), connected 2\n"
    "Redis channel disabled\n"
    "✓ PostgreSQL channel enabled\n"
    "✓ Channels enabled: websocket, postgres\n"
    "✓ enterprise-mcp: 14 операций, процесс поднят\n"
)


def _run(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    import os
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        input=stdin,
        timeout=60,
        env=env,
    )


class TestParser:
    def test_user_log_parses(self) -> None:
        from tools.diagnose_startup import parse_startup_log

        facts = parse_startup_log(USER_LOG)
        assert facts.hook_names == [
            "StreamDiagnosisHook", "RecentFilesHook",
            "SessionFileRedirectHook", "ToolResultArchiveHook",
            "ToolAuditHook", "TerminalToolPrintHook", "McpIdentityHook",
        ]
        assert facts.hook_factory_count == 1
        assert len(facts.builtin_tool_names) == 22
        assert facts.project_tools_registered == [
            "audit_analyzer_query", "compact_context", "history_search",
            "legal_summarizer_query", "document_read",
        ]
        assert facts.project_tools_disabled == ["ExampleTool"]
        assert "subagent_logging" in facts.runtime_patches_applied
        assert ("exec_timeout_cap", "shell module not loaded") in facts.runtime_patches_failed
        assert facts.runtime_patches_failed_header == ["exec_timeout_cap"]

    def test_empty_log_flags_error(self) -> None:
        from tools.diagnose_startup import parse_startup_log

        facts = parse_startup_log(EMPTY_LOG)
        assert facts.parse_errors
        assert facts.is_empty

    def test_patches_block_ends_at_first_foreign_line(self) -> None:
        """Статусные строки старта не должны попадать в патчи.

        Реальный лог печатает блок патчей через rich и сразу продолжает
        статусными строками — тоже с «✓». Парсер обязан останавливать блок
        по первой непохожей строке, а не ждать пустой.
        """
        from tools.diagnose_startup import parse_startup_log

        facts = parse_startup_log(REAL_SHAPED_LOG)
        assert facts.runtime_patches_applied == [
            "context_governor",
            "assemble_outbound",
            "subagent_logging",
        ], (
            "в патчи попали строки старта: "
            f"{facts.runtime_patches_applied}"
        )

    def test_purpose_lines_are_not_patches(self) -> None:
        """Строка «(назначение патча)» — продолжение записи, не новый патч."""
        from tools.diagnose_startup import parse_startup_log

        facts = parse_startup_log(REAL_SHAPED_LOG)
        assert not any(
            name.startswith("(") or "управлять" in name
            for name in facts.runtime_patches_applied
        )

    def test_hooks_are_read_without_wrapping_dependency(self) -> None:
        """Список хуков читается целиком, даже если одна строка.

        Раньше баннер печатался rich'ем с переносом по ширине консоли, и
        парсер видел только первую физическую строку — «MISSING REQUIRED»
        на здоровом старте.
        """
        from tools.diagnose_startup import parse_startup_log

        facts = parse_startup_log(REAL_SHAPED_LOG)
        assert len(facts.hook_names) == 6
        assert "ToolAuditHook" in facts.hook_names
        assert facts.hook_factory_count == 1



class TestCli:
    def test_user_log_returns_drift_code(self, tmp_path: Path) -> None:
        log = tmp_path / "user.log"
        log.write_text(USER_LOG, encoding="utf-8")
        r = _run("--log", str(log), "--no-color", "--json")
        assert r.returncode == 2, (r.stdout, r.stderr)
        data = json.loads(r.stdout)
        assert "subagent_logging" in data["facts"]["runtime_patches_applied"]
        assert data["diff"]["hooks"]["missing_required"] == []
        assert data["diff"]["project_tools"]["missing_required"] == []
        assert data["diff"]["runtime_patches"]["missing_required"] == []

    def test_critical_log_returns_exit_1(self, tmp_path: Path) -> None:
        log = tmp_path / "critical.log"
        log.write_text(CRITICAL_LOG, encoding="utf-8")
        r = _run("--log", str(log), "--no-color")
        assert r.returncode == 1, (r.stdout, r.stderr)
        assert "MISSING REQUIRED" in r.stderr
        assert "FAILED REQUIRED" in r.stderr

    def test_empty_log_returns_exit_1(self, tmp_path: Path) -> None:
        """Пустой/обрезанный лог → всё missing → critical → exit 1."""
        log = tmp_path / "empty.log"
        log.write_text(EMPTY_LOG, encoding="utf-8")
        r = _run("--log", str(log), "--no-color")
        assert r.returncode == 1, (r.stdout, r.stderr)
        assert "CRITICAL" in r.stderr

    def test_stdin_input(self) -> None:
        r = _run("--no-color", stdin=USER_LOG)
        assert r.returncode in (0, 2), (r.stdout, r.stderr)

    def test_strict_promotes_to_exit_1(self, tmp_path: Path) -> None:
        log = tmp_path / "user.log"
        log.write_text(USER_LOG, encoding="utf-8")
        r = _run("--log", str(log), "--no-color", "--strict")
        assert r.returncode == 1, (r.stdout, r.stderr)
