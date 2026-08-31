"""``nl_sql_generate`` — tool для генерации и выполнения SELECT по NL-запросу.

Регистрируется автоматически через ``RuntimePatcher.patch_project_tools``
(см. ``lib/services/runtime_patcher.py``).

Назначение: заменяет режим ``generated_sql`` навыка ``audit_analyzer``
(и других skill'ов с NL→SELECT) в виде generic tool. Использует:
  * ``lib/services/nl_sql_runner.py::NlSqlRunner`` — общий pipeline
    (whitelist + LLM retry + execute);
  * ``lib/services/schema_formatter.py::SchemaFormatter`` — internal
    service для описания схемы (НЕ tool);
  * ``workspace/tools/column_descriptions.py::ColumnDescriptionsTool.lookup``
    — синхронный in-process lookup подсказок по термину запроса;
  * ``lib/services/cache_provider_impl.CacheProvider`` — выполнение SQL.

Конфиг в ``project.json``::

    {
      "gateway": {
        "nl_sql_generate": {
          "enable": true,
          "max_retries": 3,
          "schema_max_chars": 12000,
          "few_shot_top_n": 2,
          "max_result_chars": 50000,
          "max_rows": 1000,
          "hints_max_matches": 5
        }
      }
    }

Контракт и поведение описаны в ``docs/skill-tool-architecture.md`` §8.1.

Domain-free: tool не знает про конкретные таблицы (``oarb.*``) и индексы.
Whitelist приходит из ``TableRegistry``, few-shot — из ресурсов с
``label='scripts_registry'``.

Observability покрывается штатным ``lib/hooks/tool_audit_hook.py``
(см. TARGET_ARCHITECTURE.md §26) — этот tool не дублирует логирование.
"""

from __future__ import annotations

import json
import re
from typing import Any, ClassVar, Optional

from pydantic import BaseModel, Field

from lib.services.nl_sql_runner import NlSqlRunner, NlSqlRunnerConfig
from lib.services.schema_formatter import SchemaFormatter
from lib.utils.text_utils import sanitize_value, truncate_middle
from nanobot.agent.tools.base import Tool, tool_parameters
from workspace.tools.column_descriptions import ColumnDescriptionsTool


__all__ = ["NlSqlGenerateTool", "NlSqlGenerateToolConfig"]


_TOKEN_SPLIT_RE = re.compile(r"[^a-zа-яё0-9]+")


class NlSqlGenerateToolConfig(BaseModel):
    """Конфиг секции ``gateway.nl_sql_generate`` в ``project.json``."""

    enable: bool = True
    max_retries: int = Field(default=3, ge=0, le=10)
    schema_max_chars: int = Field(default=12_000, ge=1000, le=100_000)
    few_shot_top_n: int = Field(default=2, ge=0, le=10)
    max_result_chars: int = Field(default=50_000, ge=1000, le=200_000)
    max_rows: int = Field(default=1000, ge=1, le=10_000)
    hints_max_matches: int = Field(default=5, ge=0, le=50)


