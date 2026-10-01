"""Конвейер ``generated_sql``: пункты 4.6 (конвейер), 4.9 (нет ``context``), 4.13, 4.14.

Тесты-эталоны — ``workspace/skills/audit_analyzer/tests/
test_audit_analyzer_edge_cases.py`` (ветки ``<NO_MATCH>``, повторы попыток) и
``test_audit_analyzer_predefined.py``/``test_audit_analyzer_behavior.py``.
Различия: вместо настоящего LLM — подставной ``llm``, вместо DuckDB —
подставной читатель, а вместо словаря с ``status``/``data`` — ``AuditResult``
или исключение с кодом.

Отдельно проверяется пункт 4.9: у ``llm``-колбэка нет параметра ``context``,
и ни одна функция библиотеки не может подклеить чужой текст в сообщения
генератору.
"""

from __future__ import annotations

import inspect

import pytest
from libs.audit import (
    MAX_ATTEMPTS,
    AuditResult,
    AuditValidationError,
    GenerationFailedError,
    QueryFailedError,
    RegistryUnavailableError,
    is_no_match,
    run_generated_sql,
    sanitize_sql_response,
    select_few_shot,
)
from test_audit_lib_fakes import (
    REGISTRY_TABLE,
    FakeSnapshot,
    script_row_violations_by_period,
)

ALLOWED = ("oarb.audits", "oarb.violations")


@pytest.fixture
def snap() -> FakeSnapshot:
    return FakeSnapshot(
        [script_row_violations_by_period()], data_rows=[{"id": 1}, {"id": 2}]
    )


def call(snap: FakeSnapshot, llm, *, query: str = "сколько аудитов", **kwargs):
    params = {
        "llm": llm,
        "explain": snap.explain,
        "read_schema": snap.schema,
        "allowed_tables": ALLOWED,
        "scripts_registry_table": REGISTRY_TABLE,
    }
    params.update(kwargs)
    return run_generated_sql(query, snap.query, **params)


def scripted(answers: list[str]):
    """Модель, которая по очереди отдаёт заготовленные ответы."""
    remaining = list(answers)

    def llm(messages):
        return remaining.pop(0) if remaining else answers[-1]

    return llm


class TestSuccess:
    def test_executes_and_returns_result(self, snap: FakeSnapshot) -> None:
        result = call(snap, lambda messages: "SELECT id FROM oarb.audits")
        assert isinstance(result, AuditResult)
        assert result.mode == "generated_sql"
        assert result.rows == [{"id": 1}, {"id": 2}]
        assert result.row_count == 2
        assert result.no_match is False

    def test_row_ceiling_applied_even_when_model_forgets_limit(
        self, snap: FakeSnapshot
    ) -> None:
        result = call(snap, lambda messages: "SELECT id FROM oarb.audits")
        assert "LIMIT 1000" in result.sql
        assert result.row_ceiling == 1000

    def test_row_ceiling_clamps_huge_limit(self, snap: FakeSnapshot) -> None:
        result = call(
            snap, lambda messages: "SELECT id FROM oarb.audits LIMIT 1000000"
        )
        assert result.sql.endswith("LIMIT 1000")

    def test_custom_ceiling(self, snap: FakeSnapshot) -> None:
        result = call(
            snap, lambda messages: "SELECT id FROM oarb.audits", row_ceiling=5
        )
        assert result.sql.endswith("LIMIT 5")
        assert result.row_ceiling == 5

    def test_smaller_model_limit_preserved(self, snap: FakeSnapshot) -> None:
        result = call(snap, lambda messages: "SELECT id FROM oarb.audits LIMIT 3")
        assert result.sql.endswith("LIMIT 3")

    def test_markdown_block_unwrapped(self, snap: FakeSnapshot) -> None:
        result = call(
            snap,
            lambda messages: "Думаю...\n```sql\nSELECT id FROM oarb.audits\n```",
        )
        assert result.sql.startswith("SELECT id FROM oarb.audits")

    def test_think_block_stripped(self, snap: FakeSnapshot) -> None:
        result = call(
            snap,
            lambda messages: "<think>размышляю</think>\nSELECT id FROM oarb.audits;",
        )
        assert result.sql.startswith("SELECT id FROM oarb.audits")
        assert not result.sql.endswith(";")

    def test_schema_limited_to_whitelist_in_prompt(self, snap: FakeSnapshot) -> None:
        seen: list[list[dict]] = []

        def llm(messages):
            seen.append(messages)
            return "SELECT id FROM oarb.audits"

        call(snap, llm)
        prompt = seen[0][0]["content"] + seen[0][1]["content"]
        assert "oarb.audits" in prompt
        assert "oarb.violations" in prompt
        assert "agent_gateway_logs" not in prompt

    def test_few_shot_comes_from_registry(self, snap: FakeSnapshot) -> None:
        seen: list[list[dict]] = []

        def llm(messages):
            seen.append(messages)
            return "SELECT id FROM oarb.audits"

        call(snap, llm, query="нарушения за период")
        prompt = seen[0][0]["content"]
        assert "Examples from the predefined registry" in prompt
        assert "oarb.violations" in prompt

    def test_prompt_tells_about_whitelist_and_ceiling(self, snap: FakeSnapshot) -> None:
        seen: list[list[dict]] = []

        def llm(messages):
            seen.append(messages)
            return "SELECT id FROM oarb.audits"

        call(snap, llm, row_ceiling=250)
        system = seen[0][0]["content"]
        assert "oarb.audits" in system and "oarb.violations" in system
        assert "<NO_MATCH>" in system
        assert "250" in system


