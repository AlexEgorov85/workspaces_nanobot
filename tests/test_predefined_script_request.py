"""Тесты ``PredefinedScriptRequestBuilder``."""

from __future__ import annotations

import pytest

from lib.services.predefined_script_registry import PredefinedScript
from lib.services.predefined_script_request import (
    PredefinedScriptRequest,
    PredefinedScriptRequestBuilder,
)
from lib.services.predefined_script_validator import ParameterValidationError


def _script(
    name: str = "demo",
    sql: str = "SELECT * FROM audits WHERE year = ?",
    parameters: dict | None = None,
    max_rows_default: int = 100,
) -> PredefinedScript:
    if parameters is None:
        parameters = {"year": {"type": "integer", "required": True}}
    return PredefinedScript(
        name=name,
        description="",
        sql_template=sql,
        parameters=parameters,
        max_rows_default=max_rows_default,
        returns="",
        long_description="",
        raw={},
    )


class TestHappyPath:
    def test_returns_request(self) -> None:
        builder = PredefinedScriptRequestBuilder(script=_script())
        req = builder.build({"year": 2024})

        assert isinstance(req, PredefinedScriptRequest)
        assert req.name == "demo"
        assert req.sql == "SELECT * FROM audits WHERE year = ? LIMIT ?"
        assert req.params == (2024, 100)
        assert req.max_rows == 100

    def test_uses_overridden_max_rows(self) -> None:
        builder = PredefinedScriptRequestBuilder(
            script=_script(max_rows_default=100), max_rows=10,
        )
        req = builder.build({"year": 2024})
        assert req.max_rows == 10
        assert req.sql == "SELECT * FROM audits WHERE year = ? LIMIT ?"
        assert req.params == (2024, 10)

    def test_default_max_rows_when_none(self) -> None:
        builder = PredefinedScriptRequestBuilder(
            script=_script(max_rows_default=50), max_rows=None,
        )
        req = builder.build({"year": 2024})
        assert req.max_rows == 50
        assert req.params == (2024, 50)

    def test_zero_max_rows_when_unset(self) -> None:
        builder = PredefinedScriptRequestBuilder(
            script=_script(max_rows_default=0), max_rows=None,
        )
        req = builder.build({"year": 2024})
        assert req.max_rows == 0
        assert req.sql == "SELECT * FROM audits WHERE year = ?"
        assert req.params == (2024,)

    def test_default_used_in_values(self) -> None:
        script = _script(parameters={"year": {"type": "integer", "default": 2024}})
        builder = PredefinedScriptRequestBuilder(script=script)
        req = builder.build({})
        assert req.params == (2024, 100)
        assert req.max_rows == 100

    def test_to_dict(self) -> None:
        builder = PredefinedScriptRequestBuilder(script=_script())
        req = builder.build({"year": 2024})
        d = req.to_dict()
        assert d["name"] == "demo"
        assert d["params"] == [2024, 100]


class TestLimitInjection:
    """Аудит 2: execution-level ``LIMIT``, не только Python truncate."""

    def test_limit_added_when_missing(self) -> None:
        builder = PredefinedScriptRequestBuilder(script=_script())
        req = builder.build({"year": 2024})
        assert "LIMIT ?" in req.sql
        assert req.params == (2024, 100)

    def test_limit_not_duplicated_when_present_literal(self) -> None:
        script = _script(
            sql="SELECT * FROM audits WHERE year = ? LIMIT 5",
            max_rows_default=100,
        )
        builder = PredefinedScriptRequestBuilder(script=script)
        req = builder.build({"year": 2024})
        assert req.sql == "SELECT * FROM audits WHERE year = ? LIMIT 5"
        assert req.params == (2024,)

    def test_limit_not_duplicated_when_present_placeholder(self) -> None:
        script = _script(
            sql="SELECT * FROM audits WHERE year = ? LIMIT ?",
            max_rows_default=100,
            parameters={
                "year": {"type": "integer", "required": True},
                "limit_n": {"type": "integer", "required": True},
            },
        )
        builder = PredefinedScriptRequestBuilder(script=script)
        req = builder.build({"year": 2024, "limit_n": 5})
        assert req.sql == "SELECT * FROM audits WHERE year = ? LIMIT ?"
        assert req.params == (2024, 5)

    def test_limit_not_added_when_max_rows_zero(self) -> None:
        script = _script(
            sql="SELECT * FROM audits",
            parameters={},
            max_rows_default=0,
        )
        builder = PredefinedScriptRequestBuilder(script=script)
        req = builder.build({})
        assert "LIMIT" not in req.sql
        assert req.params == ()

    def test_limit_case_insensitive_detection(self) -> None:
        script = _script(
            sql="select * from audits where year = ? limit 5",
            max_rows_default=100,
        )
        builder = PredefinedScriptRequestBuilder(script=script)
        req = builder.build({"year": 2024})
        assert req.sql == "select * from audits where year = ? limit 5"
        assert req.params == (2024,)


