"""Конвейер ``predefined`` (пункт 4.5): поведение агента сохранено.

Тесты-эталоны — ``workspace/skills/audit_analyzer/tests/
test_audit_analyzer_predefined.py`` агента. Формулировки проверок взяты оттуда
(строгие ISO-даты, ``::``-касты не трогаются, двоеточие внутри строки,
``min_violations=0`` сохраняет ``HAVING``, повтор параметра даёт два значения),
разница одна: вместо настоящего DuckDB — подставной читатель снимка, потому
что библиотека не должна иметь реального хранилища.

Плюс проверки пунктов 4.1, 4.13 и 4.14 на этом же конвейере.
"""

from __future__ import annotations

import pytest
from libs.audit import (
    AuditResult,
    AuditValidationError,
    DynamicQueryBuilder,
    ParameterValidator,
    QueryFailedError,
    RegistryUnavailableError,
    ScriptDefinition,
    ScriptNotFoundError,
    list_scripts,
    run_predefined,
)
from test_audit_lib_fakes import (
    REGISTRY_TABLE,
    FakeSnapshot,
    make_registry_row,
    script_row_effectiveness,
    script_row_violations_by_period,
)

ROWS = [script_row_violations_by_period(), script_row_effectiveness()]


@pytest.fixture
def snap() -> FakeSnapshot:
    return FakeSnapshot(ROWS, data_rows=[{"id": 10, "violation_code": "F-1"}])


def run(snap: FakeSnapshot, name: str = "violations_by_period", params=None, **kwargs):
    return run_predefined(name, snap.query, params, scripts_registry_table=REGISTRY_TABLE, **kwargs)


class TestHappyPath:
    def test_returns_result_object(self, snap: FakeSnapshot) -> None:
        result = run(
            snap,
            params={"date_from": "2024-01-01", "date_to": "2024-12-31"},
        )
        assert isinstance(result, AuditResult)
        assert result.mode == "predefined"
        assert result.script_name == "violations_by_period"
        assert result.no_match is False
        assert result.rows == [{"id": 10, "violation_code": "F-1"}]
        assert result.row_count == 1

    def test_parameters_are_positional_not_interpolated(self, snap: FakeSnapshot) -> None:
        run(snap, params={"date_from": "2024-01-01", "date_to": "2024-12-31"})
        text, params = snap.data_calls()[-1]
        assert "2024-01-01" not in text
        assert params == ["2024-01-01", "2024-12-31", 1000]
        assert text.count("?") == 3

    def test_limit_is_appended_from_registry_default(self, snap: FakeSnapshot) -> None:
        result = run(snap, params={"date_from": "2024-01-01", "date_to": "2024-12-31"})
        assert result.sql.endswith("LIMIT ?")
        assert result.row_ceiling == 1000

    def test_payload_is_serialisable(self, snap: FakeSnapshot) -> None:
        result = run(snap, params={"date_from": "2024-01-01", "date_to": "2024-12-31"})
        payload = result.to_payload()
        assert payload["mode"] == "predefined"
        assert payload["rows"] == result.rows
        assert payload["no_match"] is False

    def test_registry_row_limit_wins_over_ceiling(self, snap: FakeSnapshot) -> None:
        result = run(
            snap,
            params={"date_from": "2024-01-01", "date_to": "2024-12-31"},
            row_ceiling=5000,
        )
        assert result.row_ceiling == 1000

    def test_ceiling_below_registry_default_applies(self, snap: FakeSnapshot) -> None:
        result = run(
            snap,
            params={"date_from": "2024-01-01", "date_to": "2024-12-31"},
            row_ceiling=10,
        )
        assert result.row_ceiling == 10
        assert snap.data_calls()[-1][1][-1] == 10

    def test_list_scripts(self, snap: FakeSnapshot) -> None:
        listed = list_scripts(snap.query, scripts_registry_table=REGISTRY_TABLE)
        assert {item["name"] for item in listed} == {
            "violations_by_period",
            "audit_effectiveness_summary",
        }


