"""Шаблон кастомного tool'а проекта.

Скопируйте файл, переименуйте класс и ``config_key``, допишите ``execute``.
Шаблон следует конвенциям nanobot без дополнительных обёрток.

Конфиг в ``config.json``::

    {
      "tools": {
        "example": {
          "enable": true,
          "maxChars": 8000
        }
      }
    }

Конвенция по чтению кастомных секций (``config_key``):

  pydantic-схема ``ToolsConfig`` из nanobot (см.
  ``nanobot/config/schema.py``) объявляет только фиксированный набор
  подсекций (``web``/``exec``/``file``/``cliApps``/...); всё, что в неё
  не входит — отбрасывается при валидации. Поэтому ``ctx.config.example``
  НЕ существует: pydantic просто не пропустил эту секцию в
  ``agent.tools_config``.

  Чтение кастомных настроек идёт через ``ctx._settings_ref`` (полный
  pydantic-объект ``Settings``, который кладёт туда
  ``RuntimePatcher.patch_project_tools``). Это общий путь для
  ``compact_context`` (``gateway.compact.*``) и ``audit_analyzer_tool``
  (``gateway.audit_predefined.*`` / ``gateway.audit_vector.*``).
  Секции под наши tool'ы естественно класть под ``tools.<config_key>``
  (там, где их уже ищет пользователь в ``config.json``).
"""
from __future__ import annotations

from typing import Any, ClassVar

from nanobot.agent.tools.base import Tool, tool_parameters
from pydantic import BaseModel


class ExampleToolConfig(BaseModel):
    """Конфиг секции ``tools.example`` в ``config.json``."""

    enable: bool = True
    max_chars: int = 8000


@tool_parameters({
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": "Текст, длину которого нужно посчитать.",
        },
    },
    "required": ["text"],
})
class ExampleTool(Tool):
    """Возвращает длину переданного текста. Шаблон для копирования."""

    config_key: ClassVar[str] = "example"

    @classmethod
    def config_cls(cls):
        return ExampleToolConfig

    @classmethod
    def _read_settings_section(cls, ctx: Any) -> dict[str, Any]:
        """Прочитать секцию ``tools.<config_key>`` из ``_settings_ref``.

        ``ctx.config`` (``agent.tools_config``) — это pydantic-объект
        ``ToolsConfig``, который знает только встроенные подсекции и
        отбрасывает неизвестные. Поэтому кастомные tool'ы читают свои
        настройки из полного ``Settings`` через ``ctx._settings_ref``.
        """
        settings = getattr(ctx, "_settings_ref", None)
        if settings is None:
            return {}
        try:
            tools_section = settings.tools
        except AttributeError:
            return {}
        if tools_section is None:
            return {}
        try:
            section = getattr(tools_section, cls.config_key)
        except AttributeError:
            return {}
        if section is None:
            return {}
        if isinstance(section, dict):
            return dict(section)
        try:
            return dict(section)
        except Exception:
            return {"enable": bool(getattr(section, "enable", True))}

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        section = cls._read_settings_section(ctx)
        return bool(section.get("enable", True))

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        section = cls._read_settings_section(ctx)
        try:
            config = cls.config_cls()(**section)
        except Exception:
            config = cls.config_cls()
        return cls(config=config)

    def __init__(self, *, config: ExampleToolConfig) -> None:
        self.config = config

    @property
    def name(self) -> str:
        return "example_tool"

    @property
    def description(self) -> str:
        return (
            "Возвращает длину переданного текста в символах. "
            "Шаблон для кастомных tool'ов проекта."
        )

    async def execute(self, *, text: str, **_kwargs: Any) -> str:
        result = f"Длина текста: {len(text)} символов."
        limit = self.config.max_chars
        if limit > 0 and len(result) > limit:
            result = result[:limit] + f"\n\n... ({len(result) - limit} chars truncated)"
        return result