class TestNoMatch:
    """Честный отказ модели — успех с флагом, а не ошибка (как и в агенте)."""

    @pytest.mark.parametrize(
        "raw", ["<NO_MATCH>", "<no_match>", "<No_Match>", "<NO_MATCH>\n", "```\n<NO_MATCH>\n```"]
    )
    def test_recognised(self, raw: str) -> None:
        assert is_no_match(sanitize_sql_response(raw)) is True

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "SELECT 1",
            "Верни <NO_MATCH> если данных нет",
            "<NO_MATCH> и ещё текст",
            "no_match",  # без скобок — это не маркер, а обычный текст
            None,
        ],
    )
    def test_not_markers(self, raw) -> None:
        assert is_no_match(sanitize_sql_response(raw)) is False

    def test_result_is_successful_but_empty(self, snap: FakeSnapshot) -> None:
        result = call(snap, lambda messages: "<NO_MATCH>")
        assert result.no_match is True
        assert result.rows == []
        assert result.sql == ""
        assert result.row_count == 0

    def test_no_further_llm_calls(self, snap: FakeSnapshot) -> None:
        """``<NO_MATCH>`` не должен съедать остальные попытки."""
        counter = {"n": 0}

        def llm(messages):
            counter["n"] += 1
            return "<NO_MATCH>"

        call(snap, llm)
        assert counter["n"] == 1
        assert counter["n"] < MAX_ATTEMPTS


class TestForbiddenTableInPipeline:
    """4.7 в конвейере: запрещённая таблица не доходит до выполнения."""

    BAD = "SELECT * FROM public.agent_gateway_logs"

    def test_never_executed(self, snap: FakeSnapshot) -> None:
        with pytest.raises(GenerationFailedError):
            call(snap, lambda messages: self.BAD)
        assert snap.data_calls() == []
        assert snap.explain_calls == []

    def test_reported_as_forbidden_table(self, snap: FakeSnapshot) -> None:
        with pytest.raises(GenerationFailedError) as excinfo:
            call(snap, lambda messages: self.BAD, max_attempts=2)
        assert excinfo.value.code == "generation_failed"
        assert excinfo.value.last_code == "forbidden_table"
        assert "agent_gateway_logs" in excinfo.value.last_query

    def test_model_is_told_why_and_can_fix_it(self, snap: FakeSnapshot) -> None:
        seen: list[list[dict]] = []

        def llm(messages):
            seen.append(messages)
            if len(seen) == 1:
                return self.BAD
            return "SELECT id FROM oarb.audits"

        result = call(snap, llm)
        assert result.rows == [{"id": 1}, {"id": 2}]
        assert len(seen) == 2
        feedback = seen[1][-1]["content"]
        assert "agent_gateway_logs" in feedback
        assert "whitelist" in feedback

    def test_retries_are_capped(self, snap: FakeSnapshot) -> None:
        counter = {"n": 0}

        def llm(messages):
            counter["n"] += 1
            return self.BAD

        with pytest.raises(GenerationFailedError) as excinfo:
            call(snap, llm, max_attempts=3)
        assert counter["n"] == 3
        assert excinfo.value.attempts == 3


