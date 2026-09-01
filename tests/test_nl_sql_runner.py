"""Тесты для ``NlSqlRunner`` — общего NL→SELECT pipeline.

Pipeline тестируется с моками ``CacheProvider`` и ``call_llm`` (без сети).
Интеграционные тесты с реальным LLM вынесены в бенчмарки.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from lib.services.nl_sql_runner import NlSqlRunner, NlSqlRunnerConfig
from lib.services.schema_formatter import SchemaFormatter
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
    """Минимальный CacheProvider для тестов: query_sql + explain."""

    def __init__(
        self,
        *,
        query_results: list[dict] | None = None,
        explain_results: list[dict] | None = None,
    ) -> None:
        self._query_results = list(query_results or [])
        self._explain_results = list(explain_results or [])
        self.query_calls: list[tuple[str, list | None]] = []
        self.explain_calls: list[str] = []

    def query_sql(self, sql: str, params: list | None = None) -> dict:
        self.query_calls.append((sql, params))
        if self._query_results:
            return self._query_results.pop(0)
        return {"status": "error", "error": "no result configured"}

    def explain(self, sql: str) -> dict:
        self.explain_calls.append(sql)
        if self._explain_results:
            return self._explain_results.pop(0)
        return {"valid": False, "error": "no result configured"}


def _make_runner(
    provider: FakeProvider,
    *,
    max_retries: int = 3,
    few_shot_top_n: int = 2,
) -> NlSqlRunner:
    table_registry.register(SkillRegistration(
        name="demo",
        resources=(
            TableResource(name="test.audits"),
            TableResource(name="test.violations"),
        ),
    ))
    return NlSqlRunner(
        provider=provider,
        schema_formatter=SchemaFormatter(cache_ttl_sec=0),
        config=NlSqlRunnerConfig(
            max_retries=max_retries,
            few_shot_top_n=few_shot_top_n,
            schema_max_chars=12_000,
        ),
    )


class TestTokenize:
    def test_lowercase_and_split(self) -> None:
        toks = NlSqlRunner._tokenize("Hello, World! foo bar")
        assert "hello" in toks
        assert "world" in toks
        assert "foo" in toks

    def test_short_tokens_dropped(self) -> None:
        toks = NlSqlRunner._tokenize("a bb ccc dddd")
        assert "a" not in toks
        assert "bb" not in toks
        assert "ccc" in toks
        assert "dddd" in toks


class TestSanitizeSqlResponse:
    def test_plain_sql(self) -> None:
        out = NlSqlRunner._sanitize_sql_response("SELECT 1")
        assert out == "SELECT 1"

    def test_markdown_block(self) -> None:
        text = "Some text\n```sql\nSELECT * FROM t\n```\nMore text"
        assert NlSqlRunner._sanitize_sql_response(text) == "SELECT * FROM t"

    def test_multiple_blocks_takes_last(self) -> None:
        text = "```sql\nSELECT 1\n```\n```sql\nSELECT 2\n```"
        assert NlSqlRunner._sanitize_sql_response(text) == "SELECT 2"

    def test_think_block_stripped(self) -> None:
        text = "<think>reasoning</think>SELECT 1"
        assert NlSqlRunner._sanitize_sql_response(text) == "SELECT 1"

    def test_no_sql_returns_empty(self) -> None:
        assert NlSqlRunner._sanitize_sql_response("just plain text") == ""

    def test_trailing_semicolon_stripped(self) -> None:
        assert NlSqlRunner._sanitize_sql_response("SELECT 1;") == "SELECT 1"


class TestSelectFewShot:
    def test_empty_registry(self) -> None:
        runner = NlSqlRunner(provider=FakeProvider())
        assert runner._select_few_shot("что-то", []) == ""

    def test_no_overlap(self) -> None:
        scripts = [{
            "name": "demo", "description": "unrelated", "sql_template": "SELECT 1",
            "tokens": NlSqlRunner._tokenize("unrelated stuff"),
        }]
        runner = NlSqlRunner(provider=FakeProvider())
        assert runner._select_few_shot("что-то совсем другое", scripts) == ""

    def test_picks_top_matches(self) -> None:
        scripts = [
            {
                "name": "by_year",
                "description": "audits by year month",
                "sql_template": "SELECT year, count(*) FROM audits GROUP BY year",
                "tokens": NlSqlRunner._tokenize("audits by year month"),
            },
            {
                "name": "by_obj",
                "description": "violations by audited object",
                "sql_template": "SELECT object, count(*) FROM violations",
                "tokens": NlSqlRunner._tokenize("violations audited object"),
            },
        ]
        runner = NlSqlRunner(
            provider=FakeProvider(),
            config=NlSqlRunnerConfig(few_shot_top_n=1),
        )
        out = runner._select_few_shot("audits 2024 по месяцам", scripts)
        assert "audits by year month" in out
        assert "violations by audited object" not in out


class TestBuildSystemPrompt:
    def test_contains_whitelist_and_hints(self) -> None:
        runner = NlSqlRunner(provider=FakeProvider())
        prompt = runner._build_system_prompt(
            tables=["oarb.audits"],
            schema="oarb",
            hints_block="\n  4. hint",
            few_shot_block="few shot block",
        )
        assert '"oarb"."audits"' in prompt
        assert "hint" in prompt
        assert "few shot block" in prompt

    def test_no_tables_uses_none_marker(self) -> None:
        runner = NlSqlRunner(provider=FakeProvider())
        prompt = runner._build_system_prompt(
            tables=[],
            schema="main",
            hints_block="",
            few_shot_block="",
        )
        assert "(none)" in prompt


class TestEmptyRegistry:
    def test_no_tables_returns_error(self) -> None:
        provider = FakeProvider()
        runner = NlSqlRunner(provider=provider)
        result = runner.run("что-то")
        assert result["status"] == "error"
        assert "TableRegistry" in result["data"]["message"]
        assert provider.query_calls == []


class TestRunSuccess:
    def test_success_first_attempt(self) -> None:
        provider = FakeProvider(
            query_results=[{
                "status": "success",
                "row_count": 1,
                "columns": ["x"],
                "rows": [[42]],
            }],
            explain_results=[{"valid": True}],
        )
        runner = _make_runner(provider)

        with patch.object(runner, "_call_llm", return_value="SELECT 42 AS x"):
            result = runner.run("дай число")

        assert result["status"] == "success"
        assert result["data"]["sql"] == "SELECT 42 AS x"
        assert len(provider.explain_calls) == 1

    def test_retry_after_explain_failure(self) -> None:
        provider = FakeProvider(
            query_results=[{
                "status": "success",
                "row_count": 1,
                "columns": ["x"],
                "rows": [[1]],
            }],
            explain_results=[
                {"valid": False, "error": "syntax error"},
                {"valid": True},
            ],
        )
        runner = _make_runner(provider)

        responses = iter([
            "SELECT bad FROM missing",
            "SELECT 1 AS x",
        ])
        with patch.object(runner, "_call_llm", side_effect=lambda *a, **kw: next(responses)):
            result = runner.run("дай")

        assert result["status"] == "success"
        assert result["data"]["sql"] == "SELECT 1 AS x"
        assert len(provider.explain_calls) == 2

    def test_retry_after_sql_error_at_execute(self) -> None:
        provider = FakeProvider(
            query_results=[
                {"status": "error", "error": "table not found"},
                {"status": "success", "row_count": 1, "columns": ["x"], "rows": [[1]]},
            ],
            explain_results=[{"valid": True}, {"valid": True}],
        )
        runner = _make_runner(provider)

        responses = iter(["SELECT * FROM bogus", "SELECT 1 AS x"])
        with patch.object(runner, "_call_llm", side_effect=lambda *a, **kw: next(responses)):
            result = runner.run("дай")

        assert result["status"] == "success"


class TestRunFailure:
    def test_all_retries_exhausted(self) -> None:
        provider = FakeProvider(
            query_results=[{"status": "error", "error": "fail"}] * 5,
            explain_results=[{"valid": False, "error": "bad"}] * 5,
        )
        runner = _make_runner(provider, max_retries=2)

        with patch.object(runner, "_call_llm", return_value="SELECT bad"):
            result = runner.run("дай")

        assert result["status"] == "error"
        assert result["data"]["sql"] == "SELECT bad"
        assert "попыток" in result["data"]["message"]

    def test_safety_error_triggers_retry(self) -> None:
        provider = FakeProvider(
            query_results=[{
                "status": "success",
                "row_count": 0,
                "columns": [],
                "rows": [],
            }],
            explain_results=[{"valid": True}],
        )
        runner = _make_runner(provider)

        responses = iter(["DROP TABLE audits", "SELECT 1"])
        with patch.object(runner, "_call_llm", side_effect=lambda *a, **kw: next(responses)):
            result = runner.run("удали")

        assert result["status"] == "success"
        assert "DROP" not in result["data"]["sql"]

    def test_lock_error_breaks_retry_loop(self) -> None:
        provider = FakeProvider(
            explain_results=[{"valid": False, "error": "временно занята"}],
        )
        runner = _make_runner(provider)

        with patch.object(runner, "_call_llm", return_value="SELECT 1"):
            result = runner.run("дай")

        assert result["status"] == "error"
        assert "временно занята" in result["data"]["message"]
        assert len(provider.explain_calls) == 1

    def test_empty_sql_response_treated_as_error(self) -> None:
        provider = FakeProvider(
            query_results=[{
                "status": "success",
                "row_count": 1,
                "columns": ["x"],
                "rows": [[1]],
            }],
            explain_results=[{"valid": True}],
        )
        runner = _make_runner(provider)

        responses = iter(["no sql here", "SELECT 1 AS x"])
        with patch.object(runner, "_call_llm", side_effect=lambda *a, **kw: next(responses)):
            result = runner.run("дай")

        assert result["status"] == "success"
        assert result["data"]["sql"] == "SELECT 1 AS x"

    def test_llm_call_exception_triggers_retry(self) -> None:
        provider = FakeProvider(
            query_results=[{
                "status": "success",
                "row_count": 1,
                "columns": ["x"],
                "rows": [[1]],
            }],
            explain_results=[{"valid": True}],
        )
        runner = _make_runner(provider)

        responses = iter(["SELECT 1 AS x"])
        call_count = {"n": 0}

        def side_effect(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("boom")
            return next(responses)

        with patch.object(runner, "_call_llm", side_effect=side_effect):
            result = runner.run("дай")

        assert result["status"] == "success"
        assert call_count["n"] == 2


class TestFewShotDisabled:
    def test_no_registry_loaded_when_disabled(self) -> None:
        provider = FakeProvider(
            query_results=[{
                "status": "success",
                "row_count": 1,
                "columns": ["x"],
                "rows": [[1]],
            }],
            explain_results=[{"valid": True}],
        )
        runner = _make_runner(provider)

        with patch.object(runner, "_call_llm", return_value="SELECT 1 AS x"):
            result = runner.run("дай", no_few_shot=True)

        assert result["status"] == "success"
        # no_few_shot=True → _load_predefined_scripts не вызывается → нет query
        # для SELECT FROM "<schema>"."<scripts_registry>"
        registry_calls = [
            c for c in provider.query_calls if "ORDER BY name" in c[0]
        ]
        assert registry_calls == []


class TestHintsBlock:
    def test_hints_included_in_system_prompt(self) -> None:
        provider = FakeProvider(
            query_results=[{
                "status": "success",
                "row_count": 0,
                "columns": [],
                "rows": [],
            }],
            explain_results=[{"valid": True}],
        )
        runner = _make_runner(provider)

        captured_prompts: list[str] = []
        real_call = runner._call_llm

        def capture(messages, *, context=None):
            captured_prompts.append(messages[0]["content"])
            return "SELECT 1"

        with patch.object(runner, "_call_llm", side_effect=capture):
            runner.run("дай", hints_block="\n  4. custom hint")

        assert any("custom hint" in p for p in captured_prompts)


class TestLoadPredefinedScripts:
    def test_no_label_registered_returns_empty(self) -> None:
        table_registry.register(SkillRegistration(
            name="demo",
            resources=(TableResource(name="test.audits"),),
        ))
        provider = FakeProvider(query_results=[{
            "status": "success",
            "row_count": 0,
            "rows": [],
        }])
        runner = NlSqlRunner(provider=provider)
        assert runner._load_predefined_scripts() == []
        assert provider.query_calls == []

    def test_query_executed_with_registry_label(self) -> None:
        table_registry.register(SkillRegistration(
            name="demo",
            resources=(
                TableResource(name="test.audits"),
                TableResource(name="test.registry", label="scripts_registry"),
            ),
        ))
        provider = FakeProvider(query_results=[{
            "status": "success",
            "row_count": 2,
            "rows": [
                {"name": "s1", "description": "d1", "sql_template": "SELECT 1"},
                {"name": "s2", "description": "d2", "sql_template": "SELECT 2"},
            ],
        }])
        runner = NlSqlRunner(provider=provider)
        out = runner._load_predefined_scripts()
        assert len(out) == 2
        assert "ORDER BY name" in provider.query_calls[0][0]
        assert '"test"."registry"' in provider.query_calls[0][0]

    def test_query_error_returns_empty(self) -> None:
        table_registry.register(SkillRegistration(
            name="demo",
            resources=(TableResource(name="test.x", label="scripts_registry"),),
        ))
        provider = FakeProvider(query_results=[{
            "status": "error",
            "error": "boom",
        }])
        runner = NlSqlRunner(provider=provider)
        assert runner._load_predefined_scripts() == []


