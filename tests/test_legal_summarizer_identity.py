"""Идентичность оборота доходит до навыка legal_summarizer.

Цепочка, которую проверяет этот файл, короткая, но каждый её конец может
разорваться молча:

    RequestContext → tool `legal_summarizer_query` → env подпроцесса →
    `skills/legal_summarizer/scripts/llm/client.py` → `complete(identity=…)` →
    `params._meta`

Разрыв в любом месте даёт один и тот же симптом — сервер отвечает
``identity_missing``, — и выглядит он при этом как «платформа не работает».
Поэтому проверяются оба конца плюс запрет на подмену: идентичность приходит из
``RequestContext`` и журнала, а не из аргументов модели.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL_PATH = REPO_ROOT / "workspace" / "tools" / "legal_summarizer_query.py"
SKILL_CLIENT_PATH = (
    REPO_ROOT
    / "workspace"
    / "skills"
    / "legal_summarizer"
    / "scripts"
    / "llm"
    / "client.py"
)

IDENTITY_ENV = (
    "ENTERPRISE_SESSION_ID",
    "ENTERPRISE_USER_ID",
    "ENTERPRISE_REQUEST_ID",
)


def _load_tool_module() -> Any:
    spec = importlib.util.spec_from_file_location("_lsq_under_test", TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def tool_module() -> Any:
    return _load_tool_module()


class _RequestContext:
    def __init__(self, sender_id: str | None) -> None:
        self.sender_id = sender_id


@pytest.fixture
def in_a_turn(monkeypatch: Any) -> Any:
    """Живой оборот: есть ``RequestContext``, есть журнал с PK."""
    from nanobot.agent.tools import context as ctx_mod

    class _Logging:
        def get_request_id(self, session_key: str) -> str:
            return "req-from-log"

    monkeypatch.setattr(ctx_mod, "current_request_context", lambda: _RequestContext("u-1"))
    monkeypatch.setattr(ctx_mod, "current_request_session_key", lambda: "sess-1")
    return _Logging()


# ---------------------------------------------------------------------------
# Конец 1: tool кладёт идентичность в окружение подпроцесса
# ---------------------------------------------------------------------------


class TestToolPutsIdentityIntoTheSubprocessEnv:
    def test_all_three_values_are_exported(self, tool_module: Any, in_a_turn: Any) -> None:
        tool = tool_module.LegalSummarizerQueryTool(
            config=tool_module.LegalSummarizerQueryToolConfig(),
            request_id_source=in_a_turn,
        )
        env = tool._identity_env()
        assert env == {
            "ENTERPRISE_SESSION_ID": "sess-1",
            "ENTERPRISE_USER_ID": "u-1",
            "ENTERPRISE_REQUEST_ID": "req-from-log",
        }

    def test_missing_request_id_is_simply_absent(
        self, tool_module: Any, in_a_turn: Any, monkeypatch: Any
    ) -> None:
        """Нет PK — нет и переменной. Не пустая строка и не выдуманное значение.

        Подставленный ``request_id`` выглядел бы в журнале как существующий
        оборот; пустая строка выглядела бы как «идентичность была и пуста».
        """
        monkeypatch.setattr(in_a_turn, "get_request_id", lambda _key: None)
        tool = tool_module.LegalSummarizerQueryTool(
            config=tool_module.LegalSummarizerQueryToolConfig(),
            request_id_source=in_a_turn,
        )
        env = tool._identity_env()
        assert "ENTERPRISE_REQUEST_ID" not in env
        assert env["ENTERPRISE_SESSION_ID"] == "sess-1"

    def test_broken_log_does_not_take_the_call_down(
        self, tool_module: Any, in_a_turn: Any
    ) -> None:
        """Журнал может ответить отказом — вызов от этого не теряется.

        Идентичность без ``request_id`` лучше, чем отказ: журнал платформы
        покажет вызов с ``correlated = false``, и это разберут.
        """

        class _Broken:
            def get_request_id(self, _key: str) -> str:
                raise RuntimeError("журнал недоступен")

        tool = tool_module.LegalSummarizerQueryTool(
            config=tool_module.LegalSummarizerQueryToolConfig(),
            request_id_source=_Broken(),
        )
        env = tool._identity_env()
        assert env["ENTERPRISE_SESSION_ID"] == "sess-1"
        assert "ENTERPRISE_REQUEST_ID" not in env

    def test_outside_a_turn_nothing_is_exported(
        self, tool_module: Any, monkeypatch: Any
    ) -> None:
        """Вне оборота окружение остаётся чистым.

        Иначе навык, запущенный из shell вручную, отправил бы вызов с
        идентичностью прошлого оборота — и журнал показал бы чужую сессию.
        """
        from nanobot.agent.tools import context as ctx_mod

        monkeypatch.setattr(ctx_mod, "current_request_context", lambda: None)
        tool = tool_module.LegalSummarizerQueryTool(
            config=tool_module.LegalSummarizerQueryToolConfig()
        )
        assert tool._identity_env() == {}

    def test_without_sender_there_is_no_identity(
        self, tool_module: Any, monkeypatch: Any
    ) -> None:
        """Нет отправителя — нет ``user_id``, и подставлять нечего.

        Пустой ``user_id`` прошёл бы на сервер как «идентичность была и пуста»,
        то есть как настоящий, но пустой, вызов.
        """
        from nanobot.agent.tools import context as ctx_mod

        monkeypatch.setattr(
            ctx_mod, "current_request_context", lambda: _RequestContext(None)
        )
        monkeypatch.setattr(ctx_mod, "current_request_session_key", lambda: "sess-1")
        tool = tool_module.LegalSummarizerQueryTool(
            config=tool_module.LegalSummarizerQueryToolConfig()
        )
        assert tool._identity_env() == {}


# ---------------------------------------------------------------------------
# Конец 2: навык читает их и передаёт клиенту
# ---------------------------------------------------------------------------


def _load_skill_client() -> Any:
    if str(REPO_ROOT / "mcp-platform") not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / "mcp-platform"))
    spec = importlib.util.spec_from_file_location("_ls_client_under_test", SKILL_CLIENT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TestSkillReadsAndForwardsIdentity:
    @pytest.mark.parametrize(
        ("env", "expected"),
        [
            (
                {
                    "ENTERPRISE_SESSION_ID": "sess-1",
                    "ENTERPRISE_USER_ID": "u-1",
                    "ENTERPRISE_REQUEST_ID": "req-1",
                },
                ("sess-1", "u-1", "req-1"),
            ),
            (
                {"ENTERPRISE_SESSION_ID": "sess-1", "ENTERPRISE_USER_ID": "u-1"},
                ("sess-1", "u-1", None),
            ),
        ],
        ids=["полный-набор", "без-оборота"],
    )
    def test_environment_becomes_a_context(
        self, monkeypatch: Any, env: dict[str, str], expected: tuple[str, str, str | None]
    ) -> None:
        for name in IDENTITY_ENV:
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        module = _load_skill_client()
        identity = module._identity()
        assert identity is not None
        assert (identity.session_id, identity.user_id, identity.request_id) == expected

    def test_without_environment_identity_is_none(self, monkeypatch: Any) -> None:
        for name in IDENTITY_ENV:
            monkeypatch.delenv(name, raising=False)
        module = _load_skill_client()
        assert module._identity() is None

    def test_partial_environment_is_not_guessed(self, monkeypatch: Any) -> None:
        """Только ``session_id`` — это не идентичность.

        Догадка («user_id равен session_id») создала бы вызов, который выглядит
        в журнале настоящим, но принадлежит никому.
        """
        for name in IDENTITY_ENV:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("ENTERPRISE_SESSION_ID", "sess-1")
        module = _load_skill_client()
        assert module._identity() is None

    def test_chat_passes_identity_to_the_client(self, monkeypatch: Any) -> None:
        """Идентичность доходит до ``complete()``, а не теряется по дороге.

        Проверяется на аргументах вызова: навык мог бы прочитать окружение и
        забыть передать, и тогда сервер отказал бы с ``identity_missing`` без
        всяких следов в коде.
        """
        for name in IDENTITY_ENV:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("ENTERPRISE_SESSION_ID", "sess-1")
        monkeypatch.setenv("ENTERPRISE_USER_ID", "u-1")
        module = _load_skill_client()

        seen: dict[str, Any] = {}

        def fake_complete(messages: Any, **kwargs: Any) -> str:
            seen.update(kwargs)
            return "ответ"

        monkeypatch.setattr(module, "complete", fake_complete)
        module.chat([{"role": "user", "content": "вопрос"}])

        identity = seen.get("identity")
        assert identity is not None, "идентичность не дошла до клиента"
        assert identity.session_id == "sess-1"
        assert identity.user_id == "u-1"
