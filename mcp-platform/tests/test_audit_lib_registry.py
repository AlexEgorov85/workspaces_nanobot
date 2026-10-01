"""Чтение реестра скриптов: пункт 4.1 (имя — параметр), 4.2 (чтение снимком), 4.13 (без молчания).

Что здесь защищается. Реестр — единственный источник определений скриптов,
и в агенте у него было два молчаливых пути: ``load_all`` возвращал ``{}``
при любой ошибке, а ``load_script`` — ``None``. Пустой реестр и
недоступный реестр выглядели одинаково, а «почти весь реестр» (битые
строки пропускались по одной) не отличался от полного. Каждый из этих
путей теперь отдельный тест на заведомо плохой фикстуре.
"""

from __future__ import annotations

import json

import pytest
from libs.audit import (
    AuditValidationError,
    RegistryCorruptError,
    RegistryUnavailableError,
    ScriptNotFoundError,
    load_all,
    load_script,
)
from test_audit_lib_fakes import (
    REGISTRY_TABLE,
    FakeSnapshot,
    make_registry_row,
    script_row_effectiveness,
    script_row_violations_by_period,
)

VALID_ROWS = [script_row_violations_by_period(), script_row_effectiveness()]


class TestRegistryTableIsAnArgument:
    """4.1: имя таблицы приходит параметром, а не из конфига агента."""

    def test_table_name_comes_from_argument(self) -> None:
        snap = FakeSnapshot(VALID_ROWS, registry_match="other_registry")
        scripts = load_all(snap.query, "public.some_other_registry")
        assert set(scripts) == {"violations_by_period", "audit_effectiveness_summary"}
        assert '"public"."some_other_registry"' in snap.last_text
        assert "agent_predefined_scripts" not in snap.last_text

    def test_missing_table_argument_is_rejected(self) -> None:
        snap = FakeSnapshot(VALID_ROWS)
        for bad in ("", "   ", None, 42):
            with pytest.raises(AuditValidationError) as excinfo:
                load_all(snap.query, bad)  # type: ignore[arg-type]
            assert excinfo.value.code == "validation_failed"
        assert snap.calls == [], "снимок не должен трогаться без корректного имени"

    @pytest.mark.parametrize(
        "bad",
        [
            "public.agent_predefined_scripts; DROP TABLE x",
            'public."agent_predefined_scripts"',
            "a.b.c",
            "public.имя",
            "public..table",
        ],
    )
    def test_injection_looking_table_name_rejected(self, bad: str) -> None:
        """Имя приходит от вызывающей стороны, но всё равно проверяется."""
        snap = FakeSnapshot(VALID_ROWS)
        with pytest.raises(AuditValidationError):
            load_all(snap.query, bad)
        assert snap.calls == []

    def test_table_without_schema_uses_main(self) -> None:
        """Пункт 4.4: без схемы — ``main``, а не ``public``."""
        snap = FakeSnapshot(VALID_ROWS)
        load_all(snap.query, "agent_predefined_scripts")
        assert '"main"."agent_predefined_scripts"' in snap.last_text

    def test_explicit_schema_preserved(self) -> None:
        snap = FakeSnapshot(VALID_ROWS)
        load_all(snap.query, "oarb.scripts")
        assert '"oarb"."scripts"' in snap.last_text


class TestRegistryReadThroughSnapshotReader:
    """4.2: чтение идёт колбэком, плейсхолдеры — ``?`` (диалект снимка)."""

    def test_reads_via_injected_callable(self) -> None:
        snap = FakeSnapshot(VALID_ROWS)
        load_all(snap.query, REGISTRY_TABLE)
        assert len(snap.calls) == 1
        text, params = snap.calls[0]
        assert "agent_predefined_scripts" in text
        assert params is None or params == []

    def test_script_name_is_positional_parameter_not_text(self) -> None:
        """Имя скрипта — параметр, а не кусок текста запроса (пункт 4.2)."""
        snap = FakeSnapshot(VALID_ROWS)
        load_script(snap.query, REGISTRY_TABLE, "violations_by_period")
        text, params = snap.last_text, snap.last_params
        assert "WHERE name = ?" in text
        assert params == ["violations_by_period"]
        assert "violations_by_period" not in text

    def test_hostile_script_name_does_not_reach_query_text(self) -> None:
        snap = FakeSnapshot(VALID_ROWS)
        nasty = "x'; DROP TABLE oarb.audits; --"
        with pytest.raises(ScriptNotFoundError):
            load_script(snap.query, REGISTRY_TABLE, nasty)
        text, params = snap.last_text, snap.last_params
        assert nasty not in text
        assert params == [nasty]
        assert "DROP TABLE" not in text

    def test_question_mark_placeholders_kept(self) -> None:
        """Планом 4.3 смена ``?`` на ``%s`` отменена — это диалект снимка."""
        snap = FakeSnapshot(VALID_ROWS)
        load_script(snap.query, REGISTRY_TABLE, "violations_by_period")
        assert "%s" not in snap.last_text
        assert snap.last_text.count("?") == 1

    def test_result_rows_become_script_definitions(self) -> None:
        snap = FakeSnapshot(VALID_ROWS)
        script = load_script(snap.query, REGISTRY_TABLE, "violations_by_period")
        assert script.name == "violations_by_period"
        assert script.max_rows_default == 1000
        assert set(script.parameters) == {"date_from", "date_to"}
        assert script.parameters["date_from"].type == "date"
        assert script.parameters["date_from"].required is True

    def test_null_parameter_entries_are_dropped(self) -> None:
        """В реестре ``null`` означает «скрипт этот параметр не использует»."""
        row = make_registry_row(parameters={"used": {"type": "number"}, "unused": None})
        snap = FakeSnapshot([row])
        script = load_script(snap.query, REGISTRY_TABLE, "script_a")
        assert set(script.parameters) == {"used"}


