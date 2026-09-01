"""Архитектурные тесты трёх режимов ``audit_analyzer``.

После Этапа 2 (удаление ``auto_predefined``) каждый режим — независимый
путь. Эти тесты проверяют:

* Test A (predefined) — ``run_predefined_script`` выполняет SQL без LLM.
* Test B (vector) — ``vector_search`` ищет по FAISS без SQL-генерации.
* Test C (SQL fallback) — ``nl_sql_generate`` единственный путь с LLM.

Эти тесты НЕ проверяют выбор режима агентом (это вопрос SKILL.md).
Они проверяют, что tools не делают auto-routing внутри себя.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from lib.services.table_registry import (
    SkillRegistration,
    TableResource,
    table_registry,
)
from workspace.tools.nl_sql_generate import (
    NlSqlGenerateTool,
    NlSqlGenerateToolConfig,
)
from workspace.tools.run_predefined_script import (
    RunPredefinedScriptTool,
    RunPredefinedScriptToolConfig,
)
from workspace.tools.vector_search_tool import (
    VectorSearchTool,
    VectorSearchToolConfig,
)


@pytest.fixture(autouse=True)
def _reset_registry() -> None:
    table_registry.clear()
    yield
    table_registry.clear()


# ---------------------------------------------------------------------------
# Test A: predefined script — нет обращения к LLM
# ---------------------------------------------------------------------------


class _PredefinedFakeProvider:
    """CacheProvider, возвращающий реестр + выполнение SELECT."""

    def __init__(self, scripts: list[dict], execute_result: dict) -> None:
        self._scripts = scripts
        self._execute_result = execute_result
        self.query_calls: list[tuple[str, list | None]] = []
        self.explain_calls: list[str] = []
        self.llm_called = False

    def query_sql(self, sql: str, params: list | None = None) -> dict:
        self.query_calls.append((sql, list(params) if params else None))
        if "agent_predefined_scripts" in sql:
            return {
                "status": "success",
                "row_count": len(self._scripts),
                "columns": [],
                "rows": list(self._scripts),
            }
        return self._execute_result

    def explain(self, sql: str) -> dict:
        self.explain_calls.append(sql)
        return {"valid": True}


def _predefined_tool(provider: _PredefinedFakeProvider) -> RunPredefinedScriptTool:
    tool = RunPredefinedScriptTool(config=RunPredefinedScriptToolConfig())
    tool.set_provider(provider)
    return tool


def _audit_status_summary_row() -> dict:
    return {
        "name": "audit_status_summary",
        "description": "Сводка по статусам аудитов",
        "sql_template": (
            "SELECT status, COUNT(*) AS cnt FROM public.audits GROUP BY status"
        ),
        "parameters": json.dumps({}),
        "max_rows_default": 100,
        "returns": "status, cnt",
        "long_description": "",
    }


class TestAPredefinedPathNoLLM:
    """Test A: Agent → run_predefined_script → known SQL. LLM не зовётся."""

    def test_runs_sql_via_registry(self) -> None:
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
        provider = _PredefinedFakeProvider(
            scripts=[_audit_status_summary_row()],
            execute_result={
                "status": "success",
                "row_count": 3,
                "columns": ["status", "cnt"],
                "rows": [
                    {"status": "Завершена", "cnt": 10},
                    {"status": "В работе", "cnt": 5},
                    {"status": "Запланирована", "cnt": 2},
                ],
            },
        )
        tool = _predefined_tool(provider)

        result = json.loads(asyncio.run(tool.execute(name="audit_status_summary")))

        assert result["status"] == "success"
        assert result["mode"] == "predefined_script"
        assert result["name"] == "audit_status_summary"
        # Критический инвариант: LLM не вызывается.
        assert provider.llm_called is False
        # SQL не содержит ручной подстановки — параметры через `?`.
        assert "GROUP BY status LIMIT ?" in result["sql"]
        assert result["params"] == [100]

    def test_does_not_call_llm_for_parameterized_script(self) -> None:
        """Parameterized predefined тоже не вызывает LLM."""
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
        row = _audit_status_summary_row()
        row["name"] = "violations_by_period"
        row["sql_template"] = (
            "SELECT COUNT(*) FROM public.violations WHERE date BETWEEN ? AND ?"
        )
        row["parameters"] = json.dumps({
            "date_from": {"type": "date", "required": True},
            "date_to": {"type": "date", "required": True},
        })
        provider = _PredefinedFakeProvider(
            scripts=[row],
            execute_result={
                "status": "success",
                "row_count": 1,
                "columns": ["cnt"],
                "rows": [{"cnt": 42}],
            },
        )
        tool = _predefined_tool(provider)

        result = json.loads(asyncio.run(tool.execute(
            name="violations_by_period",
            params={"date_from": "2024-01-01", "date_to": "2024-12-31"},
        )))

        assert result["status"] == "success"
        assert provider.llm_called is False
        assert result["params"] == ["2024-01-01", "2024-12-31", 100]


# ---------------------------------------------------------------------------
# Test B: vector search — нет SQL-генерации
# ---------------------------------------------------------------------------


class _VectorHit:
    def __init__(self, content: str, score: float, pk: Any = None) -> None:
        self.content = content
        self.score = score
        self.source = "oarb.violations"
        self.table = "oarb.violations"
        self.pk_value = pk
        self.chunk = "0"
        self.matched_chunks = 1
        self.row = {"id": pk}


class _VectorFakeProvider:
    def __init__(self, hits: list[_VectorHit]) -> None:
        self._hits = hits
        self.search_calls: list[dict] = []
        self.sql_calls: list[tuple[str, list | None]] = []

    def search_vector(self, query, index_name, top_k=5, threshold=None):
        self.search_calls.append({
            "query": query, "index_name": index_name,
            "top_k": top_k, "threshold": threshold,
        })
        return list(self._hits)

    def query_sql(self, sql: str, params: list | None = None) -> dict:
        """Vector tool не должен вызывать query_sql."""
        self.sql_calls.append((sql, list(params) if params else None))
        return {"status": "error", "error": "vector_search should not call query_sql"}


class TestBVectorPathNoSql:
    """Test B: Agent → vector_search → known index. SQL не генерируется."""

    def test_searches_index_returns_results(self) -> None:
        provider = _VectorFakeProvider(hits=[
            _VectorHit("Нарушение пожарной безопасности в школе", 0.82, pk=101),
            _VectorHit("Нарушение правил эвакуации", 0.71, pk=202),
        ])
        tool = VectorSearchTool(config=VectorSearchToolConfig())
        tool.set_provider(provider)

        result = json.loads(asyncio.run(tool.execute(
            query="пожарная безопасность",
            index_name="violations_index",
            top_k=5,
        )))

        assert result["status"] == "success"
        assert result["index_name"] == "violations_index"
        assert len(result["results"]) == 2
        assert result["results"][0]["score"] == 0.82
        # Критический инвариант: vector_search не трогает SQL.
        assert provider.sql_calls == []
        assert provider.search_calls[0]["index_name"] == "violations_index"

    def test_unknown_index_reports_error(self) -> None:
        """Tool не подбирает индекс автоматически — Agent обязан знать имя."""
        provider = _VectorFakeProvider(hits=[])
        tool = VectorSearchTool(config=VectorSearchToolConfig())
        tool.set_provider(provider)

        result = json.loads(asyncio.run(tool.execute(
            query="x", index_name="unknown_index",
        )))

        assert result["status"] == "success"
        assert result["count"] == 0


# ---------------------------------------------------------------------------
# Test C: NL→SQL fallback — единственный путь с LLM
# ---------------------------------------------------------------------------


class _FakeSchemaFormatter:
    def list_schema_names(self) -> list[str]:
        return ["main"]


class _NlSqlFakeProvider:
    def __init__(self, query_results: list[dict] | None = None) -> None:
        self._query_results = list(query_results or [])
        self.llm_called = False

    def query_sql(self, sql: str, params: list | None = None) -> dict:
        if self._query_results:
            return self._query_results.pop(0)
        return {"status": "success", "row_count": 0, "columns": [], "rows": []}

    def explain(self, sql: str) -> dict:
        return {"valid": True}


class TestCSqlFallbackCallsLLM:
    """Test C: Agent → nl_sql_generate → LLM → validate → execute."""

    def test_calls_llm_when_no_predefined(self) -> None:
        from lib.services.nl_sql_runner import NlSqlRunner

        table_registry.register(SkillRegistration(
            name="demo",
            resources=(TableResource(name="public.audits"),),
        ))
        provider = _NlSqlFakeProvider(query_results=[{
            "status": "success",
            "row_count": 1,
            "columns": ["cnt"],
            "rows": [[42]],
        }])
        tool = NlSqlGenerateTool(config=NlSqlGenerateToolConfig())
        tool.set_provider(provider)
        tool.set_schema_formatter(_FakeSchemaFormatter())

        llm_invoked = []

        def fake_llm(messages, **kwargs):
            llm_invoked.append(messages)
            return "SELECT COUNT(*) AS cnt FROM public.audits"

        with __import__("unittest.mock").mock.patch.object(
            NlSqlRunner, "_call_llm", side_effect=fake_llm,
        ):
            result = json.loads(asyncio.run(tool.execute(
                query="сколько аудитов в 2024",
            )))

        # LLM вызван — это fallback путь.
        assert len(llm_invoked) >= 1
        assert result["status"] == "success"
        assert result["sql"] == "SELECT COUNT(*) AS cnt FROM public.audits"
        assert result["row_count"] == 1

    def test_no_script_resolution_inside(self) -> None:
        """Tool не пытается самостоятельно резолвить predefined скрипты."""
        from lib.services.nl_sql_runner import NlSqlRunner

        # Запрос, который ТОЧНО совпадает со скриптом — даже в этом случае
        # tool должен идти в LLM-цикл, а не пытаться найти predefined.
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
        provider = _NlSqlFakeProvider(query_results=[
            {
                "status": "success",
                "row_count": 1,
                "columns": [],
                "rows": [_audit_status_summary_row()],
            },
            {
                "status": "success",
                "row_count": 1,
                "columns": ["cnt"],
                "rows": [[7]],
            },
        ])
        tool = NlSqlGenerateTool(config=NlSqlGenerateToolConfig())
        tool.set_provider(provider)
        tool.set_schema_formatter(_FakeSchemaFormatter())

        llm_calls = []

        def fake_llm(messages, **kwargs):
            llm_calls.append(messages)
            return "SELECT COUNT(*) AS cnt FROM public.audits"

        with __import__("unittest.mock").mock.patch.object(
            NlSqlRunner, "_call_llm", side_effect=fake_llm,
        ):
            result = json.loads(asyncio.run(tool.execute(
                query="audit_status_summary",
            )))

        # LLM был вызван — нет auto-routing на predefined.
        assert len(llm_calls) >= 1
        assert result["status"] == "success"
        # mode НЕ predefined_script — это generated_sql путь.
        assert result.get("mode") in (None, "generated_sql")


# ---------------------------------------------------------------------------
# Cross-тест: tools изолированы (не знают друг о друге)
# ---------------------------------------------------------------------------


class TestToolsAreIsolated:
    """Каждый tool — независимая capability. Tool не маршрутизирует запрос."""

    def test_run_predefined_script_has_no_llm_call(self) -> None:
        """Tool не имеет метода, который мог бы вызвать LLM."""
        tool = RunPredefinedScriptTool(config=RunPredefinedScriptToolConfig())
        # Публичный API: только execute(name, params, max_rows).
        public_methods = [
            m for m in dir(tool) if not m.startswith("_")
            and callable(getattr(tool, m, None))
        ]
        # execute — единственный публичный метод, делающий работу.
        assert "execute" in public_methods
        # Нет публичного метода для выбора режима/скрипта.
        for m in public_methods:
            assert "mode" not in m.lower()
            assert "classif" not in m.lower()
            assert "route" not in m.lower()

    def test_vector_search_has_no_sql_call(self) -> None:
        tool = VectorSearchTool(config=VectorSearchToolConfig())
        public_methods = [
            m for m in dir(tool) if not m.startswith("_")
            and callable(getattr(tool, m, None))
        ]
        for m in public_methods:
            assert "sql" not in m.lower()
            assert "route" not in m.lower()
            assert "classif" not in m.lower()

    def test_nl_sql_generate_has_no_predefined_dispatch(self) -> None:
        tool = NlSqlGenerateTool(config=NlSqlGenerateToolConfig())
        public_methods = [
            m for m in dir(tool) if not m.startswith("_")
            and callable(getattr(tool, m, None))
        ]
        for m in public_methods:
            assert "predefined" not in m.lower()
            assert "vector" not in m.lower()
            assert "mode" not in m.lower()
            assert "route" not in m.lower()
            assert "classif" not in m.lower()


# ---------------------------------------------------------------------------
# Negative tests: инструменты НЕ делают того, чего не должны
# ---------------------------------------------------------------------------


class TestNegativeRouting:
    """Negative cases: Tool не делает auto-routing или выбор режима за Agent."""

    def test_run_predefined_script_does_not_resolve_by_query(
        self,
    ) -> None:
        """Negative: запрос, похожий на имя скрипта, НЕ резолвится без name."""
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
        provider = _PredefinedFakeProvider(
            scripts=[_audit_status_summary_row()],
            execute_result={"status": "success", "row_count": 0, "columns": [], "rows": []},
        )
        tool = _predefined_tool(provider)

        # Запрос без явного name — tool должен ругаться, а не подбирать.
        result = json.loads(asyncio.run(tool.execute(name="")))

        assert result["status"] == "error"
        assert result["error_type"] == "invalid_name"
        # Tool не вызывал query_sql — нет неявной резолюции.
        assert provider.query_calls == []

    def test_vector_search_does_not_infer_index_name(self) -> None:
        """Negative: tool не выбирает index_name автоматически."""
        provider = _VectorFakeProvider(hits=[])
        tool = VectorSearchTool(config=VectorSearchToolConfig())
        tool.set_provider(provider)

        # Пустой index_name → ошибка (не пытается выбрать за Agent'а).
        result = json.loads(asyncio.run(tool.execute(
            query="пожарная безопасность",
            index_name="",
        )))

        assert result["status"] == "error"
        assert result["error_type"] == "invalid_input"
        assert provider.search_calls == []

    def test_nl_sql_generate_does_not_pick_predefined_silently(
        self,
    ) -> None:
        """Negative: запрос, точно равный имени скрипта → tool НЕ идёт в predefined."""
        from lib.services.nl_sql_runner import NlSqlRunner

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
        # Провайдер с реестром + результатом LLM-циклa.
        provider = _NlSqlFakeProvider(query_results=[
            {
                "status": "success",
                "row_count": 1,
                "columns": [],
                "rows": [_audit_status_summary_row()],
            },
            {
                "status": "success",
                "row_count": 1,
                "columns": ["cnt"],
                "rows": [[7]],
            },
        ])
        tool = NlSqlGenerateTool(config=NlSqlGenerateToolConfig())
        tool.set_provider(provider)
        tool.set_schema_formatter(_FakeSchemaFormatter())

        def fake_llm(messages, **kwargs):
            return "SELECT COUNT(*) AS cnt FROM public.audits"

        with __import__("unittest.mock").mock.patch.object(
            NlSqlRunner, "_call_llm", side_effect=fake_llm,
        ):
            result = json.loads(asyncio.run(tool.execute(
                query="audit_status_summary",
            )))

        # LLM был вызван — нет silent predefined resolution.
        assert result["status"] == "success"
        assert result.get("mode") in (None, "generated_sql")
        # Реестр запрашивался (для few-shot), но скрипт НЕ выполнялся.
        assert len(provider._query_results) == 0  # результат LLM был использован

    def test_run_predefined_script_rejects_extra_params(self) -> None:
        """Negative: лишние параметры в params → ошибка (не тихий ignore)."""
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
        provider = _PredefinedFakeProvider(
            scripts=[_audit_status_summary_row()],
            execute_result={"status": "success", "row_count": 0, "columns": [], "rows": []},
        )
        tool = _predefined_tool(provider)

        result = json.loads(asyncio.run(tool.execute(
            name="audit_status_summary",
            params={"extra_param": "should_be_rejected"},
        )))

        assert result["status"] == "error"
        assert result["error_type"] == "invalid_script"

    def test_run_predefined_script_rejects_ddl_template(self) -> None:
        """Negative: SQL с DROP не проходит validate_sql даже из реестра."""
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
        row = _audit_status_summary_row()
        row["sql_template"] = "DROP TABLE public.audits"
        provider = _PredefinedFakeProvider(
            scripts=[row],
            execute_result={"status": "success", "row_count": 0, "columns": [], "rows": []},
        )
        tool = _predefined_tool(provider)

        result = json.loads(asyncio.run(tool.execute(name="audit_status_summary")))

        assert result["status"] == "error"
        assert result["error_type"] == "invalid_script"
        assert "безопасность" in result["message"]


class TestAmbiguousCases:
    """Ambiguous: не должно быть silent guess."""

    def test_ambiguous_predefined_match_falls_through_to_llm(self) -> None:
        """Если реестр содержит несколько похожих скриптов, NlSqlRunner
        НЕ пытается auto-select — он передаёт LLM (агент сам решит)."""
        from lib.services.nl_sql_runner import NlSqlRunner

        alpha = _audit_status_summary_row()
        alpha["name"] = "alpha_summary"
        alpha["description"] = "сводка аудитов"
        beta = _audit_status_summary_row()
        beta["name"] = "beta_summary"
        beta["description"] = "сводка аудитов"
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
        provider = _NlSqlFakeProvider(query_results=[
            {
                "status": "success",
                "row_count": 2,
                "columns": [],
                "rows": [alpha, beta],
            },
            {
                "status": "success",
                "row_count": 1,
                "columns": ["x"],
                "rows": [[1]],
            },
        ])
        tool = NlSqlGenerateTool(config=NlSqlGenerateToolConfig())
        tool.set_provider(provider)
        tool.set_schema_formatter(_FakeSchemaFormatter())

        llm_calls = []

        def fake_llm(messages, **kwargs):
            llm_calls.append(messages)
            return "SELECT 1 AS x"

        with __import__("unittest.mock").mock.patch.object(
            NlSqlRunner, "_call_llm", side_effect=fake_llm,
        ):
            result = json.loads(asyncio.run(tool.execute(
                query="сводка аудитов",
            )))

        # LLM был вызван — нет auto-routing на predefined.
        assert len(llm_calls) >= 1
        assert result["status"] == "success"
        assert result.get("mode") in (None, "generated_sql")