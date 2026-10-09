"""Integration tests: Skill workflow + Core capability.

Демонстрирует сценарии из TARGET_ARCHITECTURE.md §8 и SKILL.md Decision procedure.
Проверяет данные через generic Core capability (``CacheProvider.query_sql`` /
``CacheProvider.search_vector``), а не Agent-facing tools — те удалены (этап 18).
"""

from __future__ import annotations

import json
from dataclasses import field, dataclass
from pathlib import Path
from typing import Any

import duckdb
import pytest

from lib.services.cache_provider import SearchResult
from lib.utils.sql_safety import validate_sql


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


class _StubDB:
    """Адаптер in-memory DuckDB к ``CacheProvider.query_sql``."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self._conn = conn

    def query_sql(self, sql: str, params: list[Any] | None = None) -> dict[str, Any]:
        try:
            if params:
                result = self._conn.execute(sql, list(params))
            else:
                result = self._conn.execute(sql)
            columns = [c[0] for c in result.description] if result.description else []
            rows = [dict(zip(columns, r, strict=False)) for r in result.fetchall()]
            return {
                "status": "success",
                "row_count": len(rows),
                "columns": columns,
                "rows": rows,
            }
        except Exception as exc:
            return {
                "status": "error",
                "row_count": 0,
                "columns": [],
                "rows": [],
                "error": str(exc),
            }


class _StubVector:
    """Адаптер к ``CacheProvider.search_vector``."""

    def __init__(self, hits_by_index: dict[str, list[SearchResult]] | None = None) -> None:
        self._hits = hits_by_index or {}

    def search_vector(
        self, query, index_name, top_k=5, threshold=None
    ) -> list[SearchResult]:
        return list(self._hits.get(index_name, []))


def _make_duckdb() -> _StubDB:
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
    return _StubDB(conn)


class TestScenario1Aggregation:
    """SKILL: «Сколько проверок по годам?» → generic query_sql."""

    def test_aggregation_query(self) -> None:
        db = _make_duckdb()
        # sql_guidance rule: SELECT ... GROUP BY year
        sql = (
            "SELECT year, COUNT(*) AS cnt FROM audits GROUP BY year ORDER BY year"
        )
        assert validate_sql(sql) is None
        payload = db.query_sql(sql)
        assert payload["status"] == "success"
        assert [(r["year"], r["cnt"]) for r in payload["rows"]] == [(2024, 2), (2025, 2)]


class TestScenario2SemanticSearch:
    """SKILL: «Найди похожие нарушения» → generic search_vector."""

    def test_vector_search_with_index_name(self) -> None:
        provider = _StubVector({
            "violations_index": [
                SearchResult(content="Fire safety violation", score=0.9, pk_value=42),
            ],
        })
        results = provider.search_vector(
            "пожарная безопасность", index_name="violations_index"
        )
        assert len(results) == 1
        assert results[0].pk_value == 42
        assert "Fire" in results[0].content


class TestScenario3VectorThenDuckdb:
    """SKILL: «Найди нарушения + посчитай по годам» → search_vector → query_sql."""

    def test_composite_workflow(self) -> None:
        provider = _StubVector({
            "violations_index": [
                SearchResult(content="Fire safety issue", score=0.9, pk_value=1),
                SearchResult(content="Fire safety alert", score=0.85, pk_value=3),
            ],
        })
        results = provider.search_vector(
            "пожарная безопасность", index_name="violations_index"
        )
        ids = [r.pk_value for r in results]
        assert ids == [1, 3]

        db = _make_duckdb()
        ids_csv = ",".join(str(i) for i in ids)
        sql = (
            f"SELECT year, COUNT(*) AS cnt FROM audits "
            f"WHERE id IN ({ids_csv}) GROUP BY year ORDER BY year"
        )
        assert validate_sql(sql) is None
        payload = db.query_sql(sql)
        assert payload["status"] == "success"
        assert [(r["year"], r["cnt"]) for r in payload["rows"]] == [(2024, 1), (2025, 1)]


class TestScenario4UnknownTableRejected:
    """SKILL: «не использовать неизвестные таблицы» — query_sql это уважает."""

    def test_no_domain_routing_when_table_missing(self) -> None:
        db = _make_duckdb()
        payload = db.query_sql("SELECT * FROM nonexistent_table")
        assert payload["status"] == "error"
        # Core сообщает об ошибке без подсказок про audit-таблицы
        assert "audit" not in payload["error"].lower()


class TestScenario5SkillSelfContained:
    """Phase 8: SKILL.md — единственный источник документации skill'а.

    ``references/*.md`` удалены: progressive disclosure отключён в пользу
    self-contained документации. Тест проверяет, что SKILL.md достаточно
    полон для агента.
    """

    def test_skill_md_is_self_contained(self) -> None:
        skill_path = Path("workspace/skills/audit_analyzer/SKILL.md")
        assert skill_path.exists(), "SKILL.md must exist"
        text = skill_path.read_text(encoding="utf-8")
        assert len(text) > 2000, (
            f"SKILL.md слишком мал ({len(text)} chars) — "
            "весь контент из references/ должен быть в SKILL.md"
        )
        # 6 скриптов каталогизированы.
        for script in (
            "analytics_by_year_month",
            "audit_dynamics",
            "audit_effectiveness",
            "audit_types_stats",
            "top_audited_objects",
            "violations_by_type",
        ):
            assert script in text, f"SKILL.md должен упоминать {script}"
        # 3 FAISS-индекса каталогизированы.
        for index in ("audits_index", "violations_index", "audit_reports_index"):
            assert index in text

    def test_references_dir_not_required(self) -> None:
        """``references/`` удалён — SKILL.md self-contained (Phase 8)."""
        ref_dir = Path("workspace/skills/audit_analyzer/references")
        if ref_dir.exists():
            md_files = list(ref_dir.glob("*.md"))
            assert not md_files, (
                f"references/*.md должны быть удалены: {md_files}"
            )

    def test_agent_tools_are_the_only_audit_data_path(self) -> None:
        """Agent-facing tools — единственный рабочий путь к данным.

        Этап 18 удалил ``duckdb_query`` / ``vector_search`` и оставил агенту
        только CLI skill'а. Это решение оказалось неисполнимым: gateway
        держит ``cache.duckdb`` открытым весь свой жизненный цикл, поэтому
        CLI в отдельном процессе получает ``File is already open in ... PID
        ...`` даже в ``read_only`` и падает с «DuckDB-кеш не найден» при
        существующем файле. Агент остался без доступа к данным capability.

        Инвариант, который здесь защищается, — не «tool'ов нет», а
        «tool'ы есть И агент не отправлен в CLI»: tool исполняется в
        процессе gateway на его открытом ``CacheProvider``.
        """
        present = [
            "workspace/tools/duckdb_query_tool.py",
            "workspace/tools/vector_search_tool.py",
        ]
        for path in present:
            assert Path(path).exists(), (
                f"{path} должен существовать: это единственный рабочий путь "
                "агента к данным capability (CLI нерабочий при запущенном gateway)"
            )

    def test_agent_docs_do_not_route_to_cli(self) -> None:
        """Доки агента не отправляют его в CLI skill'а.

        Отдельная проверка от существования tool'ов: именно перенаправление
        в CLI («Tools ... отсутствуют — доступ через scripts/cli.py») держало
        агента в тупике.

        Проверяются не подстроки ``cli.py``, а однозначные маркеры
        **инструкции** отправлять в CLI. Ссылка на CLI в запрете («не
        вызывай») — это корректный текст, и ловить его нельзя: такой
        строковый запрет запрещал бы и правильную формулировку.
        """
        instruction_markers = {
            "AGENTS.md": "Tools `duckdb_query`/`vector_search` отсутствуют",
            "workspace/TOOLS.md": "## audit_analyzer — доступ через CLI",
            "workspace/skills/audit_analyzer/SKILL.md": "Доступ агента — через CLI",
        }
        for rel, marker in instruction_markers.items():
            path = Path(rel)
            if not path.is_file():
                continue
            assert marker not in path.read_text(encoding="utf-8"), (
                f"{rel}: агент всё ещё отправляется в CLI ({marker!r})"
            )

    def test_agent_docs_name_the_tools(self) -> None:
        """Доки, которые читает агент, называют оба tool'а."""
        for rel in (
            "AGENTS.md",
            "workspace/TOOLS.md",
            "workspace/skills/audit_analyzer/SKILL.md",
        ):
            path = Path(rel)
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            for tool in ("duckdb_query", "vector_search"):
                assert tool in text, f"{rel}: не упоминает {tool}"