class TestParameterOrder:
    def test_params_in_declared_order(self) -> None:
        script = _script(
            sql="SELECT * FROM audits WHERE year = ? AND org = ?",
            parameters={
                "year": {"type": "integer", "required": True},
                "org": {"type": "string", "required": True},
            },
        )
        builder = PredefinedScriptRequestBuilder(script=script)
        req = builder.build({"org": "o1", "year": 2024})
        assert req.params == (2024, "o1", 100)


class TestValidationPropagation:
    def test_missing_required_raises(self) -> None:
        builder = PredefinedScriptRequestBuilder(script=_script())
        with pytest.raises(ParameterValidationError):
            builder.build({})

    def test_unknown_param_raises(self) -> None:
        builder = PredefinedScriptRequestBuilder(script=_script())
        with pytest.raises(ParameterValidationError) as exc:
            builder.build({"year": 2024, "foo": "bar"})
        assert any("foo" in e for e in exc.value.errors)


class TestPlaceholderConsistency:
    def test_too_many_placeholders(self) -> None:
        script = _script(
            sql="SELECT * FROM audits WHERE year = ? AND foo = ?",
            parameters={"year": {"type": "integer", "required": True}},
        )
        builder = PredefinedScriptRequestBuilder(script=script)
        with pytest.raises(ValueError, match="плейсхолдеров"):
            builder.build({"year": 2024})

    def test_too_few_placeholders(self) -> None:
        script = _script(
            sql="SELECT * FROM audits",
            parameters={"year": {"type": "integer", "required": True}},
        )
        builder = PredefinedScriptRequestBuilder(script=script)
        with pytest.raises(ValueError, match="плейсхолдеров"):
            builder.build({"year": 2024})


class TestSqlSafety:
    def test_ddl_rejected(self) -> None:
        script = _script(sql="DROP TABLE audits WHERE year = ?")
        builder = PredefinedScriptRequestBuilder(script=script)
        with pytest.raises(ValueError, match="безопасность"):
            builder.build({"year": 2024})

    def test_insert_rejected(self) -> None:
        script = _script(
            sql="INSERT INTO audits (year) VALUES (?)",
            parameters={"year": {"type": "integer", "required": True}},
        )
        builder = PredefinedScriptRequestBuilder(script=script)
        with pytest.raises(ValueError, match="безопасность"):
            builder.build({"year": 2024})

    def test_multi_statement_rejected(self) -> None:
        script = _script(
            sql="SELECT ? FROM audits; DROP TABLE audits",
            parameters={"year": {"type": "integer", "required": True}},
        )
        builder = PredefinedScriptRequestBuilder(script=script)
        with pytest.raises(ValueError, match="безопасность"):
            builder.build({"year": 2024})


class TestEmptyTemplate:
    def test_empty_sql_raises(self) -> None:
        script = _script(sql="")
        with pytest.raises(ValueError, match="sql_template"):
            PredefinedScriptRequestBuilder(script=script)


class TestNoParameters:
    def test_zero_param_script(self) -> None:
        script = _script(
            sql="SELECT COUNT(*) FROM audits",
            parameters={},
            max_rows_default=0,
        )
        builder = PredefinedScriptRequestBuilder(script=script)
        req = builder.build({})
        assert req.params == ()
        assert req.sql == "SELECT COUNT(*) FROM audits"