class TestParameterValidation:
    """Поведение валидатора из агента, включая строгую дату."""

    @pytest.mark.parametrize(
        "good", ["2026-01-01", "2026-12-31", "2000-02-29", "1999-01-01"]
    )
    def test_valid_iso_dates_accepted(self, good: str, snap: FakeSnapshot) -> None:
        result = run(snap, params={"date_from": good, "date_to": "2024-12-31"})
        assert result.parameters["date_from"] == good

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "2026-1-1",
            "2026-1-01",
            "2026-01-1",
            "hello",
            "2026-99-99",
            "2026-13-01",
            "2026-02-30",
            "not-a-date",
            "2026/01/01",
        ],
    )
    def test_invalid_dates_rejected(self, bad: str, snap: FakeSnapshot) -> None:
        with pytest.raises(AuditValidationError) as excinfo:
            run(snap, params={"date_from": bad, "date_to": "2024-12-31"})
        assert "date_from" in excinfo.value.message
        assert excinfo.value.code == "validation_failed"

    def test_non_string_date_rejected(self, snap: FakeSnapshot) -> None:
        with pytest.raises(AuditValidationError) as excinfo:
            run(snap, params={"date_from": 12345, "date_to": "2024-12-31"})
        assert "date_from" in excinfo.value.message

    def test_missing_required_parameter(self, snap: FakeSnapshot) -> None:
        with pytest.raises(AuditValidationError) as excinfo:
            run(snap)
        assert "date_from" in excinfo.value.message

    def test_missing_second_date(self, snap: FakeSnapshot) -> None:
        with pytest.raises(AuditValidationError) as excinfo:
            run(snap, params={"date_from": "2024-01-01"})
        assert "date_to" in excinfo.value.message

    def test_unknown_parameter_is_an_error_not_a_warning(self, snap: FakeSnapshot) -> None:
        with pytest.raises(AuditValidationError) as excinfo:
            run(
                snap,
                params={"date_from": "2024-01-01", "date_to": "2024-12-31", "extra": "x"},
            )
        assert "extra" in excinfo.value.message

    def test_wrong_type_number(self, snap: FakeSnapshot) -> None:
        with pytest.raises(AuditValidationError) as excinfo:
            run(snap, name="audit_effectiveness_summary", params={"min_violations": "много"})
        assert "min_violations" in excinfo.value.message

    def test_bad_parameters_do_not_reach_snapshot(self, snap: FakeSnapshot) -> None:
        with pytest.raises(AuditValidationError):
            run(snap, params={"date_from": "nope", "date_to": "2024-12-31"})
        assert snap.data_calls() == []

    def test_validate_tuple_form_kept(self) -> None:
        """Совместимая форма ``(params, message)`` — как в агенте."""
        from libs.audit import load_script

        script = load_script(FakeSnapshot(ROWS).query, REGISTRY_TABLE, "violations_by_period")
        merged, error = ParameterValidator.validate(script, {})
        assert merged == {}
        assert error is not None
        assert "date_from" in error

        merged, error = ParameterValidator.validate(
            script, {"date_from": "2024-01-01", "date_to": "2024-12-31"}
        )
        assert error is None
        assert merged == {"date_from": "2024-01-01", "date_to": "2024-12-31"}


class TestBuilderBehaviour:
    """Сборка шаблона: регрессии из тестов агента."""

    def test_double_colon_cast_not_replaced(self) -> None:
        script = make_script(
            sql_template=(
                "SELECT id::TEXT AS label, value::INTEGER, :date_from "
                "FROM oarb.audits WHERE actual_date >= :date_from"
            ),
            parameters={"date_from": {"type": "date", "required": True}},
        )
        text, values = DynamicQueryBuilder.build(script, {"date_from": "2024-01-01"})
        assert "id::TEXT" in text
        assert "value::INTEGER" in text
        assert values == ["2024-01-01", "2024-01-01", 1000]
        assert text.count("?") == 3

    def test_colon_inside_string_literal_preserved(self) -> None:
        script = make_script(
            sql_template=(
                "SELECT 'a: b' AS label, :date_from "
                "FROM oarb.audits WHERE actual_date >= :date_from"
            ),
            parameters={"date_from": {"type": "date", "required": True}},
        )
        text, _values = DynamicQueryBuilder.build(script, {"date_from": "2024-01-01"})
        assert "'a: b'" in text

    def test_min_violations_zero_keeps_having(self, snap: FakeSnapshot) -> None:
        result = run(snap, name="audit_effectiveness_summary", params={"min_violations": 0})
        assert "HAVING COUNT(v.id) >= ?" in result.sql
        assert 0 in snap.data_calls()[-1][1]

    def test_min_violations_absent_drops_having(self, snap: FakeSnapshot) -> None:
        result = run(snap, name="audit_effectiveness_summary", params={})
        assert "HAVING" not in result.sql
        assert 0 not in snap.data_calls()[-1][1]

    def test_min_violations_one(self, snap: FakeSnapshot) -> None:
        result = run(snap, name="audit_effectiveness_summary", params={"min_violations": 1})
        assert "HAVING COUNT(v.id) >= ?" in result.sql
        assert 1 in snap.data_calls()[-1][1]

    def test_template_with_max_rows_always_gets_value(self) -> None:
        """Раньше лишний ``?`` оставался без значения — запрос уходил битым."""
        script = make_script(sql_template="SELECT * FROM oarb.audits LIMIT :max_rows")
        text, values = DynamicQueryBuilder.build(script, {})
        assert text.count("?") == len(values) == 1
        assert values == [1000]

    def test_limit_parameter_overrides_registry_default(self) -> None:
        script = make_script(
            sql_template="SELECT * FROM oarb.audits LIMIT :max_rows",
            parameters={"top": {"type": "limit"}},
        )
        text, values = DynamicQueryBuilder.build(script, {"top": 25})
        assert values == [25]
        assert text.endswith("LIMIT ?")

    def test_like_parameter_wrapped(self) -> None:
        script = make_script(
            sql_template="SELECT * FROM oarb.audits WHERE title LIKE :title",
            parameters={"title": {"type": "like"}},
        )
        _text, values = DynamicQueryBuilder.build(script, {"title": "пожар"})
        assert values == ["%пожар%", 1000]

    def test_missing_required_param_at_build_time(self) -> None:
        script = make_script(parameters={"p": {"type": "number", "required": True}})
        with pytest.raises(AuditValidationError):
            DynamicQueryBuilder.build(script, {})

    def test_non_positive_registry_ceiling_rejected(self) -> None:
        """Проверка сборщика для скрипта, собранного вручную.

        Через загрузчик сюда не дойти: ``max_rows_default <= 0`` отбрасывается
        как битая строка реестра. Но сборщик — публичная функция, и защита
        должна быть в нём самом.
        """
        from libs.audit.models import ScriptDefinition

        script = ScriptDefinition(
            name="broken", description="", sql_template="SELECT * FROM oarb.audits",
            max_rows_default=0,
        )
        with pytest.raises(AuditValidationError):
            DynamicQueryBuilder.build(script, {})


