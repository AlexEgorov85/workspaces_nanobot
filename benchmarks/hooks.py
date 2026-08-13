"""Хук для сбора метрик выполнения агента во время прогона бенчмарка.

Собирает информацию о вызовах инструментов, итерациях, навыках и времени выполнения.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path as _Path
from typing import Any

# Корень проекта + workspace — чтобы импорты lib.workspace / hooks работали
# независимо от рабочего каталога (тот же паттерн, что и в benchmarks/db.py).
_ROOT = _Path(__file__).resolve().parents[1]
_WORKSPACE = _ROOT / "workspace"
for _p in (str(_ROOT), str(_WORKSPACE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from nanobot.agent.hook import AgentHookContext

from hooks.base_tool_tracking_hook import BaseToolTrackingHook


class BenchmarkHook(BaseToolTrackingHook):
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
        for call in self._iter_tool_calls(context):
            name = self._tool_call_name(call)
            self._tool_names.add(name)
            self.tool_calls.append({
                "name": name,
                "params": self._tool_call_arguments(call),
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
