"""Unit-тесты для ``generated_sql_mode``: sanitize_sql_response, retry-цикл,
happy path, all-retries-failed.

Тесты подменяют ``llm.chat`` и ``db.*`` через ``monkeypatch`` — не делают
реальных HTTP/SQL-вызовов. Скрипт-модуль импортируется как top-level
``generated_sql_mode`` (с подкладыванием ``scripts/`` в ``sys.path``),
потому что именно так его видит ``cli.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

# Подкладываем scripts/ в sys.path, чтобы ``from llm import chat`` и
# ``from skill_config import ...`` внутри модуля резолвились так же,
# как при запуске CLI.
_SCRIPTS_DIR = str(
    Path(__file__).resolve().parents[1]
    / "workspace" / "skills" / "audit_analyzer" / "scripts"
)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

# Импорт после подкладывания пути — иначе `from llm import chat` упадёт.
import generated_sql_mode as gsm  # noqa: E402
import llm as llm_module  # noqa: E402


# ---------- sanitize_sql_response ---------------------------------------------


class TestSanitizeSqlResponse:
    """Извлечение SQL из ответа LLM: markdown-блоки, think-блоки, голый SQL."""

    def test_plain_sql(self) -> None:
        sql = gsm.sanitize_sql_response("SELECT 1")
        assert sql == "SELECT 1"

    def test_trailing_semicolon_stripped(self) -> None:
        sql = gsm.sanitize_sql_response("SELECT 1;\n")
        assert sql == "SELECT 1"

    def test_markdown_sql_block(self) -> None:
        text = (
            "Some preamble text\n"
            "```sql\n"
            "SELECT id FROM oarb.audits\n"
            "```\n"
        )
        sql = gsm.sanitize_sql_response(text)
        assert sql == "SELECT id FROM oarb.audits"

    def test_markdown_plain_block(self) -> None:
        """Блок ``` без указания языка тоже должен извлекаться."""
        text = (
            "```\n"
            "SELECT count(*) FROM oarb.audits\n"
            "```\n"
        )
        sql = gsm.sanitize_sql_response(text)
        assert sql == "SELECT count(*) FROM oarb.audits"

    def test_think_block_stripped(self) -> None:
        text = (
            "<think>The user wants...</think>\n"
            "SELECT 1"
        )
        sql = gsm.sanitize_sql_response(text)
        assert "<think>" not in sql
        assert "SELECT 1" in sql

    def test_no_sql_returns_text_stripped(self) -> None:
        """Нет SQL-маркера — возвращается текст без trailing ';'.
        Вызывающий код (``run``) дальше сам увидит пустой SQL после
        sanitize и валидация вернёт ошибку.
        """
        sql = gsm.sanitize_sql_response("I don't know")
        assert sql == "I don't know"

    def test_uses_last_block_when_multiple(self) -> None:
        """Если блоков несколько — берём последний (финальный SQL)."""
        text = (
            "```sql\n"
            "SELECT 1 -- draft\n"
            "```\n"
            "Hmm let me think...\n"
            "```sql\n"
            "SELECT 2 -- final\n"
            "```\n"
        )
        sql = gsm.sanitize_sql_response(text)
        assert "SELECT 2" in sql
        assert "draft" not in sql


# ---------- run() — retry-цикл и happy path -----------------------------------


class _FakeDB:
    """Минимальный fake CacheProvider для run(): get_schema/query_sql/explain."""

    def __init__(
        self,
        *,
        schema: dict | None = None,
        query_results: list[dict] | None = None,
        explain_results: list[dict] | None = None,
        predefined_registry: list[dict] | None = None,
    ) -> None:
        self.schema = schema or {
            "schema": "oarb",
            "tables": {"audits": {"columns": {"id": {"type": "integer"}}}},
        }
        self._query_results = list(query_results or [])
        self._explain_results = list(explain_results or [])
        self._predefined_registry = list(predefined_registry or [])
        self.get_schema_calls = 0
        self.query_sql_calls: list[str] = []

    def get_schema(self, *, schema_name: str, table_names: list[str] | None) -> dict:
        self.get_schema_calls += 1
        return self.schema

    def query_sql(self, sql: str, params: list | None = None) -> dict:
        self.query_sql_calls.append(sql)
        # Первый SELECT к реестру predefined_scripts — отдаём реестр (если есть).
        if "agent_predefined_scripts" in sql:
            return {
                "status": "success",
                "row_count": len(self._predefined_registry),
                "columns": ["name", "description", "sql_template"],
                "rows": self._predefined_registry,
            }
        # Иначе — берём из queue.
        if self._query_results:
            return self._query_results.pop(0)
        return {"status": "error", "error": "no more fake query results"}

    def explain(self, sql: str) -> dict:
        if self._explain_results:
            return self._explain_results.pop(0)
        return {"valid": True, "error": ""}


@pytest.fixture
def fake_db() -> _FakeDB:
    """DB с пустым реестром predefined и пустыми очередями."""
    return _FakeDB()


@pytest.fixture
def stub_llm_chat(monkeypatch: pytest.MonkeyPatch):
    """Подменить ``llm.chat`` управляемой функцией, возвращающей очередь."""
    responses: list[str] = []
    calls: list[list[dict]] = []

    def _fake_chat(messages, *, context=None, **kwargs):
        calls.append(list(messages))
        if not responses:
            return "SELECT 1"
        return responses.pop(0)

    monkeypatch.setattr(llm_module, "chat", _fake_chat)
    monkeypatch.setattr(gsm, "chat", _fake_chat)
    return _fake_chat, responses, calls