class TestRetryLoop:
    def test_explain_failure_is_retried(self, snap: FakeSnapshot) -> None:
        snap.explain_valid = False
        counter = {"n": 0}

        def llm(messages):
            counter["n"] += 1
            return "SELECT id FROM oarb.audits"

        with pytest.raises(GenerationFailedError) as excinfo:
            call(snap, llm, max_attempts=2)
        assert counter["n"] == 2
        assert excinfo.value.last_code == "validation_failed"
        assert "EXPLAIN" in excinfo.value.message

    def test_ddl_answer_is_retried_not_executed(self, snap: FakeSnapshot) -> None:
        with pytest.raises(GenerationFailedError) as excinfo:
            call(snap, lambda messages: "DROP TABLE oarb.audits", max_attempts=1)
        assert snap.data_calls() == []
        assert excinfo.value.last_code == "validation_failed"

    def test_garbage_answer_is_retried_not_executed(self, snap: FakeSnapshot) -> None:
        with pytest.raises(GenerationFailedError):
            call(snap, lambda messages: "привет, я не SQL", max_attempts=1)
        assert snap.data_calls() == []

    def test_llm_failure_is_reported(self, snap: FakeSnapshot) -> None:
        def llm(messages):
            raise RuntimeError("provider 503")

        with pytest.raises(GenerationFailedError) as excinfo:
            call(snap, llm, max_attempts=2)
        assert "provider 503" in excinfo.value.message

    def test_execution_error_is_not_retried(self, snap: FakeSnapshot) -> None:
        """Ошибка исполнения не чинится перегенерацией — это честный отказ."""
        counter = {"n": 0}

        def llm(messages):
            counter["n"] += 1
            return "SELECT id FROM oarb.audits"

        snap.data_result = {"status": "error", "rows": [], "error": "binder error"}
        with pytest.raises(QueryFailedError) as excinfo:
            call(snap, llm, max_attempts=3)
        assert counter["n"] == 1
        assert "binder error" in excinfo.value.message

    def test_default_attempt_count(self) -> None:
        assert MAX_ATTEMPTS == 4


class TestInputValidation:
    @pytest.mark.parametrize("query", ["", "   ", None, 5])
    def test_bad_query_rejected(self, query, snap: FakeSnapshot) -> None:
        with pytest.raises(AuditValidationError):
            run_generated_sql(
                query,
                snap.query,
                llm=lambda m: "SELECT id FROM oarb.audits",
                explain=snap.explain,
                read_schema=snap.schema,
                allowed_tables=ALLOWED,
                scripts_registry_table=REGISTRY_TABLE,
            )

    @pytest.mark.parametrize("ceiling", [0, -1, "100", None, True])
    def test_bad_ceiling_rejected(self, ceiling, snap: FakeSnapshot) -> None:
        with pytest.raises(AuditValidationError):
            call(snap, lambda m: "SELECT id FROM oarb.audits", row_ceiling=ceiling)

    def test_zero_attempts_rejected(self, snap: FakeSnapshot) -> None:
        with pytest.raises(AuditValidationError):
            call(snap, lambda m: "SELECT id FROM oarb.audits", max_attempts=0)

    def test_malformed_whitelist_rejected(self, snap: FakeSnapshot) -> None:
        with pytest.raises(AuditValidationError):
            call(snap, lambda m: "SELECT 1", allowed_tables=("a.b.c",))

    def test_schema_read_failure_is_query_failed(self, snap: FakeSnapshot) -> None:
        snap.schema_error = RuntimeError("information_schema unavailable")
        with pytest.raises(QueryFailedError):
            call(snap, lambda m: "SELECT id FROM oarb.audits")


