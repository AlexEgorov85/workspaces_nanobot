"""Behavioral-тесты для ``audit_analyzer`` после рефакторинга.

Проверяют **поведение** Agent ↔ Tool contracts в новой архитектуре
(только ``duckdb_query`` и ``vector_search``). Не проверяют внутреннюю
реализацию удалённых компонентов.

Покрывают шаги 24–27 плана рефакторинга:

* TestAuditAnalyerPredefinedScripts — поведение predefined через
  ``duckdb_query`` (lookup SQL + параметризованное выполнение).
* TestAuditAnalyerVectorSearch — семантический поиск и контракт ответа.
* TestAuditAnalyerFreeSql — свободный SQL (COUNT/GROUP BY/JOIN/empty).
* TestAuditAnalyerSqlErrorRetry — SQL error → Agent исправляет → успех.
* TestAuditAnalyerSkillToolSet — Agent видит только ``duckdb_query`` +
  ``vector_search`` (без удалённых tools).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb
import pytest

from lib.utils.sql_safety import validate_sql
from workspace.tools.duckdb_query_tool import DuckdbQueryTool, DuckdbQueryToolConfig
from workspace.tools.vector_search_tool import (
    VectorSearchTool,
    VectorSearchToolConfig,
)


SKILL_DIR = Path("workspace/skills/audit_analyzer")


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_audit_db() -> tuple[DuckdbQueryTool, None]:
    """Создать ``DuckdbQueryTool`` + connection-factory со схемой ``oarb``.

    ``set_connection_factory`` пересоздаёт in-memory DuckDB на каждый
    вызов ``execute`` (см. ``duckdb_query_tool._open_duckdb_connection``),
    поэтому seed-данные нужно заполнять внутри factory, а не снаружи.
    """
    config = DuckdbQueryToolConfig()
    tool = DuckdbQueryTool(config=config)

    def factory() -> duckdb.DuckDBPyConnection:
        conn = duckdb.connect(":memory:")
        conn.execute("CREATE SCHEMA IF NOT EXISTS oarb")
        conn.execute("CREATE SCHEMA IF NOT EXISTS public")
        conn.execute(
            "CREATE TABLE oarb.audits ("
            "id INTEGER, title VARCHAR, audit_type VARCHAR, "
            "actual_date DATE, status VARCHAR)"
        )
        conn.executemany(
            "INSERT INTO oarb.audits VALUES (?, ?, ?, ?, ?)",
            [
                (1, "Fire safety", "planned", None, "Запланирована"),
                (2, "Financial audit", "planned", "2024-05-01", "Завершена"),
                (3, "Compliance review", "extra", "2024-08-15", "Завершена"),
                (4, "School check", "extra", "2025-02-10", "В работе"),
            ],
        )
        conn.execute(
            "CREATE TABLE oarb.violations ("
            "id INTEGER, audit_id INTEGER, violation_code VARCHAR, "
            "description VARCHAR, severity VARCHAR)"
        )
        conn.executemany(
            "INSERT INTO oarb.violations VALUES (?, ?, ?, ?, ?)",
            [
                (10, 2, "F-1", "Fire exit blocked", "high"),
                (11, 2, "F-2", "No extinguisher", "medium"),
                (12, 3, "D-1", "Missing signature", "low"),
            ],
        )
        conn.execute(
            "CREATE TABLE public.agent_predefined_scripts ("
            "name VARCHAR, description VARCHAR, sql_template VARCHAR, "
            "parameters JSON)"
        )
        conn.executemany(
            "INSERT INTO public.agent_predefined_scripts VALUES (?, ?, ?, ?)",
            [
                (
                    "audit_status_summary",
                    "Сводка по статусам аудитов",
                    "SELECT status, COUNT(*) AS cnt FROM oarb.audits "
                    "GROUP BY status ORDER BY status",
                    "{}",
                ),
                (
                    "violations_by_period",
                    "Нарушения за период",
                    "SELECT v.id, v.violation_code, v.description "
                    "FROM oarb.violations v "
                    "JOIN oarb.audits a ON a.id = v.audit_id "
                    "WHERE a.actual_date >= ? AND a.actual_date <= ?",
                    '{"date_from": {"type": "date", "required": true}, '
                    '"date_to": {"type": "date", "required": true}}',
                ),
                (
                    "top_violations_by_type",
                    "Топ кодов нарушений",
                    "SELECT violation_code, COUNT(*) AS cnt "
                    "FROM oarb.violations GROUP BY violation_code "
                    "ORDER BY cnt DESC LIMIT ?",
                    '{"max_rows": {"type": "integer", "required": true}}',
                ),
            ],
        )
        return conn

    tool.set_connection_factory(factory)
    return tool, None


@dataclass
class _FakeHit:
    content: str
    score: float
    pk_value: Any = None
    source: str = ""
    table: str = ""
    chunk: str = ""
    matched_chunks: int = 1
    row: dict = field(default_factory=dict)


class _StubProvider:
    def __init__(self, hits_by_index: dict[str, list[_FakeHit]] | None = None) -> None:
        self._hits = hits_by_index or {}

    def search_vector(self, query, index_name, top_k=5, threshold=None):
        return list(self._hits.get(index_name, []))


# ---------------------------------------------------------------------------
# Predefined: Agent читает SQL из PG-таблицы через duckdb_query
# ---------------------------------------------------------------------------


class TestAuditAnalyerPredefinedScripts:
    """Шаг 24: Agent использует predefined через inline SQL + duckdb_query."""

    def test_predefined_lookup_via_duckdb(self) -> None:
        tool, _ = _make_audit_db()
        sql = (
            "SELECT sql_template FROM public.agent_predefined_scripts "
            "WHERE name = ?"
        )
        payload = json.loads(_run(tool.execute(sql=sql, params={"name": "audit_status_summary"})))
        assert payload["status"] == "success"
        assert "GROUP BY status" in payload["rows"][0][0]

    def test_predefined_execute_after_lookup(self) -> None:
        """Agent: 1) lookup → 2) execute SQL."""
        tool, _ = _make_audit_db()
        lookup = json.loads(_run(
            tool.execute(
                sql="SELECT sql_template FROM public.agent_predefined_scripts "
                    "WHERE name = ?",
                params={"name": "audit_status_summary"},
            )
        ))
        assert lookup["status"] == "success"
        predefined_sql = lookup["rows"][0][0]

        exec_payload = json.loads(_run(tool.execute(sql=predefined_sql)))
        assert exec_payload["status"] == "success"
        assert exec_payload["row_count"] == 3
        statuses = {row[0] for row in exec_payload["rows"]}
        assert statuses == {"Завершена", "В работе", "Запланирована"}

    def test_predefined_with_required_params(self) -> None:
        """Параметризованный predefined: date_from и date_to обязательны."""
        tool, _ = _make_audit_db()
        sql = (
            "SELECT v.id, v.violation_code "
            "FROM oarb.violations v "
            "JOIN oarb.audits a ON a.id = v.audit_id "
            "WHERE a.actual_date >= ? AND a.actual_date <= ?"
        )
        params = {"date_from": "2024-01-01", "date_to": "2024-12-31"}
        payload = json.loads(_run(tool.execute(sql=sql, params=params)))
        assert payload["status"] == "success"
        assert payload["row_count"] == 3
        codes = sorted(row[1] for row in payload["rows"])
        assert codes == ["D-1", "F-1", "F-2"]

    def test_predefined_missing_required_param_excluded_from_skill(
        self,
    ) -> None:
        """Если параметр не задан, predefined не используется — Agent
        переходит к свободному SQL (поведение на стороне SKILL.md, не tool'а).
        Этот тест проверяет, что SKILL.md явно требует обязательные параметры.
        """
        skill_text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        assert "обязательных" in skill_text, (
            "SKILL.md должен явно упоминать обязательные параметры "
            "predefined скриптов"
        )


# ---------------------------------------------------------------------------
# Vector search: семантический поиск, не превращается в SQL
# ---------------------------------------------------------------------------


class TestAuditAnalyerVectorSearch:
    """Шаг 25: vector_search передаёт index_name, возвращает score/text/metadata."""

    def test_vector_search_passes_index_name(self) -> None:
        tool = VectorSearchTool(config=VectorSearchToolConfig())
        seen: dict[str, Any] = {}

        class _CaptureProvider(_StubProvider):
            def search_vector(self, query, index_name, top_k=5, threshold=None):
                seen["index_name"] = index_name
                seen["query"] = query
                return [
                    _FakeHit(content="Fire safety", score=0.92, pk_value=42),
                ]

        tool.set_provider(_CaptureProvider())
        payload = json.loads(_run(
            tool.execute(query="пожарная безопасность", index_name="audits_index")
        ))
        assert payload["status"] == "success"
        assert seen["index_name"] == "audits_index"
        assert payload["count"] == 1

    def test_vector_search_preserves_metadata(self) -> None:
        tool = VectorSearchTool(config=VectorSearchToolConfig())
        tool.set_provider(_StubProvider({
            "audits_index": [
                _FakeHit(
                    content="Fire safety check",
                    score=0.91,
                    pk_value=42,
                    table="oarb.audits",
                    row={"id": 42, "title": "Fire safety"},
                ),
            ],
        }))
        payload = json.loads(_run(
            tool.execute(query="fire", index_name="audits_index")
        ))
        result = payload["results"][0]
        assert result["score"] == pytest.approx(0.91)
        assert "Fire" in result["text"]
        assert result["metadata"]["table"] == "oarb.audits"
        assert result["metadata"]["row"]["id"] == 42

    def test_vector_search_multiple_results_not_lost(self) -> None:
        tool = VectorSearchTool(config=VectorSearchToolConfig())
        tool.set_provider(_StubProvider({
            "violations_index": [
                _FakeHit(content="Fire exit blocked", score=0.92, pk_value=10),
                _FakeHit(content="No extinguisher", score=0.85, pk_value=11),
                _FakeHit(content="Expired certificate", score=0.81, pk_value=12),
            ],
        }))
        payload = json.loads(_run(
            tool.execute(query="безопасность", index_name="violations_index")
        ))
        assert payload["status"] == "success"
        assert payload["count"] == 3
        scores = [r["score"] for r in payload["results"]]
        assert scores == [0.92, 0.85, 0.81]

    def test_semantic_query_not_routed_to_sql(self) -> None:
        """Agent не должен превращать семантический запрос в LIKE-SQL.

        SKILL.md требует: для семантического поиска использовать
        ``vector_search``, не ``LIKE '%...%'`` через ``duckdb_query``.
        """
        skill_text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        assert "vector_search" in skill_text
        assert "семантическ" in skill_text.lower()
        assert "LIKE" in skill_text, (
            "SKILL.md должен явно запрещать LIKE для семантического поиска"
        )


# ---------------------------------------------------------------------------
# Свободный SQL: COUNT/GROUP BY/JOIN/empty
# ---------------------------------------------------------------------------


class TestAuditAnalyerFreeSql:
    """Шаг 26: свободный SQL — Agent формирует SELECT, вызывает duckdb_query."""

    def test_count_audits(self) -> None:
        tool, _ = _make_audit_db()
        sql = "SELECT COUNT(*) AS total FROM oarb.audits"
        payload = json.loads(_run(tool.execute(sql=sql)))
        assert payload["status"] == "success"
        assert payload["columns"] == ["total"]
        assert payload["rows"] == [[4]]
        assert payload["row_count"] == 1

    def test_group_by_status(self) -> None:
        tool, _ = _make_audit_db()
        sql = (
            "SELECT status, COUNT(*) AS cnt FROM oarb.audits "
            "GROUP BY status ORDER BY status"
        )
        payload = json.loads(_run(tool.execute(sql=sql)))
        assert payload["status"] == "success"
        rows_by_status = {row[0]: row[1] for row in payload["rows"]}
        assert rows_by_status == {"В работе": 1, "Завершена": 2, "Запланирована": 1}

    def test_date_filter(self) -> None:
        tool, _ = _make_audit_db()
        sql = (
            "SELECT id, title FROM oarb.audits "
            "WHERE actual_date >= ? AND actual_date < ?"
        )
        payload = json.loads(_run(
            tool.execute(sql=sql, params={"d1": "2025-01-01", "d2": "2026-01-01"})
        ))
        assert payload["status"] == "success"
        assert payload["row_count"] == 1
        assert payload["rows"][0][1] == "School check"

    def test_join_audits_violations(self) -> None:
        tool, _ = _make_audit_db()
        sql = (
            "SELECT a.title, v.violation_code "
            "FROM oarb.audits a JOIN oarb.violations v ON v.audit_id = a.id "
            "ORDER BY a.id, v.id"
        )
        payload = json.loads(_run(tool.execute(sql=sql)))
        assert payload["status"] == "success"
        assert payload["row_count"] == 3
        # Никогда не возвращаем пустой results при ненулевых rows.
        assert payload["status"] == "success"
        assert payload["rows"]

    def test_empty_result_is_not_error(self) -> None:
        """Пустой результат — нормальный ответ, не ошибка."""
        tool, _ = _make_audit_db()
        sql = "SELECT * FROM oarb.audits WHERE actual_date IS NULL AND id = 999"
        payload = json.loads(_run(tool.execute(sql=sql)))
        assert payload["status"] == "success"
        assert payload["columns"] == ["id", "title", "audit_type", "actual_date", "status"]
        assert payload["rows"] == []
        assert payload["row_count"] == 0


# ---------------------------------------------------------------------------
# SQL error → Agent retry
# ---------------------------------------------------------------------------


class TestAuditAnalyerSqlErrorRetry:
    """Шаг 27: SQL error → Agent видит structured error → исправляет → успех.

    Retry выполняет Agent, не отдельный Python service. Этот тест проверяет
    только contract: ошибка структурирована, Agent может её прочитать.
    """

    def test_unknown_table_returns_structured_error(self) -> None:
        tool, _ = _make_audit_db()
        sql = "SELECT * FROM oarb.nonexistent_table"
        payload = json.loads(_run(tool.execute(sql=sql)))
        assert payload["status"] == "error"
        assert "error_type" in payload
        assert "message" in payload
        assert "oarb.nonexistent_table" in payload["message"]

    def test_syntax_error_returns_structured_error(self) -> None:
        tool, _ = _make_audit_db()
        sql = "SELECT FORM oarb.audits"
        payload = json.loads(_run(tool.execute(sql=sql)))
        assert payload["status"] == "error"
        assert payload["error_type"] == "sql_error"
        assert payload["message"]

    def test_agent_can_recover_after_sql_error(self) -> None:
        """Сценарий Agent loop: первая попытка с ошибкой → вторая успешна."""
        tool, _ = _make_audit_db()

        # Попытка 1: неверное имя таблицы.
        bad = json.loads(_run(
            tool.execute(sql="SELECT COUNT(*) FROM oarb.audit_table")
        ))
        assert bad["status"] == "error"

        # Agent читает message и исправляет SQL.
        # Попытка 2: правильный SQL.
        good = json.loads(_run(
            tool.execute(sql="SELECT COUNT(*) FROM oarb.audits")
        ))
        assert good["status"] == "success"
        assert good["rows"] == [[4]]


# ---------------------------------------------------------------------------
# Tool set: Agent видит только два generic tool'а
# ---------------------------------------------------------------------------


class TestAuditAnalyerSkillToolSet:
    """Шаг 35: Agent видит только ``duckdb_query`` + ``vector_search``."""

    def test_skill_md_references_only_two_tools(self) -> None:
        skill_text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        assert "duckdb_query" in skill_text
        assert "vector_search" in skill_text

    def test_skill_md_forbids_removed_tools(self) -> None:
        skill_text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        for forbidden in ("run_predefined_script", "nl_sql_generate"):
            assert forbidden in skill_text, (
                f"SKILL.md должен явно запрещать {forbidden}"
            )

    def test_no_removed_tool_modules_exist(self) -> None:
        """Удалённые tool-модули не должны существовать в ``workspace/tools/``."""
        removed = [
            "workspace/tools/run_predefined_script.py",
            "workspace/tools/nl_sql_generate.py",
            "workspace/tools/column_descriptions.py",
        ]
        for path in removed:
            assert not Path(path).exists(), f"{path} должен быть удалён"

    def test_no_removed_core_services_exist(self) -> None:
        """Удалённые core-сервисы не должны существовать в ``lib/services/``."""
        removed = [
            "lib/services/nl_sql_runner.py",
            "lib/services/schema_formatter.py",
            "lib/services/column_descriptions.py",
            "lib/services/predefined_script_registry.py",
            "lib/services/predefined_script_request.py",
            "lib/services/predefined_script_validator.py",
        ]
        for path in removed:
            assert not Path(path).exists(), f"{path} должен быть удалён"

    def test_skill_references_all_exist(self) -> None:
        """Все обязательные references должны существовать (progressive disclosure)."""
        required = [
            "references/architecture.md",
            "references/schema.md",
            "references/vector_indexes.md",
            "references/sql_guidance.md",
            "references/predefined_scripts.md",
        ]
        for ref in required:
            path = SKILL_DIR / ref
            assert path.is_file(), f"{path} должен существовать"
            assert path.stat().st_size > 200, f"{path} слишком мал"

    def test_skill_predefined_module_exists(self) -> None:
        """``workspace/skills/audit_analyzer/predefined/`` — режим predefined.

        Заменил удалённый ``scripts/sql_generator.py`` (deprecated skill-side
        helper для LLM-генерации SQL): Agent теперь формирует SQL сам по
        ``references/sql_guidance.md`` или использует ``predefined.run()``.
        """
        assert (SKILL_DIR / "predefined" / "mode.py").is_file()
        assert (SKILL_DIR / "predefined" / "builder.py").is_file()
        assert (SKILL_DIR / "predefined" / "validator.py").is_file()
        assert (SKILL_DIR / "predefined" / "models.py").is_file()
        assert (SKILL_DIR / "predefined" / "scripts.py").is_file()

    def test_no_sql_generator_helper_exists(self) -> None:
        """Удалённый ``scripts/sql_generator.py`` не должен существовать."""
        assert not (SKILL_DIR / "scripts").exists() or not (
            SKILL_DIR / "scripts" / "sql_generator.py"
        ).exists()


# ---------------------------------------------------------------------------
# Generic DuckDB contract (шаг 4 — structured rows, не строка)
# ---------------------------------------------------------------------------


class TestGenericDuckdbQueryContract:
    """Шаг 4: generic DuckDB tool возвращает structured rows, не строку."""

    def test_simple_select_returns_structured_rows(self) -> None:
        conn = duckdb.connect(":memory:")
        conn.execute("CREATE TABLE t (id INTEGER, value VARCHAR)")
        conn.execute("INSERT INTO t VALUES (1, 'test')")
        tool = DuckdbQueryTool(config=DuckdbQueryToolConfig())
        tool.set_connection_factory(lambda: conn)
        payload = json.loads(_run(tool.execute(sql="SELECT 1 AS id, 'test' AS value")))
        assert payload["status"] == "success"
        assert payload["columns"] == ["id", "value"]
        assert payload["rows"] == [[1, "test"]]
        assert payload["row_count"] == 1

    def test_empty_where_false_returns_empty_rows(self) -> None:
        conn = duckdb.connect(":memory:")
        conn.execute("CREATE TABLE t (x INTEGER)")
        tool = DuckdbQueryTool(config=DuckdbQueryToolConfig())
        tool.set_connection_factory(lambda: conn)
        payload = json.loads(_run(tool.execute(sql="SELECT 1 WHERE FALSE")))
        assert payload["status"] == "success"
        assert payload["columns"] == ["1"]
        assert payload["rows"] == []
        assert payload["row_count"] == 0

    def test_ddl_rejected_with_structured_error(self) -> None:
        tool = DuckdbQueryTool(config=DuckdbQueryToolConfig())
        payload = json.loads(_run(tool.execute(sql="DROP TABLE x")))
        assert payload["status"] == "error"
        assert payload["error_type"] == "sql_error"

    def test_safety_validator_rejects_unsafe_sql(self) -> None:
        assert validate_sql("SELECT 1") is None
        assert validate_sql("INSERT INTO t VALUES (1)") is not None
        assert validate_sql("DROP TABLE t") is not None
        assert validate_sql("DELETE FROM t") is not None
