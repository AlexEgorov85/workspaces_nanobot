"""``duckdb_query`` — generic read-only SQL tool.

Регистрируется автоматически через ``RuntimePatcher.patch_project_tools``
(см. ``lib/services/runtime_patcher.py``).

Контракт и поведение описаны в ``docs/skill-tool-architecture.md`` §6.

Конфиг читается из секции ``gateway.duckdb_query.*`` в ``project.json``::

    {
      "gateway": {
        "duckdb_query": {
          "enable": true,
          "max_rows": 1000,
          "max_result_chars": 50000,
          "query_timeout_sec": 30,
          "schema_name": "oarb"
        }
      }
    }

Инфраструктура выполнения запросов:
  * ``lib/utils/sql_safety.py::validate_sql`` — последняя граница
    безопасности (SELECT-only, один statement).
  * ``lib/utils/text_utils.py::sanitize_value`` — JSON-сериализация
    результата (datetime / Decimal / NaN / bytes).
  * ``lib/utils/text_utils.py::truncate_middle`` — обрезка ответа
    под ``max_result_chars``.

Observability покрывается штатным ``lib/hooks/tool_audit_hook.py``
(см. TARGET_ARCHITECTURE.md §26) — этот tool не дублирует логирование.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar, Optional

from pydantic import BaseModel, Field

from lib.utils.sql_safety import validate_sql
from lib.utils.text_utils import sanitize_value, truncate_middle
from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters


class DuckdbQueryToolConfig(BaseModel):
    """Конфиг секции ``gateway.duckdb_query`` в ``project.json``."""

    enable: bool = True
    max_rows: int = Field(default=1000, ge=1, le=10000)
    max_result_chars: int = Field(default=50_000, ge=100, le=200_000)
    query_timeout_sec: int = Field(default=30, ge=1, le=300)
    schema_name: str = "oarb"


@tool_parameters({
    "type": "object",
    "properties": {
        "sql": {
            "type": "string",
            "description": (
                "SQL-запрос. Допускаются только SELECT/WITH/EXPLAIN; "
                "DDL/DML запрещены, multi-statement запрещён. "
                "Параметры привязываются позиционно через ``?`` или именованно через ``:name``."
            ),
        },
        "params": {
            "type": "object",
            "description": (
                "Опциональные параметры запроса. Значения передаются в "
                "DuckDB после JSON-сериализации; вложенные структуры допустимы."
            ),
            "additionalProperties": True,
        },
        "max_rows": {
            "type": "integer",
            "description": (
                "Ограничение числа возвращаемых строк; не может превышать "
                "``max_rows`` из конфигурации. По умолчанию — конфиг."
            ),
            "minimum": 1,
        },
    },
    "required": ["sql"],
})
class DuckdbQueryTool(Tool):
    """Выполнить read-only SQL-запрос в локальном DuckDB-кеше."""

    config_key: ClassVar[str] = "duckdb_query"

    def __init__(self, *, config: DuckdbQueryToolConfig) -> None:
        self.config = config
        self._connection_factory: Optional[Any] = None
        self._cache_store: Optional[Any] = None

    def set_provider(self, cache_store: Any) -> None:
        """Подключить реальный ``DuckDbCacheStore`` (production DI).

        Через него запрос исполняется в общем DuckDB-кеше
        (``gateway.vector.index.storage_table``), а не в дефолтном
        ``:memory:``-fallback'е.
        """
        self._cache_store = cache_store

    def set_connection_factory(self, factory: Any) -> None:
        """Установить фабрику DuckDB-коннектов (для DI в тестах)."""
        self._connection_factory = factory

    @classmethod
    def config_cls(cls):
        return DuckdbQueryToolConfig

    @classmethod
    def _read_settings_section(cls, ctx: Any) -> dict[str, Any]:
        """Прочитать ``gateway.duckdb_query`` из ``ctx._settings_ref``."""
        settings = getattr(ctx, "_settings_ref", None)
        if settings is None:
            return {}
        try:
            gateway = settings.gateway
        except AttributeError:
            return {}
        if gateway is None:
            return {}
        try:
            section = getattr(gateway, cls.config_key)
        except AttributeError:
            return {}
        if section is None:
            return {}
        if isinstance(section, dict):
            return dict(section)
        # pydantic-модель или SimpleNamespace: читаем по атрибутам
        out: dict[str, Any] = {}
        for field in ("enable", "max_rows", "max_result_chars",
                      "query_timeout_sec", "schema_name"):
            if hasattr(section, field):
                out[field] = getattr(section, field)
        if not out:
            try:
                out = dict(vars(section))
            except Exception:
                pass
        return out

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
            config = cls.config_cls()()
        return cls(config=config)

    @property
    def name(self) -> str:
        return "duckdb_query"

    @property
    def description(self) -> str:
        return (
            "Execute a read-only SQL query against the configured DuckDB "
            "database. Returns a structured JSON payload with columns, "
            "rows, and row_count. Rejects DDL/DML and multi-statement "
            "queries. Use for structured analysis and aggregation."
        )

    async def execute(
        self,
        *,
        sql: str,
        params: Optional[dict[str, Any]] = None,
        max_rows: Optional[int] = None,
        **_kwargs: Any,
    ) -> str:
        """Выполнить SELECT и вернуть JSON-сериализованный ответ.

        Args:
            sql: SQL-запрос (SELECT/WITH/EXPLAIN; иначе — отказ).
            params: Параметры запроса (опционально).
            max_rows: Локальное ограничение числа строк (опционально).

        Returns:
            JSON-строка со статусом и результатом (см. ``docs/skill-tool-architecture.md`` §6).
        """
        err = validate_sql(sql)
        if err is not None:
            return self._error("sql_error", err)

        effective_max_rows = max_rows or self.config.max_rows
        if effective_max_rows > self.config.max_rows:
            return self._error(
                "sql_error",
                f"max_rows={effective_max_rows} exceeds configured limit "
                f"{self.config.max_rows}",
            )

        cache_store = getattr(self, "_cache_store", None)
        if cache_store is not None:
            result = cache_store.execute_readonly(sql, params, effective_max_rows)
            error = result.get("error")
            if error:
                return self._error("sql_error", error)
            rows = result.get("rows") or []
            columns = result.get("columns") or []
        elif getattr(self, "_connection_factory", None) is not None:
            conn = self._open_duckdb_connection()
            try:
                rows, columns = self._run_with_timeout(
                    conn, sql, params, effective_max_rows
                )
            except Exception as exc:
                return self._error("sql_error", str(exc))
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        else:
            return self._error(
                "missing_infrastructure",
                "duckdb_query tool is not wired to a DuckDB cache_store; "
                "configure gateway.vector.index.storage_table and ensure the "
                "DuckDB cache is available",
            )

        sanitized_rows = [
            [sanitize_value(v) for v in row] for row in rows
        ]
        truncated_rows: list[list[Any]] = list(sanitized_rows)
        truncated = False
        text = json.dumps(
            {
                "status": "success",
                "columns": list(columns),
                "rows": truncated_rows,
                "row_count": len(truncated_rows),
                "returned_rows": len(truncated_rows),
                "truncated": False,
            },
            ensure_ascii=False,
            default=str,
        )
        if len(text) > self.config.max_result_chars:
            truncated = True
            truncated_rows = self._shrink_rows_to_fit(
                truncated_rows, list(columns), self.config.max_result_chars
            )
            text = json.dumps(
                {
                    "status": "success",
                    "columns": list(columns),
                    "rows": truncated_rows,
                    "row_count": len(sanitized_rows),
                    "returned_rows": len(truncated_rows),
                    "truncated": True,
                },
                ensure_ascii=False,
                default=str,
            )
            if len(text) > self.config.max_result_chars:
                text = truncate_middle(text, self.config.max_result_chars)
        return text

    def _shrink_rows_to_fit(
        self,
        rows: list[list[Any]],
        columns: list[str],
        max_chars: int,
    ) -> list[list[Any]]:
        """Бинарным поиском уменьшить число строк, пока JSON влезает в лимит."""
        if not rows:
            return rows
        lo, hi = 0, len(rows)
        best = 0
        while lo <= hi:
            mid = (lo + hi) // 2
            text = json.dumps(
                {
                    "status": "success",
                    "columns": columns,
                    "rows": rows[:mid],
                    "row_count": len(rows),
                    "returned_rows": mid,
                    "truncated": True,
                },
                ensure_ascii=False,
                default=str,
            )
            if len(text) <= max_chars:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return rows[:best]

    # ---------- helpers -------------------------------------------------

    def _open_duckdb_connection(self) -> Any:
        """Открыть in-memory DuckDB-коннект.

        Реальный продакшн-путь подменяется DI/integration-тестами через
        ``set_connection_factory``. По умолчанию используется
        ``duckdb.connect(":memory:")``.
        """
        factory = getattr(self, "_connection_factory", None) or _DEFAULT_DUCKDB_FACTORY
        return factory()

    def _run_with_timeout(
        self,
        conn: Any,
        sql: str,
        params: Optional[dict[str, Any]],
        max_rows: int,
    ) -> tuple[list[tuple[Any, ...]], list[str]]:
        """Запустить SQL с учётом ``query_timeout_sec``.

        DuckDB не предоставляет нативного per-statement timeout,
        поэтому используется простая защита через pragma + ограничение
        размера результата. Точная семантика — best-effort.
        """
        cur = conn.cursor()
        try:
            try:
                cur.execute(f"PRAGMA threads=1; SET statement_timeout={self.config.query_timeout_sec * 1000}")
            except Exception:
                pass
            if params:
                cur.execute(sql, list(params.values()))
            else:
                cur.execute(sql)
            if cur.description is None:
                return [], []
            columns = [c[0] for c in cur.description]
            fetched: list[tuple[Any, ...]] = []
            for row in cur.fetchmany(max_rows):
                fetched.append(row)
            return fetched, columns
        finally:
            try:
                cur.close()
            except Exception:
                pass

    def _error(self, error_type: str, message: str) -> str:
        payload = {
            "status": "error",
            "error_type": error_type,
            "message": message,
        }
        return json.dumps(payload, ensure_ascii=False)


# -----------------------------------------------------------------------
# DI-механизм для тестов: ``DuckdbQueryTool.set_connection_factory``
# -----------------------------------------------------------------------

class _QueryTimeoutError(Exception):
    pass


def _default_duckdb_connect() -> Any:
    import duckdb
    return duckdb.connect(":memory:")


_DEFAULT_DUCKDB_FACTORY = _default_duckdb_connect