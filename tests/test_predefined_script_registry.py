"""Тесты ``PredefinedScriptRegistry`` — read-only API без execution.

Покрывает:
  * get_by_name (hit / miss)
  * list_all (сортировка, JSONB-нормализация)
  * find (keyword-overlap, top_k, пустые edge cases)
  * fallback на ``label='scripts_registry'``
  * ошибка при отсутствии зарегистрированной таблицы
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from lib.services.predefined_script_registry import (
    PredefinedScript,
    PredefinedScriptRegistry,
)
from lib.services.table_registry import (
    SkillRegistration,
    TableResource,
    table_registry,
)


@pytest.fixture(autouse=True)
def _reset_registry() -> None:
    table_registry.clear()
    yield
    table_registry.clear()


class FakeProvider:
    """Минимальный CacheProvider для тестов."""

    def __init__(self, *, rows: list[dict] | None = None) -> None:
        self._rows = list(rows or [])
        self.query_calls: list[tuple[str, list | None]] = []

    def query_sql(self, sql: str, params: list | None = None) -> dict:
        self.query_calls.append((sql, list(params) if params else None))
        if self._rows:
            return {
                "status": "success",
                "row_count": len(self._rows),
                "columns": [],
                "rows": self._rows,
            }
        return {"status": "success", "row_count": 0, "columns": [], "rows": []}


def _register_scripts_table(name: str = "public.agent_predefined_scripts") -> None:
    table_registry.register(SkillRegistration(
        name="demo",
        resources=(
            TableResource(name="public.audits"),
            TableResource(name=name, label="scripts_registry"),
        ),
    ))


def _script_row(
    name: str = "audit_status_summary",
    description: str = "Сводка по статусам аудитов",
    sql: str = "SELECT status, COUNT(*) AS cnt FROM public.audits GROUP BY status",
    parameters: dict[str, Any] | None = None,
    max_rows: int = 100,
    returns: str = "status, cnt",
    long_description: str = "",
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "sql_template": sql,
        "parameters": json.dumps(parameters) if parameters is not None else "{}",
        "max_rows_default": max_rows,
        "returns": returns,
        "long_description": long_description,
    }


class TestGetByName:
    def test_hit(self) -> None:
        _register_scripts_table()
        provider = FakeProvider(rows=[_script_row()])
        reg = PredefinedScriptRegistry(provider=provider)

        out = reg.get_by_name("audit_status_summary")

        assert isinstance(out, PredefinedScript)
        assert out.name == "audit_status_summary"
        assert out.description == "Сводка по статусам аудитов"
        assert out.max_rows_default == 100
        assert out.parameters == {}
        assert "SELECT status" in out.sql_template

    def test_miss_returns_none(self) -> None:
        _register_scripts_table()
        provider = FakeProvider(rows=[])
        reg = PredefinedScriptRegistry(provider=provider)

        assert reg.get_by_name("missing") is None

    def test_empty_name_returns_none(self) -> None:
        _register_scripts_table()
        provider = FakeProvider(rows=[])
        reg = PredefinedScriptRegistry(provider=provider)

        assert reg.get_by_name("") is None

    def test_uses_parameterised_query(self) -> None:
        _register_scripts_table()
        provider = FakeProvider(rows=[])
        reg = PredefinedScriptRegistry(provider=provider)

        reg.get_by_name("foo")

        assert len(provider.query_calls) == 1
        sql, params = provider.query_calls[0]
        assert "name = ?" in sql
        assert "ORDER BY name" in sql
        assert params == ["foo"]


class TestListAll:
    def test_sorted_by_name(self) -> None:
        _register_scripts_table()
        provider = FakeProvider(rows=[
            _script_row(name="zeta"),
            _script_row(name="alpha"),
            _script_row(name="mu"),
        ])
        reg = PredefinedScriptRegistry(provider=provider)

        out = reg.list_all()

        assert [s.name for s in out] == ["zeta", "alpha", "mu"]
        sql, _ = provider.query_calls[0]
        assert "ORDER BY name" in sql

    def test_normalises_jsonb_parameters(self) -> None:
        _register_scripts_table()
        provider = FakeProvider(rows=[
            _script_row(
                name="violations_by_period",
                parameters={
                    "date_from": {"type": "date", "required": True},
                    "date_to": {"type": "date", "required": True},
                },
            ),
        ])
        reg = PredefinedScriptRegistry(provider=provider)

        out = reg.list_all()

        assert out[0].parameters == {
            "date_from": {"type": "date", "required": True},
            "date_to": {"type": "date", "required": True},
        }
        assert out[0].parameter_names() == ("date_from", "date_to")

    def test_handles_string_jsonb(self) -> None:
        _register_scripts_table()
        row = _script_row()
        row["parameters"] = '{"year": {"type": "integer", "required": false}}'
        provider = FakeProvider(rows=[row])
        reg = PredefinedScriptRegistry(provider=provider)

        out = reg.list_all()

        assert out[0].parameters == {
            "year": {"type": "integer", "required": False},
        }

    def test_handles_corrupt_jsonb(self) -> None:
        _register_scripts_table()
        row = _script_row()
        row["parameters"] = "{not-json"
        provider = FakeProvider(rows=[row])
        reg = PredefinedScriptRegistry(provider=provider)

        out = reg.list_all()

        assert out[0].parameters == {}

    def test_handles_non_dict_parameter_def(self) -> None:
        _register_scripts_table()
        row = _script_row()
        row["parameters"] = json.dumps({"foo": "string-not-dict"})
        provider = FakeProvider(rows=[row])
        reg = PredefinedScriptRegistry(provider=provider)

        out = reg.list_all()

        assert out[0].parameters == {"foo": {"type": "string"}}


class TestFind:
    def test_keyword_overlap(self) -> None:
        _register_scripts_table()
        provider = FakeProvider(rows=[
            _script_row(
                name="audit_status_summary",
                description="Сводка по статусам аудитов",
                long_description="Группировка audits по статусу за период",
            ),
            _script_row(
                name="top_violations_by_type",
                description="Топ нарушений по типу",
            ),
        ])
        reg = PredefinedScriptRegistry(provider=provider)

        out = reg.find("сводка статусов аудитов", top_k=5)

        assert len(out) == 1
        script, score = out[0]
        assert script.name == "audit_status_summary"
        assert score > 0

    def test_top_k_limits_results(self) -> None:
        _register_scripts_table()
        provider = FakeProvider(rows=[
            _script_row(name="s1", description="общая тема"),
            _script_row(name="s2", description="общая тема вторая"),
            _script_row(name="s3", description="общая тема третья"),
        ])
        reg = PredefinedScriptRegistry(provider=provider)

        out = reg.find("общая тема", top_k=2)

        assert len(out) == 2

    def test_empty_query_returns_empty(self) -> None:
        _register_scripts_table()
        provider = FakeProvider(rows=[_script_row()])
        reg = PredefinedScriptRegistry(provider=provider)

        assert reg.find("") == []
        assert reg.find("   ") == []

    def test_no_match_returns_empty(self) -> None:
        _register_scripts_table()
        provider = FakeProvider(rows=[_script_row(description="про пожары")])
        reg = PredefinedScriptRegistry(provider=provider)

        assert reg.find("абсолютно несвязанный запрос xyz") == []

    def test_short_tokens_dropped(self) -> None:
        _register_scripts_table()
        provider = FakeProvider(rows=[_script_row(description="а б в")])
        reg = PredefinedScriptRegistry(provider=provider)

        assert reg.find("а б в") == []

    def test_empty_registry_returns_empty(self) -> None:
        _register_scripts_table()
        provider = FakeProvider(rows=[])
        reg = PredefinedScriptRegistry(provider=provider)

        assert reg.find("любой запрос") == []

    def test_sort_by_score_desc_then_name(self) -> None:
        _register_scripts_table()
        provider = FakeProvider(rows=[
            _script_row(name="zeta_summary", description="сводка отчёт"),
            _script_row(name="alpha_summary", description="сводка отчёт"),
            _script_row(name="beta_other", description="сводка отчёт доп"),
        ])
        reg = PredefinedScriptRegistry(provider=provider)

        out = reg.find("сводка отчёт доп", top_k=5)

        names = [s.name for s, _ in out]
        assert names[0] == "beta_other"
        assert set(names[1:]) == {"alpha_summary", "zeta_summary"}
        scores = {s.name: score for s, score in out}
        assert scores["beta_other"] > scores["alpha_summary"]


class TestMissingRegistry:
    def test_no_label_registered_raises(self) -> None:
        table_registry.register(SkillRegistration(
            name="demo",
            resources=(TableResource(name="public.audits"),),
        ))
        provider = FakeProvider()
        reg = PredefinedScriptRegistry(provider=provider)

        with pytest.raises(RuntimeError, match="scripts_registry"):
            reg.get_by_name("anything")

    def test_query_failure_returns_empty(self) -> None:
        _register_scripts_table()

        class FailingProvider:
            def query_sql(self, sql: str, params: list | None = None) -> dict:
                return {"status": "error", "error": "boom"}

        reg = PredefinedScriptRegistry(provider=FailingProvider())

        assert reg.list_all() == []
        assert reg.get_by_name("anything") is None