@tool_parameters({
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "Запрос на естественном языке. Tool генерирует SELECT по "
                "whitelist'у таблиц из TableRegistry, валидирует через "
                "EXPLAIN и выполняет в локальном DuckDB-кеше."
            ),
        },
        "max_rows": {
            "type": "integer",
            "minimum": 1,
            "maximum": 10000,
            "description": (
                "Локальный лимит строк; не может превышать "
                "``max_rows`` из конфигурации."
            ),
        },
        "no_few_shot": {
            "type": "boolean",
            "default": False,
            "description": "Не подмешивать few-shot примеры из реестра.",
        },
        "skip_hints": {
            "type": "boolean",
            "default": False,
            "description": (
                "Не вызывать column_descriptions для подбора подсказок "
                "по термину запроса."
            ),
        },
        "hints_max_matches": {
            "type": "integer",
            "minimum": 0,
            "maximum": 50,
            "description": "Сколько подсказок подмешать в system prompt.",
        },
        "context": {
            "type": "array",
            "items": {"type": "object"},
            "description": (
                "Опциональная история чата (для LLM-контекста)."
            ),
        },
    },
    "required": ["query"],
})
class NlSqlGenerateTool(Tool):
    """Сгенерировать SELECT по NL-запросу, провалидировать и выполнить."""

    config_key: ClassVar[str] = "nl_sql_generate"

    def __init__(self, *, config: NlSqlGenerateToolConfig) -> None:
        self.config = config
        self._provider: Optional[Any] = None
        self._schema_formatter: SchemaFormatter | None = None
        self._column_descriptions: ColumnDescriptionsTool | None = None

    def set_provider(self, provider: Any) -> None:
        """DI для CacheProvider (production / integration tests)."""
        self._provider = provider

    def set_schema_formatter(self, formatter: SchemaFormatter) -> None:
        """DI для SchemaFormatter (unit-тесты)."""
        self._schema_formatter = formatter

    def set_column_descriptions(self, tool: ColumnDescriptionsTool) -> None:
        """DI для ColumnDescriptionsTool (unit-тесты).

        Без DI tool создаёт свой экземпляр с дефолтным конфигом —
        этого достаточно, если column_descriptions настроен в
        ``tools.column_descriptions`` (``data_file`` или inline entries).
        """
        self._column_descriptions = tool

    @classmethod
    def config_cls(cls):
        return NlSqlGenerateToolConfig

    @classmethod
    def _read_settings_section(cls, ctx: Any) -> dict[str, Any]:
        """Прочитать ``gateway.nl_sql_generate`` из ``ctx._settings_ref``."""
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
        out: dict[str, Any] = {}
        for field in (
            "enable", "max_retries", "schema_max_chars",
            "few_shot_top_n", "max_result_chars", "max_rows",
            "hints_max_matches",
        ):
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
        config_cls = cls.config_cls()
        try:
            config = config_cls(**section)
        except Exception:
            config = config_cls()
        return cls(config=config)

    @property
    def name(self) -> str:
        return "nl_sql_generate"

    @property
    def description(self) -> str:
        return (
            "Преобразует запрос на естественном языке в SELECT по "
            "whitelist'у зарегистрированных таблиц, проверяет синтаксис "
            "через EXPLAIN и выполняет в локальном DuckDB-кеше. "
            "Возвращает {status, sql, columns, rows, row_count}. "
            "SELECT-only, retry до max_retries при ошибке валидации. "
            "Аргументы: query (обяз.), max_rows (опц.), no_few_shot "
            "(опц.), skip_hints (опц.), hints_max_matches (опц.)."
        )

    async def execute(
        self,
        *,
        query: str,
        max_rows: Optional[int] = None,
        no_few_shot: bool = False,
        skip_hints: bool = False,
        hints_max_matches: Optional[int] = None,
        context: Optional[list[dict]] = None,
        **_kwargs: Any,
    ) -> str:
        if not query or not query.strip():
            return self._error("invalid_query", "Параметр query обязателен.")

        effective_max_rows = min(
            max_rows if max_rows else self.config.max_rows,
            self.config.max_rows,
        )

        provider = self._get_provider()
        if provider is None:
            return self._available_tables_error()

        hints_block = ""
        if not skip_hints:
            hints_block = self._build_hints_block(
                query,
                max_matches=hints_max_matches or self.config.hints_max_matches,
            )

        runner = NlSqlRunner(
            provider=provider,
            schema_formatter=self._schema_formatter or SchemaFormatter(),
            config=NlSqlRunnerConfig(
                max_retries=self.config.max_retries,
                schema_max_chars=self.config.schema_max_chars,
                few_shot_top_n=self.config.few_shot_top_n,
            ),
        )

        result = runner.run(
            query,
            context=context,
            no_few_shot=no_few_shot,
            hints_block=hints_block,
        )

        return self._format_tool_response(result, effective_max_rows)

    # ---------- helpers -------------------------------------------------

    def _get_provider(self) -> Any:
        if self._provider is not None:
            return self._provider
        # Fallback: создаём provider из runtime (как в duckdb_query_tool).
        try:
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

    def _build_hints_block(self, query: str, *, max_matches: int) -> str:
        cd = self._column_descriptions or self._default_column_descriptions()
        if cd is None or max_matches <= 0:
            return ""
        matches = cd.lookup(query, max_matches=max_matches)
        if not matches:
            return ""
        lines = ["", "  COLUMN HINTS (use these exact column names where applicable):"]
        for i, m in enumerate(matches, start=4):
            terms = m.get("terms") or []
            cols = m.get("columns") or []
            if not cols:
                continue
            terms_str = " / ".join(f"«{t}»" for t in terms if t)
            cols_str = ", ".join(f"`{c}`" for c in cols)
            lines.append(f"  {i}. {terms_str} = {cols_str}.")
        block = "\n".join(lines).rstrip()
        return block if block.count("\n") >= 2 else ""

    def _default_column_descriptions(self) -> ColumnDescriptionsTool | None:
        try:
            cd = ColumnDescriptionsTool.create(object())
            return cd
        except Exception:
            return None

    def _available_tables_error(self) -> str:
        available = self._collect_available_tables()
        message = (
            "nl_sql_generate tool не подключён к DuckDB-кешу. "
            "Убедитесь, что TableRegistry заполнен (ApplicationContext "
            "поднят) и DuckDB-снимок существует "
            "(workspace/data_store/duckdb/cache.duckdb)."
        )
        if available:
            message += f" Доступные таблицы: {available}."
        else:
            message += (
                " (нет таблиц в TableRegistry — проверьте project.json::skills.* "
                "и gateway.vector.index.storage_table)"
            )
        return self._error("missing_infrastructure", message)

    @staticmethod
    def _collect_available_tables() -> str:
        try:
            from lib.services.table_registry import table_registry

            names = list(table_registry.table_names()) + list(
                table_registry.vector_names()
            )
            return ", ".join(names) if names else ""
        except Exception:
            return ""

    def _format_tool_response(
        self,
        result: dict[str, Any],
        effective_max_rows: int,
    ) -> str:
        """Привести результат runner'а к контракту tool'а + truncate."""
        status = result.get("status", "error")
        data = result.get("data") or {}

        if status != "success":
            err_msg = data.get("message", "Unknown error")
            sql = data.get("sql", "")
            error_type = self._classify_error(err_msg)
            return self._error(error_type, err_msg, sql=sql)

        sql = data.get("sql", "")
        inner = data.get("result") or {}
        columns = inner.get("columns") or []
        raw_rows = inner.get("rows") or []
        row_count = inner.get("row_count", len(raw_rows))

        if effective_max_rows and len(raw_rows) > effective_max_rows:
            rows = raw_rows[:effective_max_rows]
            truncated = True
        else:
            rows = raw_rows
            truncated = False

        sanitized_rows = [
            [sanitize_value(v) for v in row] for row in rows
        ]

        payload = {
            "status": "success",
            "sql": sql,
            "columns": list(columns),
            "rows": sanitized_rows,
            "row_count": row_count,
            "returned_rows": len(sanitized_rows),
            "truncated": truncated,
        }
        text = json.dumps(payload, ensure_ascii=False, default=str)
        if len(text) > self.config.max_result_chars:
            truncated_rows = self._shrink_rows_to_fit(
                sanitized_rows, list(columns), self.config.max_result_chars
            )
            payload["rows"] = truncated_rows
            payload["returned_rows"] = len(truncated_rows)
            payload["truncated"] = True
            text = json.dumps(payload, ensure_ascii=False, default=str)
            if len(text) > self.config.max_result_chars:
                text = truncate_middle(text, self.config.max_result_chars)
        return text

    @staticmethod
    def _shrink_rows_to_fit(
        rows: list[list[Any]],
        columns: list[str],
        max_chars: int,
    ) -> list[list[Any]]:
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

    @staticmethod
    def _classify_error(message: str) -> str:
        msg = (message or "").lower()
        if (
            "tableregistry" in msg
            or "table registry" in msg
            or "table_registry" in msg
        ):
            return "missing_infrastructure"
        if "не удалось сгенерировать" in msg or "llm" in msg:
            return "generation_failed"
        if "syntax" in msg or "explain" in msg:
            return "explain_failed"
        return "sql_error"

    @staticmethod
    def _error(
        error_type: str,
        message: str,
        *,
        sql: str | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "status": "error",
            "error_type": error_type,
            "message": message,
        }
        if sql:
            payload["sql"] = sql
        return json.dumps(payload, ensure_ascii=False)