class TestPipelineErrors:
    """Пункт 4.14: у каждого отказа есть код, а не «режим ошибки»."""

    def test_unknown_script(self, snap: FakeSnapshot) -> None:
        with pytest.raises(ScriptNotFoundError) as excinfo:
            run(snap, name="nonexistent", params={})
        assert excinfo.value.code == "not_found"

    def test_empty_script_name(self, snap: FakeSnapshot) -> None:
        for bad in ("", "   ", None, 5):
            with pytest.raises(AuditValidationError):
                run(snap, name=bad)  # type: ignore[arg-type]

    def test_registry_unavailable_propagates(self) -> None:
        broken = FakeSnapshot(registry_error=RuntimeError("snapshot offline"))
        with pytest.raises(RegistryUnavailableError) as excinfo:
            run(broken, params={"date_from": "2024-01-01", "date_to": "2024-12-31"})
        assert excinfo.value.code == "registry_unavailable"

    def test_query_error_status_becomes_query_failed(self) -> None:
        failing = FakeSnapshot(
            ROWS, data_result={"status": "error", "rows": [], "error": "binder error"}
        )
        with pytest.raises(QueryFailedError) as excinfo:
            run(failing, params={"date_from": "2024-01-01", "date_to": "2024-12-31"})
        assert "binder error" in excinfo.value.message
        assert excinfo.value.code == "query_failed"

    def test_reader_exception_becomes_query_failed(self) -> None:
        registry = FakeSnapshot(ROWS)

        def boom(text, params=None):
            if "agent_predefined_scripts" in text:
                return registry.query(text, params)
            raise RuntimeError("connection lost")

        with pytest.raises(QueryFailedError) as excinfo:
            run_predefined(
                "violations_by_period",
                boom,
                {"date_from": "2024-01-01", "date_to": "2024-12-31"},
                scripts_registry_table=REGISTRY_TABLE,
            )
        assert "connection lost" in excinfo.value.message

    def test_non_dict_query_result(self) -> None:
        weird = FakeSnapshot(ROWS, data_result="не словарь")
        with pytest.raises(QueryFailedError):
            run(weird, params={"date_from": "2024-01-01", "date_to": "2024-12-31"})

    def test_bad_row_ceiling_type(self, snap: FakeSnapshot) -> None:
        with pytest.raises(AuditValidationError):
            run(
                snap,
                params={"date_from": "2024-01-01", "date_to": "2024-12-31"},
                row_ceiling="много",  # type: ignore[arg-type]
            )


def make_script(
    *,
    sql_template: str = "SELECT * FROM oarb.audits",
    parameters: dict | None = None,
    max_rows_default: int = 1000,
) -> ScriptDefinition:
    """Собрать ``ScriptDefinition`` из строки реестра (через настоящий загрузчик)."""
    from libs.audit import load_script

    row = make_registry_row(
        "script_a", sql_template=sql_template, parameters=parameters or {},
        max_rows_default=max_rows_default,
    )
    return load_script(FakeSnapshot([row]).query, REGISTRY_TABLE, "script_a")
