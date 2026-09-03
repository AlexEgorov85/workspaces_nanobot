"""``run_predefined_script`` — tool для выполнения предопределённого SQL-скрипта.

Регистрируется автоматически через ``RuntimePatcher.patch_project_tools``
(см. ``lib/services/runtime_patcher.py``).

Назначение: безопасное выполнение SQL из реестра
``public.agent_predefined_scripts`` (DDL в
``sql/audit_analyzer/create_public_agent_predefined_scripts.sql``).
Использует уже существующий pipeline:

  * ``lib.services.predefined_script_registry.PredefinedScriptRegistry`` —
    lookup скрипта по ``name`` через ``TableRegistry.resources_by_label``.
  * ``lib.services.predefined_script_request.PredefinedScriptRequestBuilder``
    — валидация параметров + ``?``-placeholder'ы для DuckDB.
  * ``lib.utils.sql_safety.validate_sql`` — тот же SELECT-only gate, что
    для LLM-генерированного SQL (без исключений).
  * ``CacheProvider.query_sql`` — выполнение в общем DuckDB-кеше.

Tool **не** подключается к БД напрямую, **не** валидирует SQL сам,
**не** делает parameter substitution строкой. Только оркестрация
существующих сервисов.

Конфиг в ``project.json``::

    {
      "gateway": {
        "run_predefined_script": {
          "enable": true,
          "max_rows": 1000,
          "max_result_chars": 50000
        }
      }
    }

Observability покрывается штатным ``lib/hooks/tool_audit_hook.py``
(см. TARGET_ARCHITECTURE.md §26).
"""

from __future__ import annotations

import json
from typing import Any, ClassVar, Optional

from pydantic import BaseModel, Field

from lib.services.predefined_script_registry import PredefinedScriptRegistry
from lib.services.predefined_script_request import (
    PredefinedScriptRequestBuilder,
)
from lib.services.predefined_script_validator import (
    ParameterValidationError,
)
from lib.utils.text_utils import sanitize_value, truncate_middle
from nanobot.agent.tools.base import Tool, tool_parameters


__all__ = ["RunPredefinedScriptTool", "RunPredefinedScriptToolConfig"]


class RunPredefinedScriptToolConfig(BaseModel):
    """Конфиг секции ``gateway.run_predefined_script`` в ``project.json``."""

    enable: bool = True
    max_rows: int = Field(default=1000, ge=1, le=10_000)
    max_result_chars: int = Field(default=50_000, ge=1000, le=200_000)


