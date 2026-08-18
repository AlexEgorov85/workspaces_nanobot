"""Базовый класс для хуков агента, отслеживающих вызовы инструментов.

Содержит общие хелперы для извлечения ``tool_calls`` из контекста и
(имя, аргументы) из отдельного ``tool_call``. Конкретные хуки
(``ToolAuditHook``, ``DatabaseLoggingHook``, ``BenchmarkHook``) наследуют
его и реализуют собственные хуки-методы и потребителей данных: дренаж в
аудит, логирование в БД, метрики для бенчмарков.

Живёт в ``lib/hooks/`` (фреймворковый каркас), а не в ``workspace/hooks/``:
плагин-директория содержит только самодостаточные хуки с контрактом
``cls(workspace_dir=...)``, базовый класс туда не попадает.
"""

from __future__ import annotations

from typing import Any

from nanobot.agent import AgentHook


class BaseToolTrackingHook(AgentHook):
    """Каркас хука, работающего с ``tool_calls`` агента.

    Подклассы переопределяют нужные хуки (``before_execute_tools`` /
    ``before_execute_tool`` / ``after_iteration``) и решают, что делать
    с собранными данными.
    """

    def __init__(self) -> None:
        super().__init__()

    # -- хелперы для работы с tool_calls ---------------------------------

    def _iter_tool_calls(self, context: Any) -> list:
        """Вернуть список ``tool_calls`` из контекста (пусто при отсутствии)."""
        calls = getattr(context, "tool_calls", None) or []
        return calls if isinstance(calls, list) else []

    def _tool_call_name(self, tool_call: Any) -> str:
        return str(getattr(tool_call, "name", "?"))

    def _tool_call_arguments(self, tool_call: Any) -> Any:
        return getattr(tool_call, "arguments", {})

    def _tool_call_id(self, tool_call: Any) -> str:
        """Стабильный строковый id tool_call (fallback на id() объекта)."""
        return str(getattr(tool_call, "id", None) or id(tool_call))

    def _tool_call_info(self, tool_call: Any) -> dict:
        """Нормализовать ``tool_call`` в ``{name, arguments}`` (arguments — dict)."""
        arguments = self._tool_call_arguments(tool_call)
        if not isinstance(arguments, dict):
            arguments = {}
        return {
            "name": self._tool_call_name(tool_call),
            "arguments": arguments,
        }