class TestRegistryInGeneratedSql:
    """4.13: нечитаемый реестр больше не превращается в пустой few-shot."""

    def test_broken_registry_is_an_error(self) -> None:
        broken = FakeSnapshot(registry_error=RuntimeError("snapshot offline"))
        with pytest.raises(RegistryUnavailableError):
            call(broken, lambda m: "SELECT id FROM oarb.audits")

    def test_empty_registry_is_fine(self, snap: FakeSnapshot) -> None:
        empty = FakeSnapshot([], data_rows=[{"id": 7}])
        result = call(empty, lambda m: "SELECT id FROM oarb.audits")
        assert result.rows == [{"id": 7}]


class TestNoContextArgument:
    """Пункт 4.9: у библиотеки нет способа подклеить текст вызывающей стороны."""

    def test_llm_receives_only_messages(self, snap: FakeSnapshot) -> None:
        seen: list[list[dict]] = []

        def llm(messages):
            seen.append(messages)
            return "SELECT id FROM oarb.audits"

        call(snap, llm)
        assert seen, "модель не позвали"
        for messages in seen:
            assert isinstance(messages, list)
            for message in messages:
                assert set(message) <= {"role", "content"}

    def test_run_generated_sql_has_no_context_parameter(self) -> None:
        params = set(inspect.signature(run_generated_sql).parameters)
        assert "context" not in params
        assert "history" not in params

    def test_library_never_passes_context(self) -> None:
        """Ни один вызов в библиотеке не передаёт модели ``context``.

        Проверяется по AST, а не по тексту: в docstring пункт 4.9
        упомянут намеренно, и grep по исходникам такие строки ловил бы.
        """
        import ast
        from pathlib import Path

        audit_dir = Path(__file__).resolve().parent.parent / "libs" / "audit"
        offenders: list[str] = []
        for path in audit_dir.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    for keyword in node.keywords:
                        if keyword.arg == "context":
                            offenders.append(f"{path.name}: context= в вызове")
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    args = node.args
                    names = [
                        arg.arg
                        for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs)
                    ]
                    if "context" in names:
                        offenders.append(f"{path.name}: {node.name}(context=...)")
        assert not offenders, offenders


class TestFewShotSelection:
    def test_picks_relevant_scripts(self, snap: FakeSnapshot) -> None:
        from libs.audit import load_all

        registry = load_all(snap.query, REGISTRY_TABLE)
        block = select_few_shot("нарушения за период", registry)
        assert "oarb.violations" in block

    def test_empty_registry_gives_no_block(self) -> None:
        assert select_few_shot("что угодно", {}) == ""

    def test_unrelated_query_gives_no_block(self, snap: FakeSnapshot) -> None:
        from libs.audit import load_all

        registry = load_all(snap.query, REGISTRY_TABLE)
        assert select_few_shot("а", registry) == ""


class TestUnifiedResultContract:
    """4.14: один тип результата для обоих режимов, без ``mode``-зависимых веток."""

    def test_generated_result_fields(self, snap: FakeSnapshot) -> None:
        payload = call(snap, lambda m: "SELECT id FROM oarb.audits").to_payload()
        assert set(payload) == {
            "mode",
            "row_count",
            "columns",
            "rows",
            "sql",
            "script_name",
            "parameters",
            "no_match",
            "row_ceiling",
        }

    def test_both_pipelines_agree_on_shape(self, snap: FakeSnapshot) -> None:
        from libs.audit import run_predefined

        generated = call(snap, lambda m: "SELECT id FROM oarb.audits").to_payload()
        predefined = run_predefined(
            "violations_by_period",
            snap.query,
            {"date_from": "2024-01-01", "date_to": "2024-12-31"},
            scripts_registry_table=REGISTRY_TABLE,
        ).to_payload()
        assert set(generated) == set(predefined)
        assert generated["mode"] == "generated_sql"
        assert predefined["mode"] == "predefined"

    def test_no_error_dictionaries_in_results(self, snap: FakeSnapshot) -> None:
        """Неудача — исключение, а не словарь со ``status='error'``."""
        result = call(snap, lambda m: "SELECT id FROM oarb.audits")
        assert "status" not in result.to_payload()
        assert "error" not in result.to_payload()
