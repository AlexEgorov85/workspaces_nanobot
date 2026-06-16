"""Хук для сбора метрик выполнения агента во время прогона бенчмарка.

Собирает информацию о вызовах инструментов, итерациях, навыках и времени выполнения.
"""

from __future__ import annotations

import time
from typing import Any

from nanobot.agent.hook import AgentHook, AgentHookContext


class BenchmarkHook(AgentHook):
    """Перехватывает метрики во время выполнения агента в бенчмарке."""

    def __init__(self) -> None:
        """Инициализация хука с пустыми счётчиками."""

        super().__init__()
        self.tool_calls: list[dict[str, Any]] = []
        self.iterations: int = 0
        self.skills: set[str] = set()
        self.start_time: float = 0.0
        self.end_time: float = 0.0
        self.usage: dict[str, int] = {}
        self._tool_names: set[str] = set()

    async def before_iteration(self, context: AgentHookContext) -> None:
        """Действие перед каждой итерацией агента: засекает время старта и увеличивает счётчик.

        Args:
            context: Контекст итерации агента.
        """
        if self.iterations == 0:
            self.start_time = time.time()
        self.iterations += 1
        if context.usage:
            self.usage = dict(context.usage)

    async def after_iteration(self, context: AgentHookContext) -> None:
        """Действие после каждой итерации: собирает данные о вызовах инструментов.

        Args:
            context: Контекст завершённой итерации агента.
        """
        for call in context.tool_calls:
            self._tool_names.add(call.name)
            self.tool_calls.append({
                "name": call.name,
                "params": call.arguments,
                "iteration": context.iteration,
            })

    def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
        """Финализация контента: фиксирует время окончания.

        Args:
            context: Контекст после завершения всех итераций.
            content: Итоговый контент ответа агента.

        Returns:
            Контент без изменений.
        """
        self.end_time = time.time()
        return content

    @property
    def tools_used(self) -> list[str]:
        """Список инструментов, использованных агентом (отсортированный)."""
        return sorted(self._tool_names)

    @property
    def duration_sec(self) -> float:
        """Общая длительность выполнения агента в секундах."""

        if self.start_time and self.end_time:
            return self.end_time - self.start_time
        return 0.0
