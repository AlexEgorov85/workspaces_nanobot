"""Edge-case тесты для ``audit_analyzer``: ``<NO_MATCH>`` и unknown index.

Покрывают два контракта из SKILL.md, которые легко сломать при
рефакторинге:

1. ``<NO_MATCH>`` от LLM — честный ``status="success"`` с пустым
   ``result``, а не ошибка и не silent-swap на похожую таблицу
   (``generated_sql_mode.run``).
2. Невалидный ``--index-name`` — ``status="error"`` с явным
   ``error_type`` (``unknown_index`` или ``registry_unavailable``),
   ``search_vector`` НЕ вызывается (``cli._run_vector``).

Тесты — pytest-only, прямой вызов CLI-хелперов без subprocess
(быстрее и обходит проблемы с кириллицей в PowerShell).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest


# ``generated_sql_mode.py`` импортирует ``from llm import chat`` — это
# резолвится через ``scripts/llm.py`` ТОЛЬКО если ``scripts/`` в ``sys.path``.
# ``pyproject.toml::pythonpath`` кладёт ``legal_summarizer/scripts`` ПЕРЕД
# audit_analyzer, поэтому ``import llm`` находит не тот модуль. Повторяем
# трюк ``cli.py:50-54``: добавляем ``scripts/`` в sys.path первым.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from workspace.skills.audit_analyzer.scripts import generated_sql_mode  # noqa: E402
from workspace.skills.audit_analyzer.scripts import cli as audit_cli  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers: замоканный db + подмены skill_config
# ---------------------------------------------------------------------------


class _FakeDB:
    """Generic-заглушка под ``CacheProvider``: ``get_schema`` + ``query_sql`` + ``explain``.

    ``search_vector`` поднимает ``AssertionError`` если кто-то его
    вызовет без явного разрешения (anti silent-fail guard).
    """

    def __init__(self, *, explain_valid: bool = True) -> None:
        self._explain_valid = explain_valid
        self.get_schema_calls = 0
        self.explain_calls: list[str] = []
        self.query_sql_calls: list[str] = []
        self.search_vector_calls: list[tuple[Any, ...]] = []

    def get_schema(self, schema_name: str = "", table_names=None) -> dict[str, Any]:
        self.get_schema_calls += 1
        # Контракт ``format_schema`` (lib/utils/sql_safety.py:394):
        # {"schema": "oarb", "tables": {name: {"columns": {...}}}}.
        tables = {
            name: {
                "comment": "",
                "columns": {
                    "id": {"type": "integer", "not_null": True, "comment": ""},
                },
            }
            for name in (table_names or [])
        }
        return {"schema": schema_name, "tables": tables}

    def explain(self, sql: str) -> dict[str, Any]:
        self.explain_calls.append(sql)
        if self._explain_valid:
            return {"valid": True, "error": ""}
        return {"valid": False, "error": "syntax error"}

    def query_sql(self, sql: str, params: list[Any] | None = None) -> dict[str, Any]:
        self.query_sql_calls.append(sql)
        return {
            "status": "success",
            "row_count": 0,
            "columns": [],
            "rows": [],
        }

    def search_vector(self, *args: Any, **kwargs: Any) -> list[Any]:
        self.search_vector_calls.append((args, kwargs))
        raise AssertionError(
            "search_vector не должен вызываться при unknown_index/"
            "registry_unavailable (silent-fail regression guard)"
        )


@pytest.fixture
def fake_db() -> _FakeDB:
    return _FakeDB()


@pytest.fixture
def stub_skill_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Подменить ``skill_config.*`` функции, которые читают project.json.

    ``generated_sql_mode.run()`` импортирует функции через
    ``from skill_config import get_db_tables`` — это bind алиаса внутри
    ``generated_sql_mode``. Поэтому мокаем **на ``generated_sql_mode``**,
    а не на ``skill_config``: bind уже произошёл к моменту теста.

    Также мокаем ``predefined.db_loader.load_all`` на пустой dict, чтобы
    pipeline не пытался читать реестр через TableRegistry.
    """
    monkeypatch.setattr(
        generated_sql_mode, "get_db_tables", lambda: ["audits", "violations"]
    )
    monkeypatch.setattr(generated_sql_mode, "get_db_schema", lambda: "oarb")
    monkeypatch.setattr(
        generated_sql_mode,
        "get_predefined_scripts_table",
        lambda: "public.agent_predefined_scripts",
    )

    # ``generated_sql_mode`` импортирует ``load_all`` напрямую через
    # ``from workspace.skills.audit_analyzer.scripts.predefined.db_loader
    # import load_all``. Поэтому мокаем алиас внутри ``generated_sql_mode``.
    monkeypatch.setattr(generated_sql_mode, "load_all", lambda db, table: {})


# ---------------------------------------------------------------------------
# 1. Чистая функция: is_no_match
# ---------------------------------------------------------------------------


