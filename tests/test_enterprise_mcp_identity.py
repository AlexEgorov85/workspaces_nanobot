"""Идентичность вызова едет в ``params._meta``, а не в аргументы.

Контракт: сервер читает идентичность только из ``_meta``. Если она попадёт
в аргументы, у вызова появятся два источника, а событие в журнале и файл
сессии могут описать разные вызовы. Значение, присланное моделью, границей
изоляции не является — подставляет агент.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from lib.services.enterprise_mcp_client import (
    META_PREFIX,
    CallIdentity,
    EnterpriseMcpClient,
)


class _FakeSession:
    """Сессия, которая запоминает вызов целиком."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], dict[str, str] | None]] = []

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        meta: dict[str, Any] | None = None,
    ) -> Any:
        self.calls.append((name, dict(arguments or {}), meta))
        return type("Result", (), {"content": [type("Text", (), {"text": "ok"})()]})()


def _client_with(session: _FakeSession) -> EnterpriseMcpClient:
    client = EnterpriseMcpClient(command="python", args=[], cwd=".", tool_timeout_sec=5.0)

    async def _ensure() -> _FakeSession:
        return session

    client._ensure_session = _ensure  # type: ignore[method-assign]
    return client


class TestMetaShape:
    def test_prefix_matches_the_server(self) -> None:
        """Префикс обязан совпадать с ``execution/context.py``.

        Расхождение не падает: сервер просто не увидит идентичность и
        ответит ``identity_missing`` — на живом вызове, а не в тесте.
        """
        assert META_PREFIX == "workspaces/"

    def test_identity_is_sent_in_meta(self) -> None:
        identity = CallIdentity(session_id="s-1", user_id="u-1", request_id="r-1")

        meta = identity.as_meta()

        assert meta == {
            "workspaces/session_id": "s-1",
            "workspaces/user_id": "u-1",
            "workspaces/request_id": "r-1",
        }


class TestRequestIdIsOptional:
    def test_missing_request_id_is_not_an_error(self) -> None:
        """Вызов вне оборота — законное состояние: оборота нет, значит и
        ``request_id`` нет. Ключ просто не уходит."""
        meta = CallIdentity(session_id="s-1", user_id="u-1").as_meta()

        assert "workspaces/request_id" not in meta
        assert meta == {"workspaces/session_id": "s-1", "workspaces/user_id": "u-1"}


class TestCallSendsMetaNotArguments:
    def test_identity_never_lands_in_arguments(self) -> None:
        session = _FakeSession()
        client = _client_with(session)

        asyncio.run(
            client.call(
                "history_search",
                {"query": "ошибка"},
                identity=CallIdentity(session_id="s-1", user_id="u-1", request_id="r-1"),
            )
        )

        name, arguments, meta = session.calls[0]
        assert name == "history_search"
        assert arguments == {"query": "ошибка"}
        assert meta is not None
        assert meta["workspaces/session_id"] == "s-1"

    def test_call_without_identity_sends_no_meta(self) -> None:
        """Вызов без идентичности не должен выдавать себя за изолированный:
        сервер решит сам (``identity_missing`` или переходный путь)."""
        session = _FakeSession()
        client = _client_with(session)

        asyncio.run(client.call("schema_check", {}))

        assert session.calls[0][2] is None

    def test_arguments_are_passed_through_untouched(self) -> None:
        session = _FakeSession()
        client = _client_with(session)
        payload = {"limit": 5, "offset": 0}

        asyncio.run(
            client.call("history_search", payload, identity=CallIdentity("s", "u"))
        )

        assert session.calls[0][1] == payload


class TestModelCannotSupplyIdentity:
    @pytest.mark.parametrize(
        "supplied",
        [
            {"session_id": "чужая-сессия"},
            {"user_id": "чужой-пользователь"},
            {"session_id": "чужая-сессия", "user_id": "чужой-пользователь"},
        ],
        ids=["session_id", "user_id", "оба"],
    )
    def test_model_arguments_do_not_reach_the_server_as_identity(
        self, supplied: dict[str, str]
    ) -> None:
        """Даже если модель подставила личность в аргументы, она едет как
        обычные данные вызова, а идентичность вызова — та, что задал агент."""
        session = _FakeSession()
        client = _client_with(session)

        asyncio.run(
            client.call(
                "history_search",
                {**supplied, "query": "ошибка"},
                identity=CallIdentity(session_id="моя-сессия", user_id="мой-user"),
            )
        )

        _name, arguments, meta = session.calls[0]
        assert meta is not None
        assert meta["workspaces/session_id"] == "моя-сессия"
        assert meta["workspaces/user_id"] == "мой-user"
        # в аргументах личность осталась обычным полем и в `_meta` не попала
        for key, value in supplied.items():
            assert arguments[key] == value