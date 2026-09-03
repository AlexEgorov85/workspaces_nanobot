"""Тесты ``ParameterValidator`` — валидация параметров predefined-скриптов."""

from __future__ import annotations

import pytest

from lib.services.predefined_script_validator import (
    ParameterValidationError,
    ParameterValidator,
)


def _v(**defs: dict) -> ParameterValidator:
    return ParameterValidator(parameter_defs=defs, script_name="test_script")


class TestHappyPath:
    def test_required_present(self) -> None:
        v = _v(year={"type": "integer", "required": True})
        out = v.validate({"year": 2024})
        assert out == {"year": 2024}

    def test_default_used_when_missing(self) -> None:
        v = _v(
            year={"type": "integer", "required": False, "default": 2024},
            org={"type": "string", "required": False, "default": "all"},
        )
        out = v.validate({})
        assert out == {"year": 2024, "org": "all"}

    def test_default_overridden_when_provided(self) -> None:
        v = _v(year={"type": "integer", "default": 2024})
        out = v.validate({"year": 2025})
        assert out == {"year": 2025}

    def test_no_parameters(self) -> None:
        v = _v()
        assert v.validate({}) == {}
        assert v.validate(None) == {}
        assert v.declared_names == ()

    def test_string_coercion_from_int(self) -> None:
        v = _v(name={"type": "string"})
        out = v.validate({"name": 123})
        assert out == {"name": "123"}


class TestMissingRequired:
    def test_raises(self) -> None:
        v = _v(year={"type": "integer", "required": True})
        with pytest.raises(ParameterValidationError) as exc:
            v.validate({})
        assert "year" in str(exc.value)
        assert any("обязателен" in e for e in exc.value.errors)

    def test_null_for_required_raises(self) -> None:
        v = _v(year={"type": "integer", "required": True})
        with pytest.raises(ParameterValidationError) as exc:
            v.validate({"year": None})
        assert any("обязателен" in e for e in exc.value.errors)

    def test_script_name_in_error(self) -> None:
        v = _v(year={"type": "integer", "required": True})
        with pytest.raises(ParameterValidationError) as exc:
            v.validate({})
        assert exc.value.script_name == "test_script"
        assert "[test_script]" in str(exc.value)


class TestUnknownParameters:
    def test_single_unknown(self) -> None:
        v = _v(year={"type": "integer"})
        with pytest.raises(ParameterValidationError) as exc:
            v.validate({"year": 2024, "foo": "bar"})
        assert any("foo" in e for e in exc.value.errors)

    def test_multiple_unknowns_listed(self) -> None:
        v = _v(year={"type": "integer"})
        with pytest.raises(ParameterValidationError) as exc:
            v.validate({"year": 2024, "a": 1, "b": 2})
        joined = "; ".join(exc.value.errors)
        assert "a" in joined and "b" in joined


class TestTypeCoercion:
    def test_integer_from_string(self) -> None:
        v = _v(n={"type": "integer"})
        assert v.validate({"n": "42"}) == {"n": 42}

    def test_integer_from_int(self) -> None:
        v = _v(n={"type": "integer"})
        assert v.validate({"n": 42}) == {"n": 42}

    def test_integer_rejects_bool(self) -> None:
        v = _v(n={"type": "integer"})
        with pytest.raises(ParameterValidationError):
            v.validate({"n": True})

    def test_number_accepts_float_string(self) -> None:
        v = _v(x={"type": "number"})
        assert v.validate({"x": "1.5"}) == {"x": 1.5}

    def test_boolean_true_variants(self) -> None:
        v = _v(b={"type": "boolean"})
        for raw in ["true", "TRUE", "yes", "1", "0", "false"]:
            pass
        assert v.validate({"b": "true"}) == {"b": True}
        assert v.validate({"b": "false"}) == {"b": False}
        assert v.validate({"b": 1}) == {"b": True}
        assert v.validate({"b": 0}) == {"b": False}
        assert v.validate({"b": True}) == {"b": True}

    def test_boolean_invalid_string(self) -> None:
        v = _v(b={"type": "boolean"})
        with pytest.raises(ParameterValidationError):
            v.validate({"b": "maybe"})

    def test_date_passthrough_string(self) -> None:
        v = _v(d={"type": "date"})
        assert v.validate({"d": "2024-01-01"}) == {"d": "2024-01-01"}

    def test_date_rejects_int(self) -> None:
        v = _v(d={"type": "date"})
        with pytest.raises(ParameterValidationError):
            v.validate({"d": 20240101})

    def test_unsupported_type_reported(self) -> None:
        v = _v(d={"type": "uuid"})
        with pytest.raises(ParameterValidationError):
            v.validate({"d": "abc"})


class TestValidationRules:
    def test_min_max_pass(self) -> None:
        v = _v(n={"type": "integer", "validation": {"min": 1, "max": 10}})
        assert v.validate({"n": 5}) == {"n": 5}

    def test_min_violation(self) -> None:
        v = _v(n={"type": "integer", "validation": {"min": 1}})
        with pytest.raises(ParameterValidationError) as exc:
            v.validate({"n": 0})
        assert any("min" in e for e in exc.value.errors)

    def test_max_violation(self) -> None:
        v = _v(n={"type": "integer", "validation": {"max": 10}})
        with pytest.raises(ParameterValidationError):
            v.validate({"n": 11})

    def test_min_length_violation(self) -> None:
        v = _v(s={"type": "string", "validation": {"min_length": 3}})
        with pytest.raises(ParameterValidationError) as exc:
            v.validate({"s": "ab"})
        assert any("min_length" in e for e in exc.value.errors)

    def test_max_length_violation(self) -> None:
        v = _v(s={"type": "string", "validation": {"max_length": 3}})
        with pytest.raises(ParameterValidationError):
            v.validate({"s": "abcd"})

    def test_pattern_match(self) -> None:
        v = _v(s={"type": "string", "validation": {"pattern": r"^\d{4}$"}})
        assert v.validate({"s": "1234"}) == {"s": "1234"}

    def test_pattern_mismatch(self) -> None:
        v = _v(s={"type": "string", "validation": {"pattern": r"^\d{4}$"}})
        with pytest.raises(ParameterValidationError):
            v.validate({"s": "abc"})

    def test_pattern_invalid_in_config_reported(self) -> None:
        v = _v(s={"type": "string", "validation": {"pattern": "[unclosed"}})
        with pytest.raises(ParameterValidationError) as exc:
            v.validate({"s": "abc"})
        assert any("pattern" in e for e in exc.value.errors)

    def test_choices_match(self) -> None:
        v = _v(s={"type": "string", "validation": {"choices": ["a", "b"]}})
        assert v.validate({"s": "a"}) == {"s": "a"}

    def test_choices_mismatch(self) -> None:
        v = _v(s={"type": "string", "validation": {"choices": ["a", "b"]}})
        with pytest.raises(ParameterValidationError):
            v.validate({"s": "c"})


class TestAggregateErrors:
    def test_multiple_errors_aggregated(self) -> None:
        v = _v(
            year={"type": "integer", "required": True},
            org={"type": "string"},
        )
        with pytest.raises(ParameterValidationError) as exc:
            v.validate({"foo": "x", "year": "abc"})
        assert len(exc.value.errors) >= 2
        joined = "; ".join(exc.value.errors)
        assert "foo" in joined
        assert "year" in joined