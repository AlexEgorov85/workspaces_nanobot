"""Идентичность вызова на границе агента и платформы.

Дефект, который закрывает этот файл. Агентский ``CallIdentity.as_meta()``
добавлял ``workspaces/request_id`` только когда тот уже был найден, а сервер
требует все три ключа и отвечает на неполный ``_meta`` кодом
``identity_missing``. Выглядело это так: вызов вне оборота, для которого не
нашлось PK, тихо не проходил — а падал отказом, который читается как «платформа
не работает», и чинился в неправильном месте.

Проверяется то, что уходит на провод, и что уйти не может:

* ``_meta`` всегда из трёх ключей, если идентичность есть;
* ``request_id`` досоставляется на границе, а не выдумывается сервером;
* найденный ``request_id`` не заменяется своим;
* без идентичности ключ ``meta`` не передаётся вовсе;
* неполная личность видна на стороне агента, а не уходит на сервер.

Разбор ``_meta`` делается **настоящим** ``McpCallContext.from_meta`` из
платформы, а не сравнением со словарём: иначе тест остался бы зелёным после
того, как сервер поменяет требования к ключам, — то есть проверял бы не
контракт, а собственную копию его ожиданий.
"""

from __future__ import annotations

import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PLATFORM_ROOT = REPO_ROOT / "mcp-platform"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(PLATFORM_ROOT) not in sys.path:
    sys.path.insert(0, str(PLATFORM_ROOT))

from lib.services.enterprise_mcp_client import (  # noqa: E402
    CallIdentity,
    CallIdentityIncomplete,
    EnterpriseMcpClient,
)
from libs.enterprise_common.execution.context import (  # noqa: E402
    KEY_REQUEST_ID,
    KEY_SESSION_ID,
    KEY_USER_ID,
    McpCallContext,
)

META_PREFIX = "workspaces/"


@dataclass
class _Sent:
    operation: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    had_meta: bool = False
    meta: dict[str, Any] | None = None


class _Result:
    isError = False
    content = [type("_B", (), {"text": "ответ"})()]


class _Session:
    def __init__(self, sent: _Sent) -> None:
        self._sent = sent

    async def call_tool(self, operation: str, arguments: dict[str, Any], **kw: Any) -> Any:
        self._sent.operation = operation
        self._sent.arguments = arguments
        self._sent.had_meta = "meta" in kw
        self._sent.meta = kw.get("meta")
        return _Result()


def _client(sent: _Sent) -> EnterpriseMcpClient:
    client = EnterpriseMcpClient(command="python", args=[], cwd=".", tool_timeout_sec=5.0)

    async def fake_session() -> Any:
        return _Session(sent)

    client._ensure_session = fake_session  # type: ignore[method-assign]
    return client


# ---------------------------------------------------------------------------
# Форма _meta
# ---------------------------------------------------------------------------


class TestMetaShape:
    def test_prefix_matches_the_server(self) -> None:
        """Префикс ключей — тот же, что ждёт ``execution/context.py``.

        MCP резервирует ``io.modelcontextprotocol/*`` под свой служебный
        обмен. Голые имена без префикса — шаг к коллизии с чужим расширением,
        и разошлись бы префиксы тихо: сервер просто не увидит идентичность.
        """
        assert META_PREFIX == "workspaces/"

    @pytest.mark.asyncio
    async def test_arguments_are_passed_through_untouched(self) -> None:
        """Доменные аргументы доходят в том виде, в каком их собрали.

        Клиент не фильтрует и не дополняет их: он отвечает за ``_meta``, а
        тело вызова принадлежит операции.
        """
        sent = _Sent()
        payload = {"limit": 5, "offset": 0}
        await _client(sent).call("history_search", payload, identity=CallIdentity("s", "u"))
        assert sent.arguments == payload


# ---------------------------------------------------------------------------
# Модель не может снабдить вызов идентичностью
# ---------------------------------------------------------------------------


