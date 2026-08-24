"""DisplayConfig — настройки вывода CLI-агента."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DisplayConfig:
    """Какие блоки показывать и как (typewriter, скорость)."""

    show_reasoning: bool = True
    show_tool_calls: bool = True
    show_tool_results: bool = True
    show_tool_params: bool = True
    show_progress: bool = True
    show_context_window: bool = True
    typewriter_speed: float = 0.01

    @classmethod
    def from_settings(cls, cli_settings: dict) -> DisplayConfig:
        return cls(
            show_reasoning=cli_settings.get("show_reasoning", True),
            show_tool_calls=cli_settings.get("show_tool_calls", True),
            show_tool_results=cli_settings.get("show_tool_results", True),
            show_tool_params=cli_settings.get("show_tool_params", True),
            show_progress=cli_settings.get("show_progress", True),
            show_context_window=cli_settings.get("show_context_window", True),
            typewriter_speed=float(cli_settings.get("typewriter_speed", 0.01)),
        )