class TestIsNoMatchRecognizesMarker:
    """``is_no_match`` распознаёт ``<NO_MATCH>`` в разных форматах."""

    @pytest.mark.parametrize(
        "raw",
        [
            "<NO_MATCH>",
            "<no_match>",
            "<No_Match>",
            "  <NO_MATCH>  ",
            "<NO_MATCH>.",
            "<NO_MATCH>;",
            "<NO_MATCH>,",
            "<NO_MATCH>\n",
        ],
    )
    def test_recognizes_marker(self, raw: str) -> None:
        assert generated_sql_mode.is_no_match(raw) is True

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "   ",
            "SELECT 1",
            "<no_match_text>",
            "no_match",
            "<NO_MATCH> AND SELECT 1",
            "Верни <NO_MATCH> если данных нет",
        ],
    )
    def test_rejects_non_markers(self, raw: str) -> None:
        assert generated_sql_mode.is_no_match(raw) is False


# ---------------------------------------------------------------------------
# 2-4. generated_sql_mode.run() c <NO_MATCH>
# ---------------------------------------------------------------------------


class TestNoMatchQueryBehavior:
    """``run()`` корректно обрабатывает ``<NO_MATCH>``: success, no retry, no swap."""

    def test_no_match_query_returns_success_no_match_true(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_db: _FakeDB,
        stub_skill_config: None,
    ) -> None:
        """LLM вернул ``<NO_MATCH>``: status=success, no_match=True, row_count=0."""

        chat_calls = {"n": 0}

        def fake_chat(messages, context=None) -> str:
            chat_calls["n"] += 1
            return "<NO_MATCH>"

        monkeypatch.setattr(generated_sql_mode, "chat", fake_chat)

        result = generated_sql_mode.run("нерелевантный запрос", fake_db)

        assert result["status"] == "success"
        assert result["mode"] == "generated_sql"
        assert result["data"]["no_match"] is True
        assert result["data"]["sql"] == ""
        assert result["data"]["result"]["status"] == "success"
        assert result["data"]["result"]["row_count"] == 0
        assert result["data"]["result"]["rows"] == []
        # LLM вызван один раз (без retry)
        assert chat_calls["n"] == 1
        # query_sql / explain не дёргаются — мы остановились на <NO_MATCH>
        assert fake_db.explain_calls == []
        assert fake_db.query_sql_calls == []

    def test_no_match_query_with_markdown_wrapping(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_db: _FakeDB,
        stub_skill_config: None,
    ) -> None:
        """LLM обернул ``<NO_MATCH>`` в markdown-блок: sanitizer извлечёт маркер."""

        def fake_chat(messages, context=None) -> str:
            return "```\n<NO_MATCH>\n```"

        monkeypatch.setattr(generated_sql_mode, "chat", fake_chat)

        result = generated_sql_mode.run("запрос", fake_db)

        assert result["status"] == "success"
        assert result["data"]["no_match"] is True
        assert result["data"]["result"]["row_count"] == 0

    def test_no_match_does_not_retry(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_db: _FakeDB,
        stub_skill_config: None,
    ) -> None:
        """После ``<NO_MATCH>`` — сразу success, без ``MAX_ATTEMPTS`` попыток."""

        chat_calls = {"n": 0}

        def fake_chat(messages, context=None) -> str:
            chat_calls["n"] += 1
            return "<NO_MATCH>"

        monkeypatch.setattr(generated_sql_mode, "chat", fake_chat)

        result = generated_sql_mode.run("запрос", fake_db)

        assert result["status"] == "success"
        assert chat_calls["n"] == 1
        assert chat_calls["n"] < generated_sql_mode.MAX_ATTEMPTS

    def test_no_match_lowercase_upper_variants(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_db: _FakeDB,
        stub_skill_config: None,
    ) -> None:
        """``<no_match>`` (lowercase) распознаётся — is_no_match делает .upper()."""

        monkeypatch.setattr(generated_sql_mode, "chat", lambda messages, context=None: "<no_match>")
        result = generated_sql_mode.run("запрос", fake_db)

        assert result["status"] == "success"
        assert result["data"]["no_match"] is True


# ---------------------------------------------------------------------------
# 5-8. cli._run_vector: unknown_index / registry_unavailable
# ---------------------------------------------------------------------------