class TestNoSilentRegistryPaths:
    """4.13: нечитаемый реестр — ошибка, а не пустой результат."""

    def test_exception_becomes_registry_unavailable(self) -> None:
        snap = FakeSnapshot(registry_error=RuntimeError("snapshot offline"))
        with pytest.raises(RegistryUnavailableError) as excinfo:
            load_all(snap.query, REGISTRY_TABLE)
        assert excinfo.value.code == "registry_unavailable"
        assert "snapshot offline" in excinfo.value.message

    def test_error_status_becomes_registry_unavailable(self) -> None:
        snap = FakeSnapshot(
            registry_result={"status": "error", "rows": [], "error": "catalog error"}
        )
        with pytest.raises(RegistryUnavailableError) as excinfo:
            load_all(snap.query, REGISTRY_TABLE)
        assert "catalog error" in excinfo.value.message

    def test_non_dict_result_becomes_registry_unavailable(self) -> None:
        snap = FakeSnapshot(registry_result=["не словарь"])
        with pytest.raises(RegistryUnavailableError):
            load_all(snap.query, REGISTRY_TABLE)

    def test_rows_of_wrong_type_becomes_registry_unavailable(self) -> None:
        snap = FakeSnapshot(
            registry_result={"status": "success", "row_count": 0, "rows": "не список"}
        )
        with pytest.raises(RegistryUnavailableError):
            load_all(snap.query, REGISTRY_TABLE)

    def test_empty_registry_is_data_not_error(self) -> None:
        """Ноль строк — это пустой реестр, а не сбой."""
        snap = FakeSnapshot([])
        assert load_all(snap.query, REGISTRY_TABLE) == {}

    def test_load_script_raises_instead_of_returning_none(self) -> None:
        """В агенте здесь возвращался ``None`` (пункт 4.13)."""
        snap = FakeSnapshot(registry_error=RuntimeError("snapshot offline"))
        with pytest.raises(RegistryUnavailableError):
            load_script(snap.query, REGISTRY_TABLE, "violations_by_period")

    def test_missing_script_is_not_registry_unavailable(self) -> None:
        """Отсутствие имени и нечитаемый реестр — разные ошибки."""
        snap = FakeSnapshot(VALID_ROWS)
        with pytest.raises(ScriptNotFoundError) as excinfo:
            load_script(snap.query, REGISTRY_TABLE, "no_such_script")
        assert excinfo.value.code == "not_found"
        assert not isinstance(excinfo.value, RegistryUnavailableError)

    def test_unavailable_and_absent_do_not_collapse(self) -> None:
        broken = FakeSnapshot(registry_error=RuntimeError("offline"))
        empty = FakeSnapshot([])
        with pytest.raises(RegistryUnavailableError):
            load_script(broken.query, REGISTRY_TABLE, "x")
        with pytest.raises(ScriptNotFoundError):
            load_script(empty.query, REGISTRY_TABLE, "x")


