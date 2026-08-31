"""``column_descriptions`` — generic tool для поиска подсказок по колонкам.

Регистрируется автоматически через ``RuntimePatcher.patch_project_tools``
(см. ``lib/services/runtime_patcher.py``).

Тонкий adapter поверх ``lib.services.column_descriptions.ColumnDescriptionsResolver``:
  * читает ``tools.column_descriptions.entries`` (inline) или
    ``tools.column_descriptions.data_file`` (JSON-файл) из
    ``ctx._settings_ref``;
  * делегирует ``lookup``/``all_entries`` в resolver;
  * оборачивает результат в JSON-контракт tool'а.

Контракт и поведение описаны в ``docs/skill-tool-architecture.md`` §8.2.

Domain-free: этот файл не знает ни про какие конкретные таблицы.
Словарь термин→колонка полностью конфигурируется через
``config.json::tools.column_descriptions`` (или ``data_file``)
текущей инсталляции. Resolver (``lib.services.column_descriptions``)
тоже не знает про домен — это generic механизм.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from nanobot.agent.tools.base import Tool, tool_parameters
from pydantic import BaseModel, Field

from lib.services.column_descriptions import ColumnDescriptionsResolver


__all__ = [
    "ColumnDescriptionsTool",
    "ColumnDescriptionsToolConfig",
]


class ColumnDescriptionsToolConfig(BaseModel):
    """Конфиг секции ``tools.column_descriptions`` в ``config.json``."""

    enable: bool = True
    data_file: str | None = None
    max_result_chars: int = Field(default=16_000, ge=1000, le=200_000)


@tool_parameters({
    "type": "object",
    "properties": {
        "term": {
            "type": "string",
            "description": (
                "Термин/фраза для поиска. Ищется вхождение токенов "
                "термина в синонимы ключей (case-insensitive). "
                "Если не передан — возвращаются все entries."
            ),
        },
        "match_all": {
            "type": "boolean",
            "default": False,
            "description": "Вернуть все entries (term игнорируется).",
        },
        "max_matches": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100,
            "description": "Лимит числа возвращаемых matches (default 20).",
            "default": 20,
        },
    },
})
class ColumnDescriptionsTool(Tool):
    """Поиск подсказок термин→колонка для system prompt NL→SELECT."""

    config_key: ClassVar[str] = "column_descriptions"

    def __init__(self, *, config: ColumnDescriptionsToolConfig) -> None:
        self.config = config
        self._resolver: ColumnDescriptionsResolver | None = None
        self._entries_override: dict[str, list[str]] | None = None

    @classmethod
    def config_cls(cls):
        return ColumnDescriptionsToolConfig

    @classmethod
    def _read_settings_section(cls, ctx: Any) -> dict[str, Any]:
        """Прочитать секцию ``tools.<config_key>`` из ``ctx._settings_ref``."""
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
        out: dict[str, Any] = {}
        for field_name in ("enable", "data_file", "max_result_chars"):
            if hasattr(section, field_name):
                out[field_name] = getattr(section, field_name)
        if not out:
            try:
                out = dict(vars(section))
            except Exception:
                pass
        if "entries" not in out and isinstance(section, dict):
            out["entries"] = section.get("entries")
        return out

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        section = cls._read_settings_section(ctx)
        return bool(section.get("enable", True))

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        section = cls._read_settings_section(ctx)
        config_cls = cls.config_cls()
        try:
            config = config_cls(**section)
        except Exception:
            config = config_cls()
        instance = cls(config=config)
        instance._entries_override = section.get("entries")
        return instance

    @property
    def name(self) -> str:
        return "column_descriptions"

    @property
    def description(self) -> str:
        return (
            "Возвращает структурированный словарь подсказок (термин → "
            "колонка) для подмешивания в system prompt NL→SELECT. "
            "Аргументы: term (опц., строка для поиска), match_all "
            "(вернуть все entries), max_matches (default 20). "
            "Каждый match — {terms: [...], columns: [...]}. "
            "Полезно перед вызовом nl_sql_generate, чтобы LLM не "
            "галлюцинировал имена колонок."
        )

    def _get_resolver(self) -> ColumnDescriptionsResolver:
        if self._resolver is None:
            source: dict[str, list[str]] | str | None
            if isinstance(self._entries_override, dict) and self._entries_override:
                source = self._entries_override
            elif self.config.data_file:
                source = self.config.data_file
            else:
                source = None
            self._resolver = ColumnDescriptionsResolver(entries_source=source)
        return self._resolver

    def lookup(self, term: str, *, max_matches: int = 5) -> list[dict[str, Any]]:
        """Синхронный in-process API для ``nl_sql_generate``.

        Делегирует в resolver. Сохранён для back-compat с вызывающим
        кодом (см. ``workspace/tools/nl_sql_generate.py``).
        """
        return self._get_resolver().lookup(term, max_matches=max_matches)

    async def execute(
        self,
        *,
        term: str | None = None,
        match_all: bool = False,
        max_matches: int = 20,
        **_kwargs: Any,
    ) -> str:
        resolver = self._get_resolver()
        resolver.prime()
        load_error = resolver.load_error
        if load_error:
            return self._error("load_failed", load_error)
        if match_all or not term:
            matches = resolver.all_entries(max_matches=max_matches)
        else:
            matches = resolver.lookup(term, max_matches=max(1, int(max_matches)))

        payload = {
            "status": "success",
            "term": term or "",
            "matches": matches,
            "count": len(matches),
        }
        text = json.dumps(payload, ensure_ascii=False, default=str)
        if len(text) > self.config.max_result_chars:
            return self._error(
                "result_too_large",
                f"Результат ({len(text)} символов) превышает лимит "
                f"{self.config.max_result_chars}. Уменьшите max_matches.",
            )
        return text

    @staticmethod
    def _error(error_type: str, message: str) -> str:
        payload = {
            "status": "error",
            "error_type": error_type,
            "message": message,
        }
        return json.dumps(payload, ensure_ascii=False)