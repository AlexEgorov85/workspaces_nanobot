"""Тесты ``run_predefined_script`` tool — read-only выполнение predefined SQL.

Проверяется, что tool:
  * находит скрипт через реестр по ``name``;
  * пробрасывает параметры в ``PredefinedScriptRequestBuilder``;
  * вызывает ``CacheProvider.query_sql`` (а не делает свой pool/IO);
  * валидирует SQL через ``validate_sql`` (DDL/INSERT отклоняются);
  * возвращает структурированный JSON с name/sql/params/rows;
  * обрабатывает отсутствие provider'а / скрипта.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from lib.services.table_registry import (
    SkillRegistration,
    TableResource,
    table_registry,
)
from workspace.tools.run_predefined_script import (
    RunPredefinedScriptTool,
    RunPredefinedScriptToolConfig,
)


@pytest.fixture(autouse=True)
def _reset_registry() -> None:
    table_registry.clear()
    yield
    table_registry.clear()


def _register_scripts_table() -> None:
    table_registry.register(SkillRegistration(
        name="demo",
        resources=(
            TableResource(name="public.audits"),
            TableResource(
                name="public.agent_predefined_scripts",
                label="scripts_registry",
            ),
        ),
    ))


class FakeProvider:
    """Fake CacheProvider: выдаёт скрипты из реестра + выполнё SQL."""

    def __init__(
        self,
        *,
        scripts: list[dict] | None = None,
        query_results: list[dict] | None = None,
        script_query_result: dict | None = None,
    ) -> None:
        self._scripts = list(scripts or [])
        self._query_results = list(query_results or [])
        self._script_query_result = script_query_result
        self.query_calls: list[tuple[str, list | None]] = []

    def query_sql(self, sql: str, params: list | None = None) -> dict:
        self.query_calls.append((sql, list(params) if params else None))
        if "FROM \"public\".\"agent_predefined_scripts\"" in sql:
            if self._script_query_result is not None:
                return self._script_query_result
            return {
                "status": "success",
                "row_count": len(self._scripts),
                "columns": [],
                "rows": list(self._scripts),
            }
        if self._query_results:
            return self._query_results.pop(0)
        return {"status": "error", "error": "no result configured"}


def _script_row(
    name: str = "audit_status_summary",
    description: str = "Сводка по статусам аудитов",
    sql: str = "SELECT status, COUNT(*) AS cnt FROM public.audits GROUP BY status",
    parameters: dict | None = None,
    max_rows: int = 100,
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "sql_template": sql,
        "parameters": json.dumps(parameters) if parameters is not None else "{}",
        "max_rows_default": max_rows,
        "returns": "status, cnt",
        "long_description": "",
    }


def _make_tool(provider: FakeProvider | None) -> RunPredefinedScriptTool:
    tool = RunPredefinedScriptTool(
        config=RunPredefinedScriptToolConfig(),
    )
    if provider is not None:
        tool.set_provider(provider)
    return tool


class TestRegistryLookup:
    async def test_script_not_found(self) -> None:
        _register_scripts_table()
        provider = FakeProvider(scripts=[])
        tool = _make_tool(provider)

        result = json.loads(await tool.execute(name="missing"))

        assert result["status"] == "error"
        assert result["error_type"] == "script_not_found"

    async def test_empty_name(self) -> None:
        _register_scripts_table()
        provider = FakeProvider(scripts=[])
        tool = _make_tool(provider)

        result = json.loads(await tool.execute(name=""))

        assert result["status"] == "error"
        assert result["error_type"] == "invalid_name"

    async def test_missing_infrastructure_no_provider(self) -> None:
        tool = _make_tool(provider=None)

        result = json.loads(await tool.execute(name="anything"))

        assert result["status"] == "error"
        assert result["error_type"] == "missing_infrastructure"

    async def test_no_label_registered(self) -> None:
        table_registry.register(SkillRegistration(
            name="demo",
            resources=(TableResource(name="public.audits"),),
        ))
        provider = FakeProvider()
        tool = _make_tool(provider)

        result = json.loads(await tool.execute(name="anything"))

        assert result["status"] == "error"
        assert result["error_type"] == "missing_infrastructure"


class TestExecution:
    async def test_zero_param_script_runs(self) -> None:
        _register_scripts_table()
        provider = FakeProvider(
            scripts=[_script_row()],
            query_results=[{
                "status": "success",
                "row_count": 3,
                "columns": ["status", "cnt"],
                "rows": [
                    {"status": "Завершена", "cnt": 10},
                    {"status": "В работе", "cnt": 5},
                    {"status": "Запланирована", "cnt": 2},
                ],
            }],
        )
        tool = _make_tool(provider)

        result = json.loads(await tool.execute(name="audit_status_summary"))

        assert result["status"] == "success"
        assert result["mode"] == "predefined_script"
        assert result["name"] == "audit_status_summary"
        assert result["sql"] == (
            "SELECT status, COUNT(*) AS cnt "
            "FROM public.audits GROUP BY status LIMIT ?"
        )
        assert result["params"] == [100]
        assert result["row_count"] == 3
        assert len(result["rows"]) == 3
        assert result["truncated"] is False

    async def test_params_passed_to_provider(self) -> None:
        _register_scripts_table()
        provider = FakeProvider(
            scripts=[_script_row(
                name="violations_by_period",
                sql=(
                    "SELECT COUNT(*) FROM public.audits "
                    "WHERE actual_date BETWEEN ? AND ?"
                ),
                parameters={
                    "date_from": {"type": "date", "required": True},
                    "date_to": {"type": "date", "required": True},
                },
            )],
            query_results=[{
                "status": "success",
                "row_count": 1,
                "columns": ["cnt"],
                "rows": [{"cnt": 42}],
            }],
        )
        tool = _make_tool(provider)

        result = json.loads(await tool.execute(
            name="violations_by_period",
            params={"date_from": "2024-01-01", "date_to": "2024-12-31"},
        ))

        assert result["status"] == "success"
        assert result["params"] == ["2024-01-01", "2024-12-31", 100]
        assert len(provider.query_calls) == 2
        # second call (1st — registry lookup)
        sql_call, params_call = provider.query_calls[1]
        assert "BETWEEN ? AND ? LIMIT ?" in sql_call
        assert params_call == ["2024-01-01", "2024-12-31", 100]

    async def test_execution_failure_reported(self) -> None:
        _register_scripts_table()
        provider = FakeProvider(
            scripts=[_script_row()],
            query_results=[{
                "status": "error",
                "error": "syntax error at end of input",
            }],
        )
        tool = _make_tool(provider)

        result = json.loads(await tool.execute(name="audit_status_summary"))

        assert result["status"] == "error"
        assert result["error_type"] == "execution_failed"
        assert "syntax error" in result["message"]


class TestParameterValidation:
    async def test_missing_required_param(self) -> None:
        _register_scripts_table()
        provider = FakeProvider(scripts=[_script_row(
            name="violations_by_period",
            sql="SELECT ? FROM public.audits WHERE x = ?",
            parameters={
                "date_from": {"type": "date", "required": True},
                "date_to": {"type": "date", "required": True},
            },
        )])
        tool = _make_tool(provider)

        result = json.loads(await tool.execute(
            name="violations_by_period",
            params={"date_from": "2024-01-01"},
        ))

        assert result["status"] == "error"
        assert result["error_type"] == "invalid_script"
        assert "date_to" in result["message"]

    async def test_unknown_param_rejected(self) -> None:
        _register_scripts_table()
        provider = FakeProvider(scripts=[_script_row()])
        tool = _make_tool(provider)

        result = json.loads(await tool.execute(
            name="audit_status_summary",
            params={"foo": "bar"},
        ))

        assert result["status"] == "error"
        assert "foo" in result["message"]

    async def test_invalid_param_type_rejected(self) -> None:
        _register_scripts_table()
        provider = FakeProvider(scripts=[_script_row(
            name="audits_by_year",
            sql="SELECT COUNT(*) FROM public.audits WHERE year = ?",
            parameters={"year": {"type": "integer", "required": True}},
        )])
        tool = _make_tool(provider)

        result = json.loads(await tool.execute(
            name="audits_by_year",
            params={"year": "not-an-integer"},
        ))

        assert result["status"] == "error"
        assert result["error_type"] == "invalid_script"
        assert "year" in result["message"]
        assert "integer" in result["message"]

    async def test_null_for_required_param_rejected(self) -> None:
        _register_scripts_table()
        provider = FakeProvider(scripts=[_script_row(
            name="audits_by_year",
            sql="SELECT COUNT(*) FROM public.audits WHERE year = ?",
            parameters={"year": {"type": "integer", "required": True}},
        )])
        tool = _make_tool(provider)

        result = json.loads(await tool.execute(
            name="audits_by_year",
            params={"year": None},
        ))

        assert result["status"] == "error"
        assert result["error_type"] == "invalid_script"
        assert "year" in result["message"]
        assert "обязателен" in result["message"]


class TestSqlSafetyGate:
    async def test_ddl_script_rejected(self) -> None:
        _register_scripts_table()
        provider = FakeProvider(scripts=[_script_row(
            sql="DROP TABLE public.audits",
        )])
        tool = _make_tool(provider)

        result = json.loads(await tool.execute(name="audit_status_summary"))

        assert result["status"] == "error"
        assert result["error_type"] == "invalid_script"
        assert "безопасность" in result["message"]

    async def test_placeholder_mismatch_rejected(self) -> None:
        _register_scripts_table()
        provider = FakeProvider(scripts=[_script_row(
            sql="SELECT ? FROM public.audits WHERE foo = ?",
            parameters={"a": {"type": "integer", "required": True}},
        )])
        tool = _make_tool(provider)

        result = json.loads(await tool.execute(
            name="audit_status_summary",
            params={"a": 1},
        ))

        assert result["status"] == "error"
        assert result["error_type"] == "invalid_script"
        assert "плейсхолдеров" in result["message"]


class TestMaxRows:
    async def test_override_smaller(self) -> None:
        _register_scripts_table()
        rows_data = [
            {"status": f"s{i}", "cnt": i} for i in range(20)
        ]
        provider = FakeProvider(
            scripts=[_script_row(max_rows=20)],
            query_results=[{
                "status": "success",
                "row_count": 20,
                "columns": ["status", "cnt"],
                "rows": rows_data,
            }],
        )
        tool = _make_tool(provider)

        result = json.loads(await tool.execute(
            name="audit_status_summary",
            max_rows=5,
        ))

        assert result["status"] == "success"
        assert result["returned_rows"] == 5
        assert result["truncated"] is True
        assert result["row_count"] == 20

    async def test_override_larger_clamped_to_config(self) -> None:
        _register_scripts_table()
        tool = RunPredefinedScriptTool(config=RunPredefinedScriptToolConfig(
            max_rows=10,
        ))
        provider = FakeProvider(
            scripts=[_script_row()],
            query_results=[{
                "status": "success",
                "row_count": 1,
                "columns": ["cnt"],
                "rows": [{"cnt": 5}],
            }],
        )
        tool.set_provider(provider)

        result = json.loads(await tool.execute(
            name="audit_status_summary",
            max_rows=999,
        ))

        assert result["status"] == "success"
        assert tool._effective_max_rows(999) == 10


class TestConfig:
    def test_config_defaults(self) -> None:
        cfg = RunPredefinedScriptToolConfig()
        assert cfg.enable is True
        assert cfg.max_rows == 1000
        assert cfg.max_result_chars == 50_000

    def test_tool_name(self) -> None:
        tool = _make_tool(provider=None)
        assert tool.name == "run_predefined_script"

    def test_tool_description_mentions_validation(self) -> None:
        tool = _make_tool(provider=None)
        desc = tool.description
        assert "predefined" in desc.lower()
        assert "name" in desc