# Skill-конфиг возвращает _core_cfg функции — подменим их, чтобы не зависеть
# от реального project.json и ApplicationContext.
@pytest.fixture
def stub_skill_config(monkeypatch: pytest.MonkeyPatch) -> None:
    import skill_config  # noqa: E402

    monkeypatch.setattr(skill_config, "get_db_tables", lambda: ["audits", "violations"])
    monkeypatch.setattr(skill_config, "get_db_schema", lambda: "oarb")
    monkeypatch.setattr(
        skill_config,
        "get_predefined_scripts_table",
        lambda: "public.agent_predefined_scripts",
    )


class TestGeneratedSqlRun:
    """run(): happy path / validation retry / explain retry / all-failed."""

    def test_happy_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
        stub_llm_chat,
        stub_skill_config,
        fake_db: _FakeDB,
    ) -> None:
        """LLM сразу возвращает валидный SQL → один вызов chat, success."""
        _chat, responses, _calls = stub_llm_chat
        responses.append("```sql\nSELECT count(*) AS n FROM oarb.audits\n```")
        fake_db._query_results.append(
            {"status": "success", "row_count": 1, "columns": ["n"], "rows": [{"n": 42}]}
        )
        monkeypatch.setattr(gsm, "_load_predefined_scripts", lambda db: [])
        out = gsm.run("сколько аудитов?", db=fake_db)
        assert out["status"] == "success"
        assert out["mode"] == "generated_sql"
        assert "SELECT count(*)" in out["data"]["sql"]
        assert out["data"]["result"]["row_count"] == 1
        assert out["data"]["result"]["rows"] == [{"n": 42}]
        assert len(_calls) == 1

    def test_retries_on_safety_violation(
        self,
        monkeypatch: pytest.MonkeyPatch,
        stub_llm_chat,
        stub_skill_config,
        fake_db: _FakeDB,
    ) -> None:
        """Первый SQL нарушает safety (DDL) → retry, второй — SELECT, success."""
        _chat, responses, calls = stub_llm_chat
        responses.append("DROP TABLE oarb.audits")  # safety_error
        responses.append("SELECT 1 FROM oarb.audits")  # ok
        fake_db._explain_results.append({"valid": True, "error": ""})
        fake_db._query_results.append(
            {"status": "success", "row_count": 0, "columns": ["x"], "rows": []}
        )
        monkeypatch.setattr(gsm, "_load_predefined_scripts", lambda db: [])
        out = gsm.run("?", db=fake_db)
        assert out["status"] == "success"
        # 2 вызова chat (1 fail + 1 retry success).
        assert len(calls) == 2
        # Retry-сообщение содержит прошлую ошибку.
        assert "исправь" in calls[1][-1]["content"].lower()

    def test_retries_on_explain_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
        stub_llm_chat,
        stub_skill_config,
        fake_db: _FakeDB,
    ) -> None:
        """Первый SQL не проходит EXPLAIN → retry → success."""
        _chat, responses, _calls = stub_llm_chat
        responses.append("SELECT * FROM nonexistent_table")
        responses.append("SELECT 1 FROM oarb.audits")
        fake_db._explain_results.append({"valid": False, "error": "table not found"})
        fake_db._explain_results.append({"valid": True, "error": ""})
        fake_db._query_results.append(
            {"status": "success", "row_count": 0, "columns": ["x"], "rows": []}
        )
        monkeypatch.setattr(gsm, "_load_predefined_scripts", lambda db: [])
        out = gsm.run("?", db=fake_db)
        assert out["status"] == "success"

    def test_all_retries_exhausted(
        self,
        monkeypatch: pytest.MonkeyPatch,
        stub_llm_chat,
        stub_skill_config,
        fake_db: _FakeDB,
    ) -> None:
        """Если все MAX_RETRIES+1 попыток падают по safety → error + last sql."""
        _chat, responses, _calls = stub_llm_chat
        for _ in range(gsm.MAX_RETRIES + 1):
            responses.append("DROP TABLE x")  # safety_error
        monkeypatch.setattr(gsm, "_load_predefined_scripts", lambda db: [])
        out = gsm.run("?", db=fake_db)
        assert out["status"] == "error"
        assert out["mode"] == "generated_sql"
        assert "Не удалось" in out["data"]["message"]
        assert "DROP TABLE x" in out["data"]["sql"]
        assert _calls.call_count if hasattr(_calls, "call_count") else len(_calls) == gsm.MAX_RETRIES + 1

    def test_llm_exception_is_retried(
        self,
        monkeypatch: pytest.MonkeyPatch,
        stub_llm_chat,
        stub_skill_config,
        fake_db: _FakeDB,
    ) -> None:
        """chat() упал с исключением → retry на следующей итерации."""
        _chat, responses, _calls = stub_llm_chat

        # Подменим chat, чтобы первая попытка бросила исключение,
        # вторая — вернула валидный SQL.
        call_count = {"n": 0}

        def _flaky(messages, *, context=None, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("transient LLM error")
            return "SELECT 1 FROM oarb.audits"

        monkeypatch.setattr(gsm, "chat", _flaky)
        monkeypatch.setattr(llm_module, "chat", _flaky)

        monkeypatch.setattr(gsm, "_load_predefined_scripts", lambda db: [])
        fake_db._explain_results.append({"valid": True, "error": ""})
        fake_db._query_results.append(
            {"status": "success", "row_count": 0, "columns": ["x"], "rows": []}
        )
        out = gsm.run("?", db=fake_db)
        assert out["status"] == "success"
        assert call_count["n"] == 2