class TestCorruptRegistryRows:
    """Битый реестр (пункт 4.13): частичный набор молча был хуже отказа."""

    @pytest.mark.parametrize(
        ("label", "row"),
        [
            ("нет имени", {k: v for k, v in make_registry_row().items() if k != "name"}),
            ("пустое имя", make_registry_row(name="")),
            ("имя не строка", make_registry_row(name=17)),
            ("нет sql_template", {k: v for k, v in make_registry_row().items()
                                  if k != "sql_template"}),
            ("пустой sql_template", make_registry_row(sql_template="   ")),
            ("параметры не JSON", make_registry_row(parameters="{не json")),
            ("параметры не объект", make_registry_row(parameters=json.dumps([1, 2]))),
            (
                "определение параметра не объект",
                make_registry_row(parameters=json.dumps({"p": "строка"})),
            ),
            (
                "неизвестный тип параметра",
                make_registry_row(parameters=json.dumps({"p": {"type": "хижина"}})),
            ),
            ("max_rows не число", make_registry_row(max_rows_default="много")),
            ("max_rows ноль", make_registry_row(max_rows_default=0)),
            ("max_rows отрицательный", make_registry_row(max_rows_default=-5)),
        ],
    )
    def test_corrupt_row_is_an_error(self, label: str, row: dict) -> None:
        """Битая строка поднимает ``registry_corrupt`` в обоих входах.

        Проверяется ``load_all``: строка без имени или с нестроковым именем
        не может быть найдена и по имени, поэтому для неё честный путь —
        чтение всего реестра.
        """
        snap = FakeSnapshot([row])
        with pytest.raises(RegistryCorruptError) as excinfo:
            load_all(snap.query, REGISTRY_TABLE)
        assert excinfo.value.code == "registry_corrupt", label

    @pytest.mark.parametrize(
        "row",
        [
            make_registry_row(name="script_a", sql_template=""),
            make_registry_row(name="script_a", parameters="{не json"),
            make_registry_row(name="script_a", max_rows_default="много"),
        ],
    )
    def test_corrupt_named_row_breaks_load_script(self, row: dict) -> None:
        """Та же битая строка, но найденная по имени."""
        snap = FakeSnapshot([row])
        with pytest.raises(RegistryCorruptError):
            load_script(snap.query, REGISTRY_TABLE, "script_a")

    def test_corrupt_row_breaks_load_all_too(self) -> None:
        """Раньше битая строка пропускалась, и вызывающий получал «почти всё»."""
        snap = FakeSnapshot(VALID_ROWS + [make_registry_row(name="broken", max_rows_default=0)])
        with pytest.raises(RegistryCorruptError):
            load_all(snap.query, REGISTRY_TABLE)

    def test_corrupt_row_is_not_confused_with_unavailable(self) -> None:
        snap = FakeSnapshot([make_registry_row(max_rows_default="много")])
        with pytest.raises(RegistryCorruptError) as excinfo:
            load_all(snap.query, REGISTRY_TABLE)
        assert not isinstance(excinfo.value, RegistryUnavailableError)

    def test_empty_parameters_object_is_valid(self) -> None:
        snap = FakeSnapshot([make_registry_row(parameters={})])
        script = load_script(snap.query, REGISTRY_TABLE, "script_a")
        assert script.parameters == {}

    def test_null_parameters_is_valid(self) -> None:
        row = make_registry_row()
        row["parameters"] = None
        snap = FakeSnapshot([row])
        assert load_script(snap.query, REGISTRY_TABLE, "script_a").parameters == {}


class TestRegistryApiSurface:
    def test_load_all_is_ordered_and_keyed_by_name(self) -> None:
        snap = FakeSnapshot(VALID_ROWS)
        scripts = load_all(snap.query, REGISTRY_TABLE)
        assert list(scripts) == ["audit_effectiveness_summary", "violations_by_period"]
        assert "ORDER BY name" in snap.last_text

    def test_script_name_must_be_non_empty_string(self) -> None:
        snap = FakeSnapshot(VALID_ROWS)
        for bad in ("", "  ", None, 5, ["x"]):
            with pytest.raises(AuditValidationError):
                load_script(snap.query, REGISTRY_TABLE, bad)  # type: ignore[arg-type]
        assert snap.calls == []

    def test_every_error_carries_machine_readable_code(self) -> None:
        """Пункт 4.14: у каждого отказа есть код, а не только текст."""
        cases = [
            (AuditValidationError, FakeSnapshot([]), "", ""),
            (ScriptNotFoundError, FakeSnapshot(VALID_ROWS), REGISTRY_TABLE, "no_such"),
            (
                RegistryUnavailableError,
                FakeSnapshot(registry_error=RuntimeError("x")),
                REGISTRY_TABLE,
                "",
            ),
            (
                RegistryCorruptError,
                FakeSnapshot([make_registry_row(max_rows_default=0)]),
                REGISTRY_TABLE,
                "",
            ),
        ]
        for expected, snap, table, name in cases:
            with pytest.raises(expected) as excinfo:
                if name:
                    load_script(snap.query, table, name)
                else:
                    load_all(snap.query, table)
            assert excinfo.value.code
            assert excinfo.value.message
