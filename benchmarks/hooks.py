from __future__ import annotations

import time
from typing import Any

from nanobot.agent.hook import AgentHook, AgentHookContext


class BenchmarkHook(AgentHook):
    """Captures metrics during a benchmark agent run."""

    def __init__(self) -> None:
        super().__init__()
        self.tool_calls: list[dict[str, Any]] = []
        self.iterations: int = 0
        self.skills: set[str] = set()
        self.start_time: float = 0.0
        self.end_time: float = 0.0
        self.usage: dict[str, int] = {}
        self._tool_names: set[str] = set()

    async def before_iteration(self, context: AgentHookContext) -> None:
        if self.iterations == 0:
            self.start_time = time.time()
        self.iterations += 1
        if context.usage:
            self.usage = dict(context.usage)

    async def after_iteration(self, context: AgentHookContext) -> None:
        for call in context.tool_calls:
            self._tool_names.add(call.name)
            self.tool_calls.append({
                "name": call.name,
                "params": call.arguments,
                "iteration": context.iteration,
            })

    def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
        self.end_time = time.time()
        return content

    @property
    def tools_used(self) -> list[str]:
        return sorted(self._tool_names)

    @property
    def duration_sec(self) -> float:
        if self.start_time and self.end_time:
            return self.end_time - self.start_time
        return 0.0
