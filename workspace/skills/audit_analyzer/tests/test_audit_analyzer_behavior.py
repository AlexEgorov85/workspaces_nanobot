"""Behavioral-тесты для ``audit_analyzer`` после рефакторинга.

Проверяют **поведение** Skill ↔ Core contracts в новой архитектуре.
Agent-facing tools (``duckdb_query`` / ``vector_search``) удалены (этап 18):
Skill взаимодействует с Core напрямую через ``CacheProvider.query_sql``
и ``CacheProvider.search_vector`` (см. ``SKILL.md``).

Покрывают шаги 24–27 плана рефакторинга:

* TestAuditAnalyerPredefinedScripts — поведение predefined через
  ``predefined.run()`` (DB-source lookup + параметризованное выполнение).
* TestAuditAnalyerVectorSearch — семантический поиск и контракт ответа.
* TestAuditAnalyerFreeSql — свободный SQL (COUNT/GROUP BY/JOIN/empty).
* TestAuditAnalyerSqlErrorRetry — SQL error → Agent исправляет → успех.
* TestAuditAnalyerSkillToolSet — Agent работает через Core capability,
  а не через удалённые tools.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import duckdb
import pytest

from lib.services.cache_provider import SearchResult
from lib.utils.sql_safety import validate_sql


try:
    from conftest import AUDIT_SKILL_DIR as SKILL_DIR
except ImportError:
    SKILL_DIR = Path(__file__).resolve().parent.parent


class _DBService:
    """Generic provider-адаптер к ``CacheProvider.query_sql`` (dict-rows)."""

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


class _StubProvider:
    """Generic provider-адаптер к ``CacheProvider.search_vector``."""

    def __init__(self, hits_by_index: dict[str, list[SearchResult]] | None = None) -> None:
        self._hits = hits_by_index or {}

    def search_vector(
        self, query, index_name, top_k=5, threshold=None
    ) -> list[SearchResult]:
        return list(self._hits.get(index_name, []))


def _make_audit_db() -> _DBService:
    """Создать in-memory DuckDB со схемой ``oarb``, обёрнутой в ``_DBService``.

    Эквивалент production-path: ``CacheProvider.query_sql`` против
    DuckDB-PG снимка. ``_DBService`` воспроизводит контракт
    ``{status, row_count, columns, rows}``.
    """
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SCHEMA IF NOT EXISTS oarb")
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
    return _DBService(conn)


# ---------------------------------------------------------------------------
# Predefined: skill-owned registry доступен через predefined.run(), НЕ через PG
# ---------------------------------------------------------------------------


class TestAuditAnalyerPredefinedScripts:
    """Этап 24/Phase 7: predefined — DB-source (public.agent_predefined_scripts).

    Канонический источник SQL — таблица ``public.agent_predefined_scripts``
    в PostgreSQL, читается через DuckDB-PG-снимок в ``predefined.run()``
    (см. ``scripts/predefined/db_loader.py`` и
    ``sql/audit_analyzer/seed_predefined_scripts.sql``).

    Python ``REGISTRY`` удалён; ``predefined.run()`` теперь читает только
    из DB. Agent работает через generic ``CacheProvider`` (см. SKILL.md).
    """

    def test_predefined_registry_is_db_source(self, db_service) -> None:
        """Реестр predefined — DB-таблица, не Python dict.

        Тест читает из DuckDB-PG снимка через preload-фикстуру из
        ``conftest.py``: ``public.agent_predefined_scripts`` уже засеян.
        """
        from workspace.skills.audit_analyzer.scripts.predefined import (
            load_all,
            run,
        )

        scripts = load_all(db_service, "public.agent_predefined_scripts")
        assert "audit_status_summary" in scripts
        assert "violations_by_period" in scripts
        assert "top_violations_by_type" in scripts

        # Скрипт с обязательными date-параметрами валидируется через run().
        result = run(
            "violations_by_period",
            db_service,
            {"date_from": "2024-01-01", "date_to": "2024-12-31"},
            predefined_table="public.agent_predefined_scripts",
        )
        assert result["status"] == "success"

    def test_predefined_execute_after_lookup(self, db_service) -> None:
        """Agent: выбор predefined → run() выполняет SQL через generic provider."""
        from workspace.skills.audit_analyzer.scripts.predefined import run

        result = run(
            "audit_status_summary",
            db_service,
            predefined_table="public.agent_predefined_scripts",
        )
        assert result["status"] == "success"
        assert result["data"]["script_name"] == "audit_status_summary"
        statuses = {r["status"] for r in result["data"]["result"]["rows"]}
        assert statuses == {"Завершена", "В работе", "Запланирована"}

    def test_predefined_with_required_params(self, db_service) -> None:
        """Параметризованный predefined: date_from и date_to обязательны."""
        from workspace.skills.audit_analyzer.scripts.predefined import run

        result = run(
            "violations_by_period",
            db_service,
            {"date_from": "2024-01-01", "date_to": "2024-12-31"},
            predefined_table="public.agent_predefined_scripts",
        )
        assert result["status"] == "success"
        codes = {r["violation_code"] for r in result["data"]["result"]["rows"]}
        assert codes == {"D-1", "F-1", "F-2"}

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
    """Шаг 25: search_vector через Core — index_name, score/content/metadata."""

    def test_vector_search_passes_index_name(self) -> None:
        seen: dict[str, Any] = {}

        class _CaptureProvider(_StubProvider):
            def search_vector(self, query, index_name, top_k=5, threshold=None):
                seen["index_name"] = index_name
                seen["query"] = query
                return [
                    SearchResult(content="Fire safety", score=0.92, pk_value=42),
                ]

        provider = _CaptureProvider()
        results = provider.search_vector("пожарная безопасность", index_name="audits_index")
        assert seen["index_name"] == "audits_index"
        assert len(results) == 1
        assert results[0].pk_value == 42
        assert results[0].score == pytest.approx(0.92)

    def test_vector_search_preserves_metadata(self) -> None:
        provider = _StubProvider({
            "audits_index": [
                SearchResult(
                    content="Fire safety check",
                    score=0.91,
                    pk_value=42,
                    table="oarb.audits",
                    row={"id": 42, "title": "Fire safety"},
                ),
            ],
        })
        results = provider.search_vector("fire", index_name="audits_index")
        assert len(results) == 1
        hit = results[0]
        assert hit.score == pytest.approx(0.91)
        assert "Fire" in hit.content
        assert hit.table == "oarb.audits"
        assert hit.row["id"] == 42

    def test_vector_search_multiple_results_not_lost(self) -> None:
        provider = _StubProvider({
            "violations_index": [
                SearchResult(content="Fire exit blocked", score=0.92, pk_value=10),
                SearchResult(content="No extinguisher", score=0.85, pk_value=11),
                SearchResult(content="Expired certificate", score=0.81, pk_value=12),
            ],
        })
        results = provider.search_vector("безопасность", index_name="violations_index")
        assert len(results) == 3
        scores = [r.score for r in results]
        assert scores == [0.92, 0.85, 0.81]

    def test_vector_results_serialize_via_asdict(self) -> None:
        """Контракт поиска сериализуется через ``asdict(SearchResult)`` —
        тот же путь, что в ``predefined/CLI`` (поле ``data.results``)."""
        provider = _StubProvider({
            "audits_index": [
                SearchResult(content="Fire safety", score=0.9, pk_value=42),
            ],
        })
        results = provider.search_vector("fire", index_name="audits_index")
        payload = {"results": [asdict(r) for r in results], "count": len(results)}
        assert payload["count"] == 1
        assert payload["results"][0]["content"] == "Fire safety"
        assert payload["results"][0]["pk_value"] == 42

    def test_semantic_query_not_routed_to_sql(self) -> None:
        """Agent не должен превращать семантический запрос в LIKE-SQL.

        SKILL.md требует: для семантического поиска использовать
        vector capability (логические имена `audits_index` /
        `violations_index` / `audit_reports_index`), не свободный SQL с
        ``LIKE '%...%'``.
        """
        skill_text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        assert "audits_index" in skill_text
        assert "violations_index" in skill_text
        assert "audit_reports_index" in skill_text
        assert "vector" in skill_text.lower()
        assert "семантическ" in skill_text.lower()
        # Решение о маршрутизации семантики — в decision tree, не в LIKE-SQL.
        assert "decision tree" in skill_text.lower() or "Decision tree" in skill_text


# ---------------------------------------------------------------------------
# Свободный SQL: COUNT/GROUP BY/JOIN/empty
# ---------------------------------------------------------------------------


class TestAuditAnalyerFreeSql:
    """Шаг 26: свободный SQL — Agent формирует SELECT через Core ``query_sql``."""

    def test_count_audits(self) -> None:
        db = _make_audit_db()
        sql = "SELECT COUNT(*) AS total FROM oarb.audits"
        payload = db.query_sql(sql)
        assert payload["status"] == "success"
        assert payload["columns"] == ["total"]
        assert payload["rows"] == [{"total": 4}]
        assert payload["row_count"] == 1

    def test_group_by_status(self) -> None:
        db = _make_audit_db()
        sql = (
            "SELECT status, COUNT(*) AS cnt FROM oarb.audits "
            "GROUP BY status ORDER BY status"
        )
        payload = db.query_sql(sql)
        assert payload["status"] == "success"
        rows_by_status = {row["status"]: row["cnt"] for row in payload["rows"]}
        assert rows_by_status == {"В работе": 1, "Завершена": 2, "Запланирована": 1}

    def test_date_filter(self) -> None:
        db = _make_audit_db()
        sql = (
            "SELECT id, title FROM oarb.audits "
            "WHERE actual_date >= ? AND actual_date < ?"
        )
        payload = db.query_sql(sql, ["2025-01-01", "2026-01-01"])
        assert payload["status"] == "success"
        assert payload["row_count"] == 1
        assert payload["rows"][0]["title"] == "School check"

    def test_join_audits_violations(self) -> None:
        db = _make_audit_db()
        sql = (
            "SELECT a.title, v.violation_code "
            "FROM oarb.audits a JOIN oarb.violations v ON v.audit_id = a.id "
            "ORDER BY a.id, v.id"
        )
        payload = db.query_sql(sql)
        assert payload["status"] == "success"
        assert payload["row_count"] == 3
        # Никогда не возвращаем пустой results при ненулевых rows.
        assert payload["rows"]

    def test_empty_result_is_not_error(self) -> None:
        """Пустой результат — нормальный ответ, не ошибка."""
        db = _make_audit_db()
        sql = "SELECT * FROM oarb.audits WHERE actual_date IS NULL AND id = 999"
        payload = db.query_sql(sql)
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
        db = _make_audit_db()
        payload = db.query_sql("SELECT * FROM oarb.nonexistent_table")
        assert payload["status"] == "error"
        assert payload["row_count"] == 0
        assert payload["error"]
        assert "oarb.nonexistent_table" in payload["error"]

    def test_syntax_error_returns_structured_error(self) -> None:
        db = _make_audit_db()
        payload = db.query_sql("SELECT FORM oarb.audits")
        assert payload["status"] == "error"
        assert payload["error"]

    def test_agent_can_recover_after_sql_error(self) -> None:
        """Сценарий Agent loop: первая попытка с ошибкой → вторая успешна."""
        db = _make_audit_db()

        # Попытка 1: неверное имя таблицы.
        bad = db.query_sql("SELECT COUNT(*) FROM oarb.audit_table")
        assert bad["status"] == "error"

        # Agent читает message и исправляет SQL.
        # Попытка 2: правильный SQL.
        good = db.query_sql("SELECT COUNT(*) AS total FROM oarb.audits")
        assert good["status"] == "success"
        assert good["rows"] == [{"total": 4}]


# ---------------------------------------------------------------------------
# Agent работает через Core capability, а не через удалённые tools
# ---------------------------------------------------------------------------


class TestAuditAnalyerSkillToolSet:
    """Этап 18/35: Agent-facing tools удалены — работа через Core capability."""

    def test_skill_md_documents_runtime_boundary(self) -> None:
        """SKILL.md описывает границу runtime — skill не зависит от Agent tools."""
        skill_text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        # Skill работает через Core capability, не через Agent-facing tools.
        assert "Core" in skill_text or "core" in skill_text
        # Удалённые tools не должны быть частью data flow.
        assert "duckdb_query_tool.py" not in skill_text
        assert "vector_search_tool.py" not in skill_text

    def test_skill_md_references_db_source_of_truth(self) -> None:
        """SKILL.md указывает, что canonical source — PostgreSQL."""
        skill_text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        assert "public.agent_predefined_scripts" in skill_text
        assert "PostgreSQL" in skill_text or "postgresql" in skill_text.lower()

    def test_skill_md_documents_three_vector_indexes(self) -> None:
        """SKILL.md каталогизирует все три логических индекса."""
        skill_text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        for index in ("audits_index", "violations_index", "audit_reports_index"):
            assert index in skill_text, (
                f"SKILL.md должен упоминать {index} (логический FAISS-индекс)"
            )

    def test_no_removed_tool_modules_exist(self) -> None:
        """Удалённые tool-модули не должны существовать в ``workspace/tools/``."""
        removed = [
            "workspace/tools/run_predefined_script.py",
            "workspace/tools/nl_sql_generate.py",
            "workspace/tools/column_descriptions.py",
            "workspace/tools/duckdb_query_tool.py",
            "workspace/tools/vector_search_tool.py",
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

    def test_skill_md_is_self_contained(self) -> None:
        """SKILL.md — единственный источник документации по skill'у.

        ``references/`` удалены (Phase 8): весь релевантный контент
        (каталог скриптов, описание индексов, бизнес-глоссарий, SQL guidance)
        перенесён в SKILL.md.

        Техническая schema (колонки/типы ``oarb.*``) **не** прописывается
        вручную — она читается через ``CacheProvider.get_schema()``. Поэтому
        здесь нет asserts на ``oarb.audits`` / ``oarb.violations`` / etc.
        """
        skill_text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        assert len(skill_text) > 2000, "SKILL.md подозрительно мал"
        # Все 6 скриптов каталогизированы.
        for script in (
            "analytics_by_year_month",
            "audit_dynamics",
            "audit_effectiveness",
            "audit_types_stats",
            "top_audited_objects",
            "violations_by_type",
        ):
            assert script in skill_text, f"SKILL.md должен упоминать {script}"
        # Все 3 FAISS-индекса каталогизированы.
        for index in ("audits_index", "violations_index", "audit_reports_index"):
            assert index in skill_text
        # Технической schema (типы колонок, «## Схема домена») быть не должно.
        assert "## Схема домена" not in skill_text, (
            "SKILL.md не должен содержать раздел «## Схема домена» с типами колонок — "
            "schema читается через CacheProvider.get_schema()"
        )

    def test_references_dir_not_required(self) -> None:
        """``references/`` удалён — SKILL.md self-contained (Phase 8)."""
        ref_dir = SKILL_DIR / "references"
        if ref_dir.exists():
            md_files = list(ref_dir.glob("*.md"))
            assert not md_files, (
                f"references/*.md должны быть удалены (Phase 8): {md_files}"
            )

    def test_skill_predefined_module_exists(self) -> None:
        """``workspace/skills/audit_analyzer/scripts/predefined/`` — режим predefined."""
        assert (SKILL_DIR / "scripts" / "predefined" / "mode.py").is_file()
        assert (SKILL_DIR / "scripts" / "predefined" / "builder.py").is_file()
        assert (SKILL_DIR / "scripts" / "predefined" / "validator.py").is_file()
        assert (SKILL_DIR / "scripts" / "predefined" / "models.py").is_file()
        assert not (SKILL_DIR / "scripts" / "predefined" / "scripts.py").exists()
        assert (SKILL_DIR / "scripts" / "predefined" / "db_loader.py").is_file()

    def test_no_sql_generator_helper_exists(self) -> None:
        """Удалённый ``scripts/sql_generator.py`` не должен существовать."""
        assert not (SKILL_DIR / "scripts").exists() or not (
            SKILL_DIR / "scripts" / "sql_generator.py"
        ).exists()


# ---------------------------------------------------------------------------
# Generic Core DuckDB contract (structured rows, не строка)
# ---------------------------------------------------------------------------


class TestGenericDuckdbQueryContract:
    """Шаг 4: Core ``query_sql`` возвращает structured rows, не строку."""

    def test_simple_select_returns_structured_rows(self) -> None:
        conn = duckdb.connect(":memory:")
        conn.execute("CREATE TABLE t (id INTEGER, value VARCHAR)")
        conn.execute("INSERT INTO t VALUES (1, 'test')")
        db = _DBService(conn)
        payload = db.query_sql("SELECT 1 AS id, 'test' AS value")
        assert payload["status"] == "success"
        assert payload["columns"] == ["id", "value"]
        assert payload["rows"] == [{"id": 1, "value": "test"}]
        assert payload["row_count"] == 1

    def test_empty_where_false_returns_empty_rows(self) -> None:
        conn = duckdb.connect(":memory:")
        conn.execute("CREATE TABLE t (x INTEGER)")
        db = _DBService(conn)
        payload = db.query_sql("SELECT 1 AS one WHERE FALSE")
        assert payload["status"] == "success"
        assert payload["columns"] == ["one"]
        assert payload["rows"] == []
        assert payload["row_count"] == 0

    def test_safety_validator_rejects_unsafe_sql(self) -> None:
        assert validate_sql("SELECT 1") is None
        assert validate_sql("INSERT INTO t VALUES (1)") is not None
        assert validate_sql("DROP TABLE t") is not None
        assert validate_sql("DELETE FROM t") is not None