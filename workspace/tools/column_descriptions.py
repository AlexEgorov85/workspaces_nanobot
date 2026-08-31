"""``column_descriptions`` — tool для поиска подсказок по колонкам.

Регистрируется автоматически через ``RuntimePatcher.patch_project_tools``
(см. ``lib/services/runtime_patcher.py``).

Назначение: вернуть структурированный словарь подсказок (термин → колонка),
которые подмешиваются в system prompt ``nl_sql_generate``. Заменяет
бывший ``workspace/skills/audit_analyzer/scripts/column_hints.py``.

Конфиг в ``config.json``::

    {
      "tools": {
        "column_descriptions": {
          "enable": true,
          "entries": {
            "audited objects|objects of audit|проверяемые|объекты проверок": [
              "oarb.audits.auditee_entity"
            ],
            "violations|нарушения": ["oarb.violations"]
          }
        }
      }
    }

Опционально entries могут быть вынесены в отдельный JSON-файл через
``data_file`` (например, ``data_file: "workspace/data/column_descriptions.json"``);
это полезно для больших словарей. Если указан ``data_file`` — он
перекрывает inline ``entries``.

Формат ``data_file`` / ``entries``::

    {
      "audited objects|objects of audit|проверяемые|объекты проверок": [
        "oarb.audits.auditee_entity"
      ],
      "violations|нарушения": ["oarb.violations"]
    }

Ключ может содержать ``|`` — это список синонимов; совпадение
с любым из них считается положительным.

Если ни ``data_file``, ни ``entries`` не заданы — tool возвращает
пустой список matches.

Контракт и поведение описаны в ``docs/skill-tool-architecture.md`` §8.2.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, ClassVar

from nanobot.agent.tools.base import Tool, tool_parameters
from pydantic import BaseModel, Field


__all__ = ["ColumnDescriptionsTool", "ColumnDescriptionsToolConfig"]


class ColumnDescriptionsToolConfig(BaseModel):
    """Конфиг секции ``tools.column_descriptions`` в ``config.json``."""

    enable: bool = True
    data_file: str | None = None
    max_result_chars: int = Field(default=16_000, ge=1000, le=200_000)


_TOKEN_SPLIT_RE = re.compile(r"[^a-zа-яё0-9]+")


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
        self._entries_cache: dict[str, list[str]] | None = None

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
            "Каждый match — {terms: [...], columns: [\"schema.table.column\"]}. "
            "Полезно перед вызовом nl_sql_generate, чтобы LLM не "
            "галлюцинировал имена колонок."
        )

    async def execute(
        self,
        *,
        term: str | None = None,
        match_all: bool = False,
        max_matches: int = 20,
        **_kwargs: Any,
    ) -> str:
        entries, load_error = self._load_entries()
        if entries is None:
            return self._error(
                "load_failed",
                f"Не удалось загрузить entries column_descriptions: {load_error}",
            )

        if match_all or not term:
            matches = self._all_entries_as_matches(entries)
        else:
            matches = self._search(term, entries)

        matches = matches[: max(1, int(max_matches))]

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

    # ---------- public helpers (для nl_sql_generate) --------------------

    def lookup(self, term: str, *, max_matches: int = 5) -> list[dict[str, Any]]:
        """Синхронный lookup для in-process вызова из nl_sql_generate.

        Возвращает список ``{"terms": [...], "columns": [...]}`` (без
        JSON-обёртки). Используется ``NlSqlGenerateTool`` чтобы получить
        hints без обращения к function-calling.
        """
        entries, _ = self._load_entries()
        if not entries:
            return []
        return self._search(term, entries)[: max(1, max_matches)]

    # ---------- internals ------------------------------------------------

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        return {
            tok
            for tok in _TOKEN_SPLIT_RE.split((text or "").lower())
            if len(tok) >= 3
        }

    @staticmethod
    def _split_synonyms(key: str) -> list[str]:
        return [s.strip() for s in key.split("|") if s.strip()]

    def _all_entries_as_matches(
        self, entries: dict[str, list[str]]
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for key, cols in entries.items():
            out.append({
                "terms": self._split_synonyms(key),
                "columns": list(cols),
            })
        return out

    def _search(
        self, term: str, entries: dict[str, list[str]]
    ) -> list[dict[str, Any]]:
        q_tokens = self._tokenize(term)
        if not q_tokens:
            return []
        scored: list[tuple[int, dict[str, Any]]] = []
        for key, cols in entries.items():
            synonyms = self._split_synonyms(key)
            score = 0
            for syn in synonyms:
                syn_tokens = self._tokenize(syn)
                intersect = q_tokens & syn_tokens
                if intersect:
                    score += len(intersect)
                elif any(
                    qt in self._tokenize(syn) for qt in q_tokens
                ):
                    score += 1
            if score > 0:
                scored.append((score, {
                    "terms": synonyms,
                    "columns": list(cols),
                }))
        scored.sort(key=lambda x: (-x[0], x[1]["terms"][0] if x[1]["terms"] else ""))
        return [m for _, m in scored]

    def _load_entries(self) -> tuple[dict[str, list[str]] | None, str | None]:
        """Загрузить entries: data_file → inline → empty."""
        if self._entries_cache is not None:
            return self._entries_cache, None

        inline = getattr(self, "_entries_override", None)
        if isinstance(inline, dict) and inline:
            self._entries_cache = self._normalize_entries(inline)
            return self._entries_cache, None

        data_file = self.config.data_file
        if data_file:
            return self._load_from_file(data_file)

        return {}, None

    def invalidate_cache(self) -> None:
        self._entries_cache = None

    @staticmethod
    def _normalize_entries(raw: Any) -> dict[str, list[str]]:
        if not isinstance(raw, dict):
            return {}
        out: dict[str, list[str]] = {}
        for key, cols in raw.items():
            if not isinstance(key, str) or not key:
                continue
            if isinstance(cols, list):
                cleaned = [c for c in cols if isinstance(c, str) and c]
                if cleaned:
                    out[key] = cleaned
            elif isinstance(cols, str) and cols:
                out[key] = [cols]
        return out

    @classmethod
    def _load_from_file(
        cls, data_file: str
    ) -> tuple[dict[str, list[str]] | None, str | None]:
        path = Path(data_file)
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.is_file():
            return None, f"data_file не найден: {path}"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return None, f"ошибка чтения {path}: {exc}"
        return cls._normalize_entries(raw), None

    @staticmethod
    def _error(error_type: str, message: str) -> str:
        payload = {
            "status": "error",
            "error_type": error_type,
            "message": message,
        }
        return json.dumps(payload, ensure_ascii=False)
