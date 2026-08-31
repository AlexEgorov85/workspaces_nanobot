"""``ColumnDescriptionsResolver`` — generic resolver термин→колонка.

Используется tool'ом ``column_descriptions`` и ``NlSqlGenerateTool``
(in-process lookup hints). Не знает про конкретные домены
(``oarb.audit_analyzer`` или иные skill'ы) — это чистый механизм
tokenize → match → score, который читает словарь из конфигурации
(``ctx._settings_ref.tools.column_descriptions.entries`` или
``data_file``) и возвращает структурированный список matches.

Формат словаря (любой skill может подложить свой)::

    {
      "term synonym 1|term synonym 2|синоним": [
        "schema.table.column"
      ],
      ...
    }

Ключ — список синонимов через ``|``; совпадение с любым из них —
положительный сигнал. Поиск case-insensitive, токены ≥ 3 символов.

Используется:
  * ``workspace/tools/column_descriptions.py`` — tool-адаптер,
    читает конфиг и публикует matches через function calling;
  * ``workspace/tools/nl_sql_generate.py`` — in-process
    ``resolver.lookup(term)`` для подмешивания hints в system prompt.

Domain knowledge (какие именно термины → какие колонки) живёт **вне**
этого сервиса — в ``config.json::tools.column_descriptions.entries``
текущей инсталляции (или в ``data_file``). Resolver не знает,
какой skill его вызывает.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


__all__ = ["ColumnDescriptionsResolver"]


_TOKEN_SPLIT_RE = re.compile(r"[^a-zа-яё0-9]+")


class ColumnDescriptionsResolver:
    """Generic resolver термин→колонка (механизм lookup без домена).

    Аргумент ``entries_source`` — либо dict (inline-словарь), либо
    строка-путь к JSON-файлу (data_file), либо ``None`` (пустой
    словарь → lookup всегда возвращает ``[]``). Этот класс не
    интерпретирует домен: формат entries задаётся вызывающим, и
    resolver не делает предположений о значениях колонок.
    """

    def __init__(
        self,
        entries_source: dict[str, list[str]] | str | None = None,
        *,
        workspace_root: Path | None = None,
    ) -> None:
        self._entries_cache: dict[str, list[str]] | None = None
        self._load_error: str | None = None
        self._entries_source = entries_source
        self._workspace_root = workspace_root or Path.cwd()

    def invalidate_cache(self) -> None:
        self._entries_cache = None
        self._load_error = None

    @property
    def load_error(self) -> str | None:
        """Текст ошибки последней попытки чтения ``data_file`` (``None``
        если source — inline-dict или чтение прошло успешно).
        """
        return self._load_error

    def _load(self) -> dict[str, list[str]]:
        if self._entries_cache is not None:
            return self._entries_cache
        if isinstance(self._entries_source, dict):
            self._entries_cache = self._normalize(self._entries_source)
            return self._entries_cache
        if isinstance(self._entries_source, str) and self._entries_source:
            path = Path(self._entries_source)
            if not path.is_absolute():
                path = self._workspace_root / path
            if not path.is_file():
                self._load_error = f"data_file не найден: {path}"
                self._entries_cache = {}
                return self._entries_cache
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                self._load_error = f"ошибка чтения {path}: {exc}"
                self._entries_cache = {}
                return self._entries_cache
            self._entries_cache = self._normalize(raw)
            return self._entries_cache
        self._entries_cache = {}
        return self._entries_cache

    def prime(self) -> None:
        """Принудительно поднять кеш + диагностировать ошибку загрузки.

        Полезно для tool-адаптера: проверить ``load_error`` ДО первого
        ``lookup/all_entries``, чтобы корректно вернуть ``load_failed``,
        а не молчаливый ``success`` с пустым результатом.
        """
        self._load()

    @staticmethod
    def _normalize(raw: Any) -> dict[str, list[str]]:
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

    def lookup(
        self, term: str, *, max_matches: int = 5
    ) -> list[dict[str, Any]]:
        """Синхронный lookup ``{terms: [...], columns: [...]}``.

        Args:
            term: термин/фраза для поиска.
            max_matches: лимит числа возвращаемых matches (≥ 1).

        Returns:
            Пустой список, если нет entries или нет совпадений.
        """
        entries = self._load()
        if not entries:
            return []
        return self._search(term, entries)[: max(1, max_matches)]

    def all_entries(self, *, max_matches: int | None = None) -> list[dict[str, Any]]:
        """Вернуть все entries в формате ``{terms: [...], columns: [...]}``.

        Args:
            max_matches: опциональный лимит (≥ 1). ``None`` — без лимита.
        """
        entries = self._load()
        out = [
            {
                "terms": self._split_synonyms(key),
                "columns": list(cols),
            }
            for key, cols in entries.items()
        ]
        if max_matches is not None:
            out = out[: max(1, int(max_matches))]
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