class TestVectorUnknownIndexErrors:
    """``_run_vector`` отдаёт структурированную ошибку при unknown/unavailable индексе."""

    def test_unknown_index_returns_error_type_unknown_index(
        self, monkeypatch: pytest.MonkeyPatch, fake_db: _FakeDB
    ) -> None:
        monkeypatch.setattr(
            audit_cli,
            "_resolve_known_index",
            lambda name: (
                False,
                f"vector index '{name}' is not registered. "
                "Available indexes: audits_index, violations_index.",
            ),
        )

        result = audit_cli._run_vector(
            query="пожарная безопасность",
            db=fake_db,
            index_name="nonexistent_index",
            top_k=5,
            threshold=None,
        )

        assert result["status"] == "error"
        assert result["data"]["error_type"] == "unknown_index"
        assert "nonexistent_index" in result["data"]["message"]
        # search_vector не вызывался
        assert fake_db.search_vector_calls == []

    def test_registry_unavailable_returns_error_type_registry_unavailable(
        self, monkeypatch: pytest.MonkeyPatch, fake_db: _FakeDB
    ) -> None:
        monkeypatch.setattr(
            audit_cli,
            "_resolve_known_index",
            lambda name: (
                None,
                "Не удалось прочитать runtime-реестр индексов: PG offline. "
                "Запустите gateway (python gateway.py).",
            ),
        )

        result = audit_cli._run_vector(
            query="пожарная безопасность",
            db=fake_db,
            index_name="audits_index",
            top_k=5,
            threshold=None,
        )

        assert result["status"] == "error"
        assert result["data"]["error_type"] == "registry_unavailable"
        assert "PG offline" in result["data"]["message"]
        # search_vector не вызывался — иначе silent fail на registry-offline
        assert fake_db.search_vector_calls == []

    def test_unknown_index_message_lists_available(
        self, monkeypatch: pytest.MonkeyPatch, fake_db: _FakeDB
    ) -> None:
        """``msg`` содержит список доступных индексов — пользователь видит варианты."""

        available_msg = (
            "vector index 'wrong' is not registered. "
            "Available indexes: audits_index, violations_index, audit_reports_index."
        )
        monkeypatch.setattr(
            audit_cli,
            "_resolve_known_index",
            lambda name: (False, available_msg),
        )

        result = audit_cli._run_vector(
            query="запрос",
            db=fake_db,
            index_name="wrong",
            top_k=5,
            threshold=None,
        )

        assert result["status"] == "error"
        assert result["data"]["error_type"] == "unknown_index"
        # Список доступных индексов попадает в message, не теряется.
        for idx in ("audits_index", "violations_index", "audit_reports_index"):
            assert idx in result["data"]["message"]

    def test_unknown_index_does_not_call_search_vector(
        self, monkeypatch: pytest.MonkeyPatch, fake_db: _FakeDB
    ) -> None:
        """Anti silent-fail: при unknown_index search_vector не вызывается.

        Если бы ``_run_vector`` падал в ``search_vector`` (провайдер тихо
        вернул бы ``[]`` через ``_search_error``), пользователь увидел бы
        «Документы не найдены» как ``status=success``. Регрессия здесь
        недопустима (см. SKILL.md §vector).
        """

        class _ExplodingProvider(_FakeDB):
            def search_vector(self, *args: Any, **kwargs: Any) -> list[Any]:
                raise AssertionError(
                    "search_vector не должен вызываться при unknown_index "
                    "(silent-fail regression guard)"
                )

        exploding_db = _ExplodingProvider()
        monkeypatch.setattr(
            audit_cli,
            "_resolve_known_index",
            lambda name: (False, "vector index 'bad' is not registered"),
        )

        result = audit_cli._run_vector(
            query="запрос",
            db=exploding_db,
            index_name="bad",
            top_k=5,
            threshold=None,
        )

        assert result["status"] == "error"
        assert result["data"]["error_type"] == "unknown_index"

    def test_unknown_index_uses_default_index_name_when_none_provided(
        self, monkeypatch: pytest.MonkeyPatch, fake_db: _FakeDB
    ) -> None:
        """Если ``--index-name`` не задан — дефолт ``audits_index`` всё равно
        проходит через ``_resolve_known_index`` (нет silent default)."""

        captured: dict[str, Any] = {}

        def fake_resolve(name: str) -> tuple[bool | None, str]:
            captured["index_name"] = name
            return (False, f"vector index '{name}' is not registered")

        monkeypatch.setattr(audit_cli, "_resolve_known_index", fake_resolve)

        result = audit_cli._run_vector(
            query="запрос",
            db=fake_db,
            index_name=None,
            top_k=5,
            threshold=None,
        )

        assert captured["index_name"] == "audits_index"
        assert result["status"] == "error"
        assert result["data"]["error_type"] == "unknown_index"


# ---------------------------------------------------------------------------
# Smoke: вспомогательная проверка MagicMock-контракта для search_vector
# ---------------------------------------------------------------------------


def test_fake_db_search_vector_raises_by_default() -> None:
    """Защитный тест: ``_FakeDB.search_vector`` падает, чтобы случайный
    вызов был виден как ошибка, а не молчаливое ``[]``.
    """
    db = _FakeDB()
    with pytest.raises(AssertionError, match="silent-fail"):
        db.search_vector("q", index_name="x", top_k=5)