"""Internal service: формирует описание схемы БД для LLM system prompt.

Не является tool'ом — это internal helper, вызываемый из ``NlSqlRunner`` и
других shared-компонентов, которым нужно текстовое представление схемы
для подмешивания в промпт. Кешируется на уровне процесса (TTL).

Архитектурное решение (см. план в docs/skill-tool-architecture.md):
tool'ы, которым нужна схема, обращаются к этому сервису напрямую (через
DI / in-process call), а не через отдельный tool ``schema_describe``.
Это дешевле по токенам и не плодит лишний шаг в pipeline агента.

Зависимости (только shared infra):
  * ``lib.services.table_registry.table_registry`` — whitelist таблиц.
  * ``lib.services.cache_provider_impl.CacheProvider.get_schema`` — DDL снимок.
  * ``lib.utils.sql_safety.format_schema`` — форматтер текста.
"""

from __future__ import annotations

import time
from typing import Any

from lib.services.table_registry import table_registry
from lib.utils.sql_safety import format_schema


__all__ = ["SchemaFormatter", "schema_formatter_singleton"]


class SchemaFormatter:
    """Internal service для формирования описания схемы БД в LLM-промпт.

    Использует ``TableRegistry`` как источник whitelist'а таблиц и
    ``CacheProvider.get_schema`` для снимка структуры. Результат
    кешируется на ``cache_ttl_sec`` (дефолт 60 секунд) — get_schema
    может быть дорогим (DuckDB describe), а описание редко меняется
    между вызовами в рамках одной сессии.

    Не знает про конкретные домены (audit/legal/etc.) — работает с любым
    набором таблиц, зарегистрированным в TableRegistry.
    """

    def __init__(self, *, cache_ttl_sec: int = 60) -> None:
        if cache_ttl_sec < 0:
            raise ValueError(
                f"cache_ttl_sec должен быть >= 0, получено: {cache_ttl_sec}"
            )
        self._cache_ttl_sec = cache_ttl_sec
        self._cache: dict[tuple[str, frozenset[str] | None, int], str] = {}
        self._cache_ts: dict[tuple[str, frozenset[str] | None, int], float] = {}

    def list_tables(self) -> list[str]:
        """Все зарегистрированные таблицы (skills + infra).

        Используется как whitelist для system prompt ``nl_sql_generate``.
        """
        return list(table_registry.table_names())

    def list_schema_names(self) -> list[str]:
        """Уникальные схемы среди зарегистрированных таблиц.

        Берётся префикс ``schema`` из ``schema.table``. Если ни одной
        таблицы не зарегистрировано — возвращает ``["main"]``.
        """
        names: set[str] = set()
        for t in self.list_tables():
            if "." in t:
                names.add(t.split(".", 1)[0])
        return sorted(names) if names else ["main"]

    def format_for_llm(
        self,
        *,
        schema_name: str | None = None,
        table_names: list[str] | None = None,
        max_chars: int = 12000,
    ) -> str:
        """Вернуть текстовое описание схемы для system prompt.

        Args:
            schema_name: имя схемы (например, ``"oarb"``). Если ``None`` —
                берётся первая зарегистрированная схема.
            table_names: whitelist таблиц (полные имена ``schema.table``).
                Если ``None`` — все таблицы из TableRegistry.
            max_chars: жёсткий лимит длины результата. Если описание длиннее —
                обрезается по границе строки с маркером ``[truncated]``.

        Returns:
            Многострочная строка в формате ``format_schema``. Пустая
            строка, если провайдер недоступен (нет кеша / БД).
        """
        if max_chars <= 0:
            raise ValueError(f"max_chars должен быть > 0, получено: {max_chars}")

        resolved_schema = schema_name or (self.list_schema_names() or ["main"])[0]
        resolved_tables = self._normalize_table_names(table_names)
        cache_key = (
            resolved_schema,
            frozenset(resolved_tables) if resolved_tables is not None else None,
            max_chars,
        )

        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        provider = self._open_cache_provider()
        if provider is None:
            return ""

        try:
            schema_dict = provider.get_schema(
                schema_name=resolved_schema,
                table_names=resolved_tables,
            )
        except Exception:
            return ""
        finally:
            try:
                provider.close()
            except Exception:
                pass

        text = format_schema(schema_dict)
        text = self._truncate(text, max_chars)
        self._store_cache(cache_key, text)
        return text

    def invalidate_cache(self) -> None:
        """Очистить весь кеш (например, после обновления DuckDB-снимка)."""
        self._cache.clear()
        self._cache_ts.clear()

    # ---------- helpers --------------------------------------------------

    @staticmethod
    def _normalize_table_names(
        table_names: list[str] | None,
    ) -> list[str] | None:
        """Привести список таблиц к ``[schema.table, ...]``.

        Принимает три формы:
          * ``["oarb.audits", "oarb.violations"]`` — полные имена (как есть);
          * ``[["oarb", "audits"]]`` — пары schema/table (склеиваем);
          * ``None`` — все таблицы (возвращаем ``None``).

        Возвращает ``None`` если на входе ``None`` или пустой список.
        """
        if not table_names:
            return None
        out: list[str] = []
        for entry in table_names:
            if isinstance(entry, (list, tuple)) and len(entry) == 2:
                schema, table = entry
                if schema and table:
                    out.append(f"{schema}.{table}")
            elif isinstance(entry, str) and entry:
                out.append(entry)
        return out or None

    @staticmethod
    def _truncate(text: str, max_chars: int) -> str:
        """Обрезать текст до ``max_chars`` по границе строки.

        Если обрезка сработала — добавляется маркер ``[truncated]``.
        """
        if len(text) <= max_chars:
            return text
        marker = "\n[truncated]"
        budget = max_chars - len(marker)
        if budget <= 0:
            return marker[:max_chars]
        cut = text[:budget]
        nl = cut.rfind("\n")
        if nl > budget * 0.7:
            cut = cut[:nl]
        return cut.rstrip() + marker

    def _get_cached(self, key: tuple) -> str | None:
        if self._cache_ttl_sec == 0:
            return None
        ts = self._cache_ts.get(key)
        if ts is None:
            return None
        if (time.monotonic() - ts) > self._cache_ttl_sec:
            self._cache.pop(key, None)
            self._cache_ts.pop(key, None)
            return None
        return self._cache.get(key)

    def _store_cache(self, key: tuple, value: str) -> None:
        if self._cache_ttl_sec == 0:
            return
        self._cache[key] = value
        self._cache_ts[key] = time.monotonic()

    @staticmethod
    def _open_cache_provider() -> Any:
        """Открыть ``CacheProvider`` из runtime-конфига.

        Чтение cache_path идёт из ``TableRegistry.snapshot_path`` —
        единый runtime-snapshot для всех skill'ов. Provider создаётся
        в read-only режиме: get_schema не требует записи.
        """
        from lib.services.cache_provider_impl import PostgresDuckDbProvider
        from lib.services.table_registry import table_registry
        from pathlib import Path

        workspace_root = Path.cwd()
        for candidate in (Path.cwd(), Path(__file__).resolve().parents[2]):
            if (candidate / "workspace").is_dir():
                workspace_root = candidate
                break

        cache_path = table_registry.snapshot_path(workspace_root)
        if not cache_path.is_file():
            return None

        try:
            return PostgresDuckDbProvider(
                schema=None,
                tables=None,
                additional_tables=[],
                cache_path=str(cache_path),
                vector_db_table="",
                vector_index_path="",
                vector_indexes={},
                vector_store_table="",
            )
        except Exception:
            return None


schema_formatter_singleton = SchemaFormatter()
"""Singleton для удобства вызова из разных мест (DI заменяется в тестах)."""