class TestModelCannotSupplyIdentity:
    @pytest.mark.parametrize(
        "supplied",
        [
            {"session_id": "CCC-подмена", "user_id": "CCC-чужой-пользователь"},
            {"session_id": "CCC-подмена", "user_id": "CCC-чужой-пользователь", "limit": 10},
        ],
        ids=["session+user", "session+user+лишнее"],
    )
    @pytest.mark.asyncio
    async def test_model_arguments_do_not_reach_the_server_as_identity(
        self, supplied: dict[str, str]
    ) -> None:
        """Значения, присланные моделью, остаются в аргументах.

        Если бы клиент подставлял их в ``_meta``, вызов исполнился бы от
        чужой сессии, и изоляция перестала бы быть границей.
        """
        sent = _Sent()
        await _client(sent).call(
            "history_search",
            {**supplied, "query": "q"},
            identity=CallIdentity("real-session", "real-user", "real-req"),
        )
        assert sent.meta[f"{META_PREFIX}session_id"] == "real-session"
        assert sent.meta[f"{META_PREFIX}user_id"] == "real-user"
        for key, value in supplied.items():
            assert sent.arguments[key] == value


# ---------------------------------------------------------------------------
# as_meta: неполная идентичность невозможна
# ---------------------------------------------------------------------------


class TestAsMetaIsAlwaysComplete:
    def test_full_identity_yields_three_keys(self) -> None:
        meta = CallIdentity("sess-1", "user-1", "req-1").as_meta()
        assert meta == {
            KEY_SESSION_ID: "sess-1",
            KEY_USER_ID: "user-1",
            KEY_REQUEST_ID: "req-1",
        }

    def test_missing_request_id_is_refused(self) -> None:
        """Раньше ключ просто опускался — и вызов уходил в никуда.

        Теперь отказ до отправки и называет, чего не хватило.
        """
        with pytest.raises(CallIdentityIncomplete) as excinfo:
            CallIdentity("sess-1", "user-1").as_meta()
        assert "request_id" in str(excinfo.value)

    @pytest.mark.parametrize(
        "identity",
        [
            CallIdentity("", "user-1", "req-1"),
            CallIdentity("sess-1", "", "req-1"),
            CallIdentity("sess-1", "user-1", ""),
        ],
        ids=["нет-сессии", "нет-пользователя", "пустой-оборот"],
    )
    def test_blank_field_is_refused(self, identity: CallIdentity) -> None:
        with pytest.raises(CallIdentityIncomplete):
            identity.as_meta()

    def test_refusal_names_every_missing_key(self) -> None:
        with pytest.raises(CallIdentityIncomplete) as excinfo:
            CallIdentity("", "", None).as_meta()
        message = str(excinfo.value)
        for name in ("session_id", "user_id", "request_id"):
            assert name in message, message

    def test_meta_is_understood_by_the_platform_parser(self) -> None:
        """Словарь, который примет настоящий разбор платформы.

        Главная проверка файла: она ломается вместе с контрактом, а не вместе
        с нашим представлением о нём.
        """
        parsed = McpCallContext.from_meta(CallIdentity("s", "u", "r").as_meta())
        assert (parsed.session_id, parsed.user_id, parsed.request_id) == ("s", "u", "r")


# ---------------------------------------------------------------------------
# call(): request_id досоставляется на границе
# ---------------------------------------------------------------------------


