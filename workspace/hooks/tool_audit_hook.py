from __future__ import annotations

from typing import Any

from nanobot.agent import AgentHook, AgentHookContext


class ToolAuditHook(AgentHook):
    """Accumulates every tool call (name, args, status, error, result preview)
    across all iterations of a turn so the caller can inject the full audit
    trail into ``OutboundMessage.metadata["_tool_audit"]``."""

    def __init__(self) -> None:
        super().__init__()
        self._entries: list[dict[str, Any]] = []
        self._pending_start: int = 0

    async def before_execute_tools(self, ctx: AgentHookContext) -> None:
        self._pending_start = len(self._entries)
        for tc in ctx.tool_calls:
            arguments = tc.arguments if isinstance(tc.arguments, dict) else {}
            self._entries.append({
                "name": tc.name,
                "arguments": arguments,
                "status": "started",
                "error": None,
                "result_preview": None,
                "iteration": ctx.iteration,
            })

    async def after_iteration(self, ctx: AgentHookContext) -> None:
        for i, ev in enumerate(ctx.tool_events):
            idx = self._pending_start + i
            if idx >= len(self._entries):
                continue
            status = ev.get("status", "unknown")
            self._entries[idx]["status"] = status
            detail = ev.get("detail", "")
            if status == "error":
                self._entries[idx]["error"] = detail
            elif status == "ok" and detail:
                self._entries[idx]["result_preview"] = detail[:200]

    def drain(self) -> list[dict[str, Any]]:
        entries = self._entries
        self._entries = []
        return entries
