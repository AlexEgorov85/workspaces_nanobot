"""Integration tests: Skill workflow + Tool.

Демонстрирует сценарии из TARGET_ARCHITECTURE.md §8 и SKILL.md Decision procedure.
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


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_duckdb() -> DuckdbQueryTool:
    config = DuckdbQueryToolConfig()
    tool = DuckdbQueryTool(config=config)

    def factory():
        conn = duckdb.connect(":memory:")
        conn.execute(
            "CREATE TABLE audits ("
            "id INTEGER, year INTEGER, title VARCHAR, auditee VARCHAR)"
        )
        conn.executemany(
            "INSERT INTO audits VALUES (?, ?, ?, ?)",
            [
                (1, 2024, "Fire safety check", "Org A"),
                (2, 2024, "Financial audit", "Org B"),
                (3, 2025, "Fire safety audit", "Org A"),
                (4, 2025, "Compliance review", "Org C"),
            ],
        )
        return conn

    tool.set_connection_factory(factory)
    return tool


class TestScenario1Aggregation:
    """SKILL: «Сколько проверок по годам?» → duckdb_query."""

    def test_aggregation_query(self) -> None:
        tool = _make_duckdb()
        # sql_guidance rule: SELECT ... GROUP BY year
        sql = (
            "SELECT year, COUNT(*) AS cnt FROM audits GROUP BY year ORDER BY year"
        )
        assert validate_sql(sql) is None
        payload = json.loads(_run(tool.execute(sql=sql)))
        assert payload["status"] == "success"
        assert payload["rows"] == [[2024, 2], [2025, 2]]


class TestScenario2SemanticSearch:
    """SKILL: «Найди похожие нарушения» → vector_search."""

    def test_vector_search_with_index_name(self) -> None:
        config = VectorSearchToolConfig()
        tool = VectorSearchTool(config=config)
        provider = _StubProvider({
            "violations_index": [
                _FakeHit(content="Fire safety violation", score=0.9, pk_value=42),
            ],
        })
        tool.set_provider(provider)
        payload = json.loads(_run(
            tool.execute(query="пожарная безопасность", index_name="violations_index")
        ))
        assert payload["status"] == "success"
        assert payload["count"] == 1
        assert payload["results"][0]["id"] == 42
        assert "Fire" in payload["results"][0]["text"]


class TestScenario3VectorThenDuckdb:
    """SKILL: «Найди нарушения + посчитай по годам» → vector_search → duckdb_query."""

    def test_composite_workflow(self) -> None:
        vector_config = VectorSearchToolConfig()
        vector_tool = VectorSearchTool(config=vector_config)
        provider = _StubProvider({
            "violations_index": [
                _FakeHit(content="Fire safety issue", score=0.9, pk_value=1),
                _FakeHit(content="Fire safety alert", score=0.85, pk_value=3),
            ],
        })
        vector_tool.set_provider(provider)
        v_payload = json.loads(_run(
            vector_tool.execute(query="пожарная безопасность", index_name="violations_index")
        ))
        ids = [r["id"] for r in v_payload["results"]]
        assert ids == [1, 3]

        duckdb_tool = _make_duckdb()
        ids_csv = ",".join(str(i) for i in ids)
        sql = (
            f"SELECT year, COUNT(*) FROM audits "
            f"WHERE id IN ({ids_csv}) GROUP BY year ORDER BY year"
        )
        assert validate_sql(sql) is None
        d_payload = json.loads(_run(duckdb_tool.execute(sql=sql)))
        assert d_payload["status"] == "success"
        assert d_payload["rows"] == [[2024, 1], [2025, 1]]


class TestScenario4UnknownTableRejected:
    """SKILL: «не использовать неизвестные таблицы» — duckdb_query это уважает."""

    def test_no_domain_routing_when_table_missing(self) -> None:
        tool = _make_duckdb()
        payload = json.loads(_run(
            tool.execute(sql="SELECT * FROM nonexistent_table")
        ))
        assert payload["status"] == "error"
        # Tool сообщает об ошибке без подсказок про audit-таблицы
        assert "audit" not in payload["message"].lower()


class TestScenario5SkillReferencesExist:
    """SKILL.md ссылается на references/, которые существуют."""

    @pytest.mark.parametrize(
        "ref_path",
        [
            "workspace/skills/audit_analyzer/references/schema.md",
            "workspace/skills/audit_analyzer/references/vector_indexes.md",
            "workspace/skills/audit_analyzer/references/sql_guidance.md",
        ],
    )
    def test_reference_file_exists(self, ref_path: str) -> None:
        path = Path(ref_path)
        assert path.exists(), f"{ref_path} must exist for progressive disclosure"
        assert path.stat().st_size > 200, f"{ref_path} too small"

    def test_skill_md_uses_decision_procedure(self) -> None:
        """SKILL.md должен содержать decision logic для выбора capability.

        Проверяем наличие одного из вариантов:
        - явный раздел "Decision procedure" / "Decision tree";
        - таблица "задача → capability";
        - список правил выбора tool'а.

        После рефакторинга архитектура — ровно 3 capability:
        ``run_predefined_script``, ``vector_search``, ``nl_sql_generate``.
        Прямой SQL через ``duckdb_query`` **не** рекомендуется в
        SKILL.md (см. Этап 14 коррекционного pass'а).
        """
        skill = Path("workspace/skills/audit_analyzer/SKILL.md").read_text(encoding="utf-8")
        markers = [
            "Decision procedure",
            "Decision tree",
            "| Задача |",
            "задача → capability",
        ]
        assert any(m in skill for m in markers), (
            "SKILL.md не содержит decision logic — добавьте раздел "
            "'Decision tree' / 'Decision procedure' или таблицу "
            "'задача → capability'."
        )
        assert "run_predefined_script" in skill
        assert "vector_search" in skill
        assert "nl_sql_generate" in skill
        # Прямой SQL через duckdb_query НЕ должен быть в SKILL.md —
        # архитектура трёх capability запрещает его как data flow.
        assert "duckdb_query" not in skill, (
            "SKILL.md не должен рекомендовать duckdb_query как путь "
            "получения audit data — только 3 capability."
        )