@tool_parameters({
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": (
                "Имя предопределённого скрипта (PK в "
                "public.agent_predefined_scripts)."
            ),
        },
        "params": {
            "type": "object",
            "description": (
                "Параметры скрипта — словарь {param_name: value}. "
                "Валидируются по ``parameters`` JSONB из реестра "
                "(type/required/default/validation). Default-значения "
                "подставляются автоматически."
            ),
        },
        "max_rows": {
            "type": "integer",
            "minimum": 1,
            "maximum": 10000,
            "description": (
                "Переопределить лимит строк из реестра (``max_rows_default``). "
                "Не может превышать ``max_rows`` из конфигурации."
            ),
        },
    },
    "required": ["name"],
})
class RunPredefinedScriptTool(Tool):
    """Найти скрипт по имени, провалидировать параметры, выполнить SQL."""

    config_key: ClassVar[str] = "run_predefined_script"

    def __init__(self, *, config: RunPredefinedScriptToolConfig) -> None:
        self.config = config
        self._provider: Optional[Any] = None

    def set_provider(self, provider: Any) -> None:
        """DI для CacheProvider (production / integration tests)."""
        self._provider = provider

    @classmethod
    def config_cls(cls):
        return RunPredefinedScriptToolConfig

    @classmethod
    def _read_settings_section(cls, ctx: Any) -> dict[str, Any]:
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
        for field in ("enable", "max_rows", "max_result_chars"):
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
        return "run_predefined_script"

    @property
    def description(self) -> str:
        return (
            "Выполнить предопределённый SQL-скрипт из реестра "
            "public.agent_predefined_scripts по имени. Параметры "
            "скрипта передаются в ``params`` и валидируются по схеме "
            "(type/required/default/validation) из реестра. "
            "Возвращает {status, name, sql, params, columns, rows, "
            "row_count}. SELECT-only — через тот же ``validate_sql``, "
            "что и ``nl_sql_generate``/``duckdb_query``. "
            "Аргументы: name (обяз.), params (опц.), max_rows (опц.)."
        )

    async def execute(
        self,
        *,
        name: str,
        params: Optional[dict[str, Any]] = None,
        max_rows: Optional[int] = None,
        **_kwargs: Any,
    ) -> str:
        if not name or not name.strip():
            return self._error("invalid_name", "Параметр name обязателен.")

        provider = self._get_provider()
        if provider is None:
            return self._missing_infrastructure_error()

        try:
            registry = PredefinedScriptRegistry(provider=provider)
            script = registry.get_by_name(name)
        except RuntimeError as exc:
            return self._error("missing_infrastructure", str(exc))

        if script is None:
            return self._error(
                "script_not_found",
                f"Скрипт {name!r} не найден в реестре.",
            )

        try:
            builder = PredefinedScriptRequestBuilder(
                script=script,
                max_rows=self._effective_max_rows(max_rows),
            )
            request = builder.build(params)
        except (ValueError, ParameterValidationError) as exc:
            return self._error("invalid_script", str(exc))

        result = provider.query_sql(request.sql, list(request.params))
        if result.get("status") != "success":
            return self._error(
                "execution_failed",
                result.get("error", "query failed"),
                sql=request.sql,
            )

        return self._format_response(request, result)

    # ---------- helpers -------------------------------------------------

    def _get_provider(self) -> Any:
        if self._provider is not None:
            return self._provider
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

    def _effective_max_rows(self, override: int | None) -> int | None:
        candidates = [v for v in (override, self.config.max_rows) if v and v > 0]
        if not candidates:
            return None
        return min(candidates)

    def _format_response(
        self,
        request: Any,
        result: dict[str, Any],
    ) -> str:
        columns = list(result.get("columns") or [])
        raw_rows = list(result.get("rows") or [])
        row_count = int(result.get("row_count") or len(raw_rows))
        max_rows = request.max_rows or self.config.max_rows
        if max_rows and len(raw_rows) > max_rows:
            rows = raw_rows[:max_rows]
            truncated = True
        else:
            rows = raw_rows
            truncated = False

        sanitized_rows = [
            {col: sanitize_value(r.get(col) if isinstance(r, dict) else r[idx])
             for idx, col in enumerate(columns)}
            for r in rows
        ] if columns else [[sanitize_value(v) for v in r] for r in rows]

        payload: dict[str, Any] = {
            "status": "success",
            "mode": "predefined_script",
            "name": request.name,
            "sql": request.sql,
            "params": list(request.params),
            "columns": columns,
            "rows": sanitized_rows,
            "row_count": row_count,
            "returned_rows": len(sanitized_rows),
            "truncated": truncated,
        }
        text = json.dumps(payload, ensure_ascii=False, default=str)
        if len(text) > self.config.max_result_chars:
            shrunk = self._shrink_rows_to_fit(
                sanitized_rows, columns, self.config.max_result_chars,
            )
            payload["rows"] = shrunk
            payload["returned_rows"] = len(shrunk)
            payload["truncated"] = True
            text = json.dumps(payload, ensure_ascii=False, default=str)
            if len(text) > self.config.max_result_chars:
                text = truncate_middle(text, self.config.max_result_chars)
        return text

    @staticmethod
    def _shrink_rows_to_fit(
        rows: list[Any],
        columns: list[str],
        max_chars: int,
    ) -> list[Any]:
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

    def _missing_infrastructure_error(self) -> str:
        return self._error(
            "missing_infrastructure",
            "run_predefined_script tool не подключён к DuckDB-кешу. "
            "Убедитесь, что TableRegistry заполнен (ApplicationContext "
            "поднят) и DuckDB-снимок существует "
            "(workspace/data_store/duckdb/cache.duckdb).",
        )

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