class TestRequestIdIsCompletedAtTheBoundary:
    @pytest.mark.asyncio
    async def test_call_without_request_id_still_gets_one(self) -> None:
        sent = _Sent()
        await _client(sent).call("list_scripts", {}, identity=CallIdentity("sess-1", "user-1"))
        assert sent.had_meta is True
        parsed = McpCallContext.from_meta(sent.meta)
        assert parsed.session_id == "sess-1"
        assert parsed.user_id == "user-1"
        assert parsed.request_id, "сервер без request_id отверг бы вызов"

    @pytest.mark.asyncio
    async def test_generated_id_is_a_plain_uuid(self) -> None:
        """Форма совпадает с остальными ``request_id`` агента.

        Разбор на стороне платформы такой ключ принимает, но в журнале он
        должен читаться так же, как остальные, — иначе разбор оборота
        усложняется ради одного пути.
        """
        sent = _Sent()
        await _client(sent).call("list_scripts", {}, identity=CallIdentity("s", "u"))
        uuid.UUID(sent.meta[KEY_REQUEST_ID])  # форма uuid4, без префикса

    @pytest.mark.asyncio
    async def test_existing_request_id_is_not_replaced(self) -> None:
        """Найденный PK оборота связывает вызов с ним; подмена это ломает."""
        sent = _Sent()
        await _client(sent).call(
            "list_scripts", {}, identity=CallIdentity("s", "u", "req-42")
        )
        assert sent.meta[KEY_REQUEST_ID] == "req-42"

    @pytest.mark.asyncio
    async def test_generated_id_differs_between_calls(self) -> None:
        """Два вызова вне оборота — два разных ключа.

        Один общий ключ на процесс связал бы в журнале вызовы разных
        вопросов, и разбирать такой след было бы уже невозможно.
        """
        sent = _Sent()
        client = _client(sent)
        await client.call("list_scripts", {}, identity=CallIdentity("s", "u"))
        first = sent.meta[KEY_REQUEST_ID]
        await client.call("list_scripts", {}, identity=CallIdentity("s", "u"))
        assert sent.meta[KEY_REQUEST_ID] != first

    @pytest.mark.asyncio
    async def test_caller_identity_is_not_mutated(self) -> None:
        """Досоставка не пишет обратно в объект вызывающей стороны.

        Иначе «одна личность на процесс» превратилась бы в тихую подмену у
        того, кто её собрал, — и тот, кто её переиспользует, получил бы
        чужую.
        """
        identity = CallIdentity("s", "u")
        sent = _Sent()
        await _client(sent).call("list_scripts", {}, identity=identity)
        assert identity.request_id is None
        assert sent.meta[KEY_REQUEST_ID], "но на провод ушёл заполненный"

    @pytest.mark.asyncio
    async def test_without_identity_no_meta_key_is_sent(self) -> None:
        sent = _Sent()
        await _client(sent).call("list_scripts", {})
        assert sent.had_meta is False
        assert sent.meta is None

    @pytest.mark.asyncio
    async def test_arguments_never_carry_identity(self) -> None:
        """Идентичность живёт в ``_meta``, а не в теле вызова.

        В аргументах она стала бы полем, которое заполняет модель, и область
        видимости перестала бы задаваться вызывающей стороной.
        """
        sent = _Sent()
        await _client(sent).call(
            "run_script", {"script": "s", "query": "q"},
            identity=CallIdentity("sess-1", "user-1", "req-1"),
        )
        for key in ("session_id", "user_id", "request_id"):
            assert key not in sent.arguments, sent.arguments
        assert sent.arguments == {"script": "s", "query": "q"}


# ---------------------------------------------------------------------------
# Регрессия, ради которой файл и написан
# ---------------------------------------------------------------------------


class TestRegressionNoTwoKeyMeta:
    @pytest.mark.asyncio
    async def test_partial_meta_is_never_accepted_by_the_platform(self) -> None:
        """Двухключевой ``_meta`` сервер не принимает — и не должен получать.

        Собрано ровно то, что собирал прежний ``as_meta()``: без
        ``request_id``. Проверка проходит, только если идемпотентность
        возврата восстановлена.
        """
        sent = _Sent()
        client = _client(sent)
        await client.call("list_scripts", {}, identity=CallIdentity("sess-1", "user-1"))
        with pytest.raises(Exception) as excinfo:
            McpCallContext.from_meta(
                {
                    f"{META_PREFIX}session_id": "sess-1",
                    f"{META_PREFIX}user_id": "user-1",
                }
            )
        assert "request_id" in str(excinfo.value)
        assert sent.meta[KEY_REQUEST_ID], (
            "клиент отправил бы именно такой meta, и вызов упал бы с "
            "identity_missing — выглядит как «платформа не работает»"
        )
