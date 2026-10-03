"""Тесты на инструмент ``history_search`` — тонкий адаптер над MCP-операцией.

Change ``enterprise-mcp-platform``, фаза 2.16: поиск по журналу выполняет
владелец данных (capability ``data`` на стороне платформы). Tool больше НЕ
строит SQL — он подставляет личность текущего запроса и вызывает операцию
``history_search`` через MCP-клиент.

Поэтому тесты проверяют две вещи:

  * **аргументы операции** — что именно уходит владельцу данных (включая
    security-границы: ``session_id`` / ``user_id``, ``limit`` / ``offset``);
  * **конверт ответа** — как адаптер превращает страницу операции в ответ
    агенту (``has_more``, ``next_offset``, ``results_truncated``,
    ``payload_truncated``, ``session_scope``, error-конверты).

Механизм мока — фейковый клиент ``_FakeClient``: единственный async-метод
``call(operation, arguments)``, который пишет аргументы вызова в ``calls``
и возвращает заранее заданный JSON-ответ операции.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from lib.services.enterprise_mcp_client import (
    EnterpriseMcpUnavailable,
    EnterpriseOperationError,
)
from workspace.tools.history_search_tool import (
    HistorySearchTool,
    HistorySearchToolConfig,
)

#: Имя операции платформы, которую вызывает адаптер.
_OPERATION = "history_search"

#: Полный набор ключей аргументов операции ``history_search``.
#: Фиксирует контракт: адаптер не добавляет и не теряет ни одного ключа.
_ARGUMENT_KEYS = {
    "session_id",
    "user_id",
    "query",
    "event_type",
    "tool_name",
    "since",
    "until",
    "limit",
    "offset",
}

_SESSION_KEY_PATCH = "workspace.tools.history_search_tool._current_session_key"
_USER_ID_PATCH = "workspace.tools.history_search_tool._current_user_id"


class _FakeClient:
    """Фейковый MCP-клиент с единственным async-методом ``call``.

    Пишет ``(operation, arguments)`` каждого вызова в ``calls``, после чего
    либо возвращает заранее заданный JSON-ответ операции, либо бросает
    заданное исключение (для error-конвертов адаптера).
    """

    def __init__(
        self, *, response: str = "{}", raises: BaseException | None = None
    ) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._response = response
        self._raises = raises

    async def call(self, operation: str, arguments: dict) -> str:
        self.calls.append((operation, arguments))
        if self._raises is not None:
            raise self._raises
        return self._response

    @property
    def operation(self) -> str:
        """Имя операции единственного (или последнего) вызова."""
        assert self.calls, "операция не вызывалась"
        return self.calls[-1][0]

    @property
    def arguments(self) -> dict[str, Any]:
        """Аргументы единственного (или последнего) вызова."""
        assert self.calls, "операция не вызывалась"
        return self.calls[-1][1]


def _op_response(
    hits: list[dict] | None = None,
    *,
    next_offset: int | None = None,
    truncated: bool = False,
) -> str:
    """JSON-ответ операции ``history_search`` в формате платформы."""
    return json.dumps(
        {"hits": hits or [], "next_offset": next_offset, "truncated": truncated},
        ensure_ascii=False,
    )


def _make_tool(
    hits: list[dict] | None = None,
    *,
    next_offset: int | None = None,
    truncated: bool = False,
    config: HistorySearchToolConfig | None = None,
    client: _FakeClient | None = None,
) -> tuple[HistorySearchTool, _FakeClient]:
    """Tool + фейковый клиент, отдающий ``hits`` одной страницей."""
    if client is None:
        client = _FakeClient(
            response=_op_response(hits, next_offset=next_offset, truncated=truncated),
        )
    tool = HistorySearchTool(
        config=config or HistorySearchToolConfig(),
        client=client,
    )
    return tool, client


def _session(session_key: str | None = "s"):
    """Подменить приватный helper личности сессии."""
    return patch(_SESSION_KEY_PATCH, return_value=session_key)


def _user(user_id: str | None = "alice"):
    """Подменить приватный helper личности пользователя."""
    return patch(_USER_ID_PATCH, return_value=user_id)


def _fake_hits() -> list[dict]:
    """Страница из двух событий в формате, который отдаёт платформа."""
    return [
        {
            "id": "2028c04b-3f25-4153-bb3e-067293f594a8",
            "timestamp": "2026-01-01T10:00:00+00:00",
            "event_type": "agent.compacted",
            "name": "compact_context",
            "level": "INFO",
            "summary": "context compacted",
            "payload": {
                "archived_msgs": 5,
                "tokens_before": 1000,
                "tokens_after": 200,
            },
        },
        {
            "id": "9a370996-73ca-4d18-aab7-4a9b718ae204",
            "timestamp": "2026-01-01T09:00:00+00:00",
            "event_type": "agent.completed",
            "name": "run",
            "level": "INFO",
            "summary": "итоговый ответ",
            "payload": {"final_content": "длинный ответ агента"},
        },
    ]


def _rows(n: int, *, event_type: str = "x", payload: dict | None = None) -> list[dict]:
    """``n`` однотипных событий для тестов пагинации/truncation."""
    return [
        {
            "id": f"id-{i}",
            "timestamp": f"t{i}",
            "event_type": event_type,
            "name": "x",
            "level": "INFO",
            "summary": "s",
            "payload": {} if payload is None else payload,
        }
        for i in range(n)
    ]


# ------------------------------------------------------------------ контракт


@pytest.mark.asyncio
async def test_operation_arguments_match_platform_contract() -> None:
    """Ровно один вызов ``history_search`` с фиксированным набором ключей."""
    tool, client = _make_tool()
    with _session("s"):
        await tool.execute(query=None)

    assert len(client.calls) == 1
    assert client.operation == _OPERATION
    assert set(client.arguments) == _ARGUMENT_KEYS


@pytest.mark.asyncio
async def test_default_arguments_are_neutral() -> None:
    """Без фильтров: пустой query, ``None``-фильтры, limit из max_rows, offset 0."""
    tool, client = _make_tool()
    with _session("s"):
        await tool.execute(query=None)

    args = client.arguments
    assert args["query"] == ""
    assert args["event_type"] is None
    assert args["tool_name"] is None
    assert args["since"] is None
    assert args["until"] is None
    assert args["limit"] == HistorySearchToolConfig().max_rows
    assert args["offset"] == 0


@pytest.mark.asyncio
async def test_search_current_session_passes_session_id_to_operation() -> None:
    """scope='current' → в операцию уходит session_id текущего запроса."""
    tool, client = _make_tool(_fake_hits())
    with _session("postgres:123"):
        result = await tool.execute(query="ответ", session_scope="current")

    data = json.loads(result)
    assert data["status"] == "success"
    assert data["count"] == 2
    assert client.operation == _OPERATION
    assert client.arguments["session_id"] == "postgres:123"
    # user_id в scope='current' не участвует — даже если он доступен.
    assert client.arguments["user_id"] is None


@pytest.mark.asyncio
async def test_search_all_scope_passes_user_id_to_operation() -> None:
    """scope='all' → в операцию уходит user_id текущего запроса."""
    tool, client = _make_tool()
    with _user("alice"):
        await tool.execute(query=None, session_scope="all")

    assert client.operation == _OPERATION
    assert client.arguments["user_id"] == "alice"
    # Никакой session_id: scope='all' не сужается до текущей сессии.
    assert client.arguments["session_id"] == ""


@pytest.mark.asyncio
async def test_search_event_type_filter_passed_to_operation() -> None:
    tool, client = _make_tool(_fake_hits())
    with _session("s"):
        await tool.execute(query="", event_type="context_compacted")

    assert client.arguments["event_type"] == "context_compacted"
    assert client.arguments["query"] == ""


@pytest.mark.asyncio
async def test_search_tool_name_filter_passed_to_operation() -> None:
    """Фильтр tool_name уходит отдельным аргументом операции."""
    tool, client = _make_tool()
    with _session("s"):
        await tool.execute(
            query=None,
            event_type="tool_result",
            tool_name="compact_context",
        )

    assert client.arguments["tool_name"] == "compact_context"
    assert client.arguments["event_type"] == "tool_result"


@pytest.mark.asyncio
async def test_search_tool_name_omitted_is_not_sent() -> None:
    """Без tool_name фильтр не подставляется (нет значения по умолчанию)."""
    tool, client = _make_tool()
    with _session("s"):
        await tool.execute(query=None, event_type="tool_result")

    assert client.arguments["tool_name"] is None
    assert "compact_context" not in json.dumps(client.arguments)


@pytest.mark.asyncio
async def test_search_tool_name_combined_with_query() -> None:
    """tool_name + query уходят вместе, каждый своим аргументом."""
    tool, client = _make_tool()
    with _session("s"):
        await tool.execute(
            query="договор",
            event_type="tool_call",
            tool_name="duckdb_query",
        )

    args = client.arguments
    assert args["tool_name"] == "duckdb_query"
    assert args["query"] == "договор"
    assert args["event_type"] == "tool_call"


@pytest.mark.asyncio
async def test_search_time_bounds_passed_to_operation() -> None:
    """since/until доходят до операции без преобразований."""
    tool, client = _make_tool()
    with _session("s"):
        await tool.execute(
            query=None,
            since="2026-01-01T00:00:00+00:00",
            until="2026-02-01T00:00:00+00:00",
        )

    assert client.arguments["since"] == "2026-01-01T00:00:00+00:00"
    assert client.arguments["until"] == "2026-02-01T00:00:00+00:00"


@pytest.mark.asyncio
async def test_search_truncates_long_output() -> None:
    """Большой payload ужимается до ``max_result_chars``, JSON остаётся валидным."""
    big = [{"timestamp": "t", "event_type": "agent.started", "level": "INFO",
            "summary": "s", "payload": {"x": "y" * 10000}}]
    tool, _client = _make_tool(
        big,
        config=HistorySearchToolConfig(max_result_chars=200),
    )
    with _session("s"):
        result = await tool.execute(query=None)
    # исходный JSON был бы ~10030 символов; усечение заметно режет,
    # при этом JSON остаётся валидным (агент должен его распарсить)
    assert len(result) < 1000
    parsed = json.loads(result)
    assert parsed["status"] == "success"


@pytest.mark.asyncio
async def test_search_includes_name_field_in_events() -> None:
    """Поле ``name`` операции попадает в событие ответа."""
    hits = [
        {
            "id": "id-1",
            "timestamp": "2026-01-01T10:00:00+00:00",
            "event_type": "tool.completed",
            "name": "compact_context",
            "level": "INFO",
            "summary": "compact ok",
            "payload": {"status": "ok"},
        }
    ]
    tool, _client = _make_tool(hits)
    with _session("s"):
        result = await tool.execute(query=None)
    parsed = json.loads(result)
    assert parsed["count"] == 1
    ev = parsed["events"][0]
    assert ev["name"] == "compact_context"
    assert ev["event_type"] == "tool.completed"


@pytest.mark.asyncio
async def test_search_returns_event_id_in_each_event() -> None:
    """``id`` операции маппится в ``event_id`` каждого события (ANALYSIS.md, gap №3)."""
    hits = [
        {
            "id": "11111111-2222-3333-4444-555555555555",
            "timestamp": "2026-01-01T10:00:00+00:00",
            "event_type": "tool.completed",
            "name": "compact_context",
            "level": "INFO",
            "summary": "compact ok",
            "payload": {"status": "ok"},
        },
        {
            "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "timestamp": "2026-01-01T09:00:00+00:00",
            "event_type": "agent.completed",
            "name": "run",
            "level": "INFO",
            "summary": "ответ агента",
            "payload": {"final_content": "..."},
        },
    ]
    tool, _client = _make_tool(hits)
    with _session("s"):
        result = await tool.execute(query=None)
    parsed = json.loads(result)
    assert parsed["count"] == 2
    ids = [ev.get("event_id") for ev in parsed["events"]]
    assert "11111111-2222-3333-4444-555555555555" in ids
    assert "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" in ids


@pytest.mark.asyncio
async def test_payload_is_returned_as_json_string() -> None:
    """payload объекта операции сериализуется в JSON-строку (контракт TOOLS.md)."""
    tool, _client = _make_tool(_fake_hits())
    with _session("s"):
        result = await tool.execute(query=None)
    ev = json.loads(result)["events"][0]
    assert isinstance(ev["payload"], str)
    assert json.loads(ev["payload"])["archived_msgs"] == 5


# ----------------------------------------------------------------- пагинация


class TestPagination:
    """``offset`` и ``has_more``/``next_offset`` (D1, D2 change)."""

    @pytest.mark.asyncio
    async def test_schema_includes_offset_with_default_zero(self):
        """JSON-schema ``history_search`` содержит ``offset`` с дефолтом 0."""
        tool = HistorySearchTool(config=HistorySearchToolConfig(), client=_FakeClient())
        params = tool.to_schema()["function"]["parameters"]
        assert "offset" in params["properties"]
        assert params["properties"]["offset"]["type"] == "integer"
        assert params["properties"]["offset"].get("default") == 0
        assert params["properties"]["offset"]["minimum"] == 0

    @pytest.mark.asyncio
    async def test_operation_passes_limit_and_offset_verbatim(self):
        """limit/offset уходят в операцию как есть (без N+1-трюка адаптера)."""
        tool, client = _make_tool()
        with _session("s"):
            await tool.execute(query=None, limit=7, offset=3)

        assert client.arguments["limit"] == 7
        assert client.arguments["offset"] == 3

    @pytest.mark.asyncio
    async def test_limit_capped_by_config_max_rows(self):
        """limit из аргументов модели не может превысить max_rows конфига."""
        tool, client = _make_tool(
            config=HistorySearchToolConfig(max_rows=50),
        )
        with _session("s"):
            await tool.execute(query=None, limit=500)

        assert client.arguments["limit"] == 50

    @pytest.mark.asyncio
    async def test_negative_offset_clamped_to_zero(self):
        """Отрицательный offset не уходит в операцию (схема требует >= 0)."""
        tool, client = _make_tool()
        with _session("s"):
            await tool.execute(query=None, offset=-5)

        assert client.arguments["offset"] == 0

    @pytest.mark.asyncio
    async def test_offset_zero_equivalent_to_default_behavior(self):
        """``offset=0`` (по умолчанию) ведёт себя как раньше."""
        tool, client = _make_tool(_rows(3))
        with _session("s"):
            r1 = await tool.execute(query=None)
            r2 = await tool.execute(query=None, offset=0)
        assert r1 == r2
        assert client.calls[-1][1]["offset"] == 0

    @pytest.mark.asyncio
    async def test_has_more_true_when_next_page_reported(self):
        """Платформа сообщила ``next_offset`` → has_more=true, count=10."""
        tool, _client = _make_tool(_rows(10), next_offset=10)
        with _session("s"):
            result = await tool.execute(query=None, limit=10)
        data = json.loads(result)
        assert data["has_more"] is True
        assert data["count"] == 10
        assert data["next_offset"] == 10
        assert data["results_truncated"] is False

    @pytest.mark.asyncio
    async def test_has_more_true_when_server_truncated(self):
        """``truncated`` от платформы (сервер упёрся в свой предел) →
        has_more=true даже при ``next_offset=null``."""
        tool, _client = _make_tool(_rows(10), truncated=True)
        with _session("s"):
            result = await tool.execute(query=None, limit=10)
        data = json.loads(result)
        assert data["has_more"] is True
        assert data["count"] == 10
        assert data["next_offset"] == 10

    @pytest.mark.asyncio
    async def test_has_more_false_on_last_page(self):
        """Последняя страница: limit=10, offset=20, операция вернула 5 → count=5."""
        tool, _client = _make_tool(_rows(5))
        with _session("s"):
            result = await tool.execute(query=None, limit=10, offset=20)
        data = json.loads(result)
        assert data["has_more"] is False
        assert data["count"] == 5
        assert data["next_offset"] == 25
        assert data["results_truncated"] is False

    @pytest.mark.asyncio
    async def test_has_more_true_when_results_truncated_even_without_db_more(self):
        """Регрессия: платформа не сообщила следующую страницу, но truncation
        адаптера выбросил часть событий → ``has_more=true`` (агенту
        обязательно звать следующую страницу)."""
        tool, _client = _make_tool(
            _rows(10, event_type="tool_call", payload={"k": "v"}),
            config=HistorySearchToolConfig(max_result_chars=300),
        )
        with _session("s"):
            result = await tool.execute(query=None, limit=10)
        data = json.loads(result)
        assert data["results_truncated"] is True
        assert data["has_more"] is True
        assert data["count"] < 10
        # next_offset = offset + count после truncation'а
        assert data["next_offset"] == data["count"]

    @pytest.mark.asyncio
    async def test_next_offset_no_truncation(self):
        """Без truncation: ``next_offset = offset + count``."""
        tool, _client = _make_tool(_rows(10))
        with _session("s"):
            result = await tool.execute(query=None, limit=10)
        data = json.loads(result)
        assert data["count"] == 10
        assert data["next_offset"] == 10

    @pytest.mark.asyncio
    async def test_next_offset_after_truncation(self):
        """С truncation: ``next_offset = 0 + count`` (НЕ ``0 + limit``)."""
        tool, _client = _make_tool(
            _rows(10, event_type="tool_call", payload={"k": "v"}),
            config=HistorySearchToolConfig(max_result_chars=400),
        )
        with _session("s"):
            result = await tool.execute(query=None, limit=10, offset=0)
        data = json.loads(result)
        assert data["results_truncated"] is True
        assert data["count"] < 10
        assert data["next_offset"] == data["count"]

    @pytest.mark.asyncio
    async def test_next_offset_with_offset_arg(self):
        tool, _client = _make_tool(_rows(4))
        with _session("s"):
            result = await tool.execute(query=None, limit=10, offset=10)
        data = json.loads(result)
        assert data["count"] == 4
        assert data["next_offset"] == 14

    @pytest.mark.asyncio
    async def test_empty_result_no_truncation(self):
        """Пустой результат: ``count=0, has_more=false, results_truncated=false``."""
        tool, _client = _make_tool([])
        with _session("s"):
            result = await tool.execute(query=None)
        data = json.loads(result)
        assert data["status"] == "success"
        assert data["count"] == 0
        assert data["has_more"] is False
        assert data["results_truncated"] is False
        assert data["next_offset"] == 0
        assert data["events"] == []


# ---------------------------------------------------------- truncation-флаги


class TestTruncationFlags:
    """``results_truncated`` vs ``payload_truncated`` — независимые флаги."""

    @pytest.mark.asyncio
    async def test_results_truncated_and_deprecated_alias(self):
        """При truncation: ``results_truncated=true`` и ``truncated=true``.
        Без truncation: оба ``false``."""
        tool_trunc, _c = _make_tool(
            _rows(10, event_type="tool_call", payload={"k": "v"}),
            config=HistorySearchToolConfig(max_result_chars=300),
        )
        with _session("s"):
            r_trunc = await tool_trunc.execute(query=None, limit=10)
        data = json.loads(r_trunc)
        assert data["results_truncated"] is True
        assert data["truncated"] is True

        small = [
            {"id": "id-1", "timestamp": "t1", "event_type": "tool.started",
             "name": "x", "level": "INFO", "summary": "s",
             "payload": {"k": "v"}}
        ]
        tool, _client = _make_tool(small)
        with _session("s"):
            r_ok = await tool.execute(query=None)
        data = json.loads(r_ok)
        assert data["results_truncated"] is False
        assert data["truncated"] is False

    @pytest.mark.asyncio
    async def test_payload_truncated_per_event(self):
        """payload > per_event_cap → ``payload_truncated=true`` только на этом событии."""
        big = "x" * 5000
        hits = [
            {"id": "big", "timestamp": "t1", "event_type": "llm.requested",
             "name": "llm", "level": "INFO", "summary": "s",
             "payload": {"prompt": big}},
            {"id": "small", "timestamp": "t2", "event_type": "tool.started",
             "name": "x", "level": "INFO", "summary": "s",
             "payload": {"k": "ok"}},
        ]
        tool, _client = _make_tool(hits)
        with _session("s"):
            result = await tool.execute(query=None, limit=10)
        data = json.loads(result)
        assert data["count"] == 2
        assert data["results_truncated"] is False
        by_id = {ev["event_id"]: ev for ev in data["events"]}
        assert by_id["big"]["payload_truncated"] is True
        assert by_id["small"]["payload_truncated"] is False

    @pytest.mark.asyncio
    async def test_results_and_payload_independent(self):
        """payload_truncated=true, results_truncated=false (payload ужат, события не выброшены)."""
        big = "x" * 5000
        hits = [
            {"id": "big", "timestamp": "t1", "event_type": "llm.requested",
             "name": "llm", "level": "INFO", "summary": "s",
             "payload": {"prompt": big}},
        ]
        tool, _client = _make_tool(hits)
        with _session("s"):
            result = await tool.execute(query=None, limit=10)
        data = json.loads(result)
        assert data["count"] == 1
        assert data["results_truncated"] is False
        assert data["events"][0]["payload_truncated"] is True

    @pytest.mark.asyncio
    async def test_payload_truncated_via_max_result_chars_second_pass(self):
        """Регрессия: один event с большим payload, max_result_chars настолько
        мал, что срабатывает второй проход (cap //= 2). Событие остаётся,
        payload_truncated=true, results_truncated=false."""
        big = "x" * 5000  # > per_event_cap (4000)
        hits = [
            {"id": "only", "timestamp": "t1", "event_type": "llm.requested",
             "name": "llm", "level": "INFO", "summary": "s",
             "payload": {"prompt": big}},
        ]
        # max_result_chars достаточно мал, чтобы один пережатый payload
        # всё ещё не влез — должно сработать уменьшение cap и пережим.
        tool, _client = _make_tool(
            hits,
            config=HistorySearchToolConfig(max_result_chars=400),
        )
        with _session("s"):
            result = await tool.execute(query=None, limit=10)
        data = json.loads(result)
        assert data["count"] == 1
        assert data["results_truncated"] is False
        assert data["events"][0]["payload_truncated"] is True

    @pytest.mark.asyncio
    async def test_truncated_alias_equals_results_truncated(self):
        """Deprecated ``truncated`` всегда равен ``results_truncated``."""
        tool, _client = _make_tool(
            _rows(10, event_type="tool_call", payload={"k": "v"}),
            config=HistorySearchToolConfig(max_result_chars=300),
        )
        with _session("s"):
            result = await tool.execute(query=None, limit=10)
        data = json.loads(result)
        assert data["truncated"] == data["results_truncated"]

    @pytest.mark.asyncio
    async def test_final_json_respects_max_result_chars(self):
        """Регрессия на пункт 5 review: ``_render()`` должен включать
        ``payload_truncated`` в проверяемый JSON, иначе финальный ответ
        превысит ``max_result_chars`` на ~16-20 байт за счёт
        дополнительного поля ``\"payload_truncated\": false`` на каждое
        событие.

        Сценарий: 10 событий с маленькими payload (~120 байт каждое).
        Без ``payload_truncated`` в ``_render`` суммарный JSON был бы
        ~1200 байт. С ``payload_truncated`` — ~1240 байт (10 ×
        ``"payload_truncated": false,`` ≈ 28 байт на событие).
        Тест проверяет, что финальный JSON строго ≤ ``max_result_chars``
        на разумных порогах (400+; меньше — невалидный сценарий
        из-за структурного overhead'а ~310 байт на 1 событие).
        """
        hits = _rows(10, event_type="tool_call", payload={"k": "v"})
        for max_chars in (400, 800, 1200, 2000):
            tool, _client = _make_tool(
                hits,
                config=HistorySearchToolConfig(max_result_chars=max_chars),
            )
            with _session("s"):
                result = await tool.execute(query=None, limit=10)
            assert len(result) <= max_chars, (
                f"len(result)={len(result)} > max_result_chars={max_chars} "
                "(payload_truncated учтён в _render)"
            )


# ------------------------------------------------------------ error-конверты


class TestErrorEnvelopes:
    """Ошибки клиента/операции не должны показывать модели traceback."""

    @pytest.mark.asyncio
    async def test_client_none_returns_mcp_unavailable(self):
        """Клиент не создан (раздел enterprise_mcp выключен) → структурная ошибка."""
        tool = HistorySearchTool(config=HistorySearchToolConfig(), client=None)
        with _session("s"):
            result = await tool.execute(query=None)
        data = json.loads(result)
        assert data["status"] == "error"
        assert data["error_type"] == "mcp_unavailable"
        assert "mcp" in data["message"].lower()

    @pytest.mark.asyncio
    async def test_operation_error_maps_code_to_error_type(self):
        """Доменная ошибка операции: ``error_type`` = код из исключения."""
        client = _FakeClient(
            raises=EnterpriseOperationError("invalid_arguments", "limit must be int"),
        )
        tool, _c = _make_tool(client=client)
        with _session("s"):
            result = await tool.execute(query=None)
        data = json.loads(result)
        assert data["status"] == "error"
        assert data["error_type"] == "invalid_arguments"
        assert "limit must be int" in data["message"]

    @pytest.mark.asyncio
    async def test_mcp_unavailable_maps_to_mcp_unavailable(self):
        """Сервер не поднялся/не ответил → ``mcp_unavailable``, без traceback."""
        client = _FakeClient(
            raises=EnterpriseMcpUnavailable("enterprise-mcp не ответил за 30s"),
        )
        tool, _c = _make_tool(client=client)
        with _session("s"):
            result = await tool.execute(query=None)
        data = json.loads(result)
        assert data["status"] == "error"
        assert data["error_type"] == "mcp_unavailable"

    @pytest.mark.asyncio
    async def test_unexpected_error_does_not_leak_to_model(self):
        """Неизвестное исключение клиента → ``unexpected_error``, стек не возвращается."""
        client = _FakeClient(raises=ValueError("boom"))
        tool, _c = _make_tool(client=client)
        with _session("s"):
            result = await tool.execute(query=None)
        data = json.loads(result)
        assert data["status"] == "error"
        assert data["error_type"] == "unexpected_error"
        assert "Traceback" not in result

    @pytest.mark.asyncio
    async def test_unparsable_response_is_protocol_error(self):
        """Нечитаемый ответ операции → ``protocol_error`` (а не success с мусором)."""
        client = _FakeClient(response="<not json>")
        tool, _c = _make_tool(client=client)
        with _session("s"):
            result = await tool.execute(query=None)
        data = json.loads(result)
        assert data["status"] == "error"
        assert data["error_type"] == "protocol_error"


# --------------------------------------------------------------- изоляция


@pytest.mark.skip(
    reason="Out of scope for 0.3.5 upgrade, tracked in ISSUE-NB035-4",
)
class TestUserIsolation:
    """Cross-user isolation для ``history_search(session_scope="all")``.

    Закрывает security gap: раньше ``scope='all'`` возвращал глобальный
    набор событий — alice видела события bob, c, …. Теперь ``scope='all'``
    ограничивает выборку ``user_id`` (security boundary), и этот
    ``user_id`` подставляет адаптер, а не модель.
    """

    @pytest.mark.asyncio
    async def test_all_scope_filters_by_user_id_alice(self):
        """alice запрашивает scope='all' → в операцию уходит ``user_id='alice'``."""
        tool, client = _make_tool()
        with _user("alice"):
            await tool.execute(query=None, session_scope="all")
        assert client.arguments["user_id"] == "alice"

    @pytest.mark.asyncio
    async def test_all_scope_excludes_other_users(self):
        """bob запрашивает scope='all' → уходит ``user_id='bob'``, никакого 'alice'."""
        tool, client = _make_tool()
        with _user("bob"):
            await tool.execute(query=None, session_scope="all")
        assert client.arguments["user_id"] == "bob"
        assert "alice" not in json.dumps(client.arguments)

    @pytest.mark.asyncio
    async def test_all_scope_missing_user_returns_error(self):
        """Без identity → ``missing_user_identity``, операция НЕ вызывается."""
        tool, client = _make_tool()
        with _user(None):
            result = await tool.execute(query=None, session_scope="all")
        data = json.loads(result)
        assert data["status"] == "error"
        assert data["error_type"] == "missing_user_identity"
        assert client.calls == []

    @pytest.mark.asyncio
    async def test_current_scope_filters_by_session_id(self):
        """Регрессия: ``scope='current'`` сохраняет поведение по session_id."""
        tool, client = _make_tool()
        with _session("telegram:42"):
            await tool.execute(query=None, session_scope="current")
        assert client.arguments["session_id"] == "telegram:42"

    @pytest.mark.asyncio
    async def test_current_scope_missing_session_returns_error(self):
        """Без identity для session_key → ``missing_session_identity``."""
        tool, client = _make_tool()
        with _session(None):
            result = await tool.execute(query=None, session_scope="current")
        data = json.loads(result)
        assert data["status"] == "error"
        assert data["error_type"] == "missing_session_identity"
        assert client.calls == []

    @pytest.mark.asyncio
    async def test_response_does_not_leak_user_id(self):
        """Ни на одном уровне JSON-ответа нет поля ``user_id``."""
        hits = [
            {
                "id": "id-1",
                "timestamp": "t1",
                "event_type": "tool.started",
                "name": "x",
                "level": "INFO",
                "summary": "s",
                "payload": {"k": "v"},
            }
        ]
        tool, _client = _make_tool(hits)
        with _user("alice"):
            result = await tool.execute(query=None, session_scope="all")
        data = json.loads(result)
        # Корень
        assert "user_id" not in data
        # События
        for ev in data["events"]:
            assert "user_id" not in ev
            # payload внутри события — JSON-string, проверяем строкой
            assert "user_id" not in ev["payload"]

    @pytest.mark.asyncio
    async def test_current_scope_does_not_pass_user_id(self):
        """scope='current' ограничивает выборку по session_id; ``user_id``
        в аргументы не попадает вообще (даже если для строки той же сессии
        записан ошибочный ``user_id``). Это контракт из спеки §3."""
        tool, client = _make_tool()
        with _session("telegram:42"):
            await tool.execute(query=None, session_scope="current")
        assert client.arguments["user_id"] is None

    @pytest.mark.asyncio
    async def test_invalid_session_scope_returns_error(self):
        """Невалидный ``session_scope`` → ``invalid_session_scope``."""
        tool, client = _make_tool()
        result = await tool.execute(query=None, session_scope="bogus")
        data = json.loads(result)
        assert data["status"] == "error"
        assert data["error_type"] == "invalid_session_scope"
        assert client.calls == []


@pytest.mark.skip(
    reason="Out of scope for 0.3.5 upgrade, tracked in ISSUE-NB035-4",
)
class TestOperationArgumentsGuard:
    """Primary guard на аргументы операции (security boundary).

    Защита от регрессии: tool не строит SQL, поэтому «форма запроса» больше
    не выражается текстом — она выражается набором аргументов операции.
    Эти тесты фиксируют фактические аргументы и то, что выборка ВСЕГДА
    ограничена ровно одной личностью: не unscoped, не «обе сразу».
    """

    @pytest.mark.asyncio
    async def test_scope_current_uses_session_id_predicate(self):
        """scope='current' → session_id = session_key, user_id = None."""
        tool, client = _make_tool()
        with _session("telegram:42"):
            await tool.execute(query=None, session_scope="current")
        assert client.operation == _OPERATION
        assert client.arguments["session_id"] == "telegram:42"
        assert client.arguments["user_id"] is None

    @pytest.mark.asyncio
    async def test_scope_all_uses_user_id_predicate(self):
        """scope='all' → user_id = sender_id, session_id пустой (никакого
        unscoped fallback'а и никакого сужения до своей сессии)."""
        tool, client = _make_tool()
        with _user("alice"):
            await tool.execute(query=None, session_scope="all")
        assert client.operation == _OPERATION
        assert client.arguments["user_id"] == "alice"
        assert client.arguments["session_id"] == ""

    @pytest.mark.asyncio
    async def test_scope_all_missing_user_does_not_call_operation(self):
        """scope='all' без identity → ``missing_user_identity``, вызова нет."""
        tool, client = _make_tool()
        with _user(None):
            result = await tool.execute(query=None, session_scope="all")
        assert json.loads(result)["error_type"] == "missing_user_identity"
        assert client.calls == []

    @pytest.mark.asyncio
    async def test_scope_current_missing_session_does_not_call_operation(self):
        """scope='current' без session_key → ``missing_session_identity``,
        вызова нет."""
        tool, client = _make_tool()
        with _session(None):
            result = await tool.execute(query=None, session_scope="current")
        assert json.loads(result)["error_type"] == "missing_session_identity"
        assert client.calls == []

    @pytest.mark.asyncio
    async def test_no_unscoped_operation_arguments(self):
        """Primary guard: выборка ВСЕГДА ограничена ровно одной личностью —
        либо непустой ``user_id`` с пустым ``session_id`` (scope='all'),
        либо непустой ``session_id`` с ``user_id is None`` (scope='current').
        Обе личности пустыми быть не могут: это был бы unscoped-вызов."""
        tool, client = _make_tool()
        with _user("alice"):
            await tool.execute(query=None, session_scope="all")
        args = client.arguments
        assert bool(args["user_id"]) and not args["session_id"]

        tool, client = _make_tool()
        with _session("telegram:42"):
            await tool.execute(query=None, session_scope="current")
        args = client.arguments
        assert bool(args["session_id"]) and args["user_id"] is None

    @pytest.mark.asyncio
    async def test_user_id_is_not_taken_from_tool_arguments(self):
        """Личность подставляет адаптер: даже если модель подсунула
        ``user_id``/``session_id`` в аргументы, они игнорируются —
        в операцию уходит только identity текущего запроса."""
        tool, client = _make_tool()
        with _session("postgres:123"):
            await tool.execute(
                query=None,
                user_id="attacker",
                session_id="postgres:999",
            )
        assert client.arguments["user_id"] is None
        assert client.arguments["session_id"] == "postgres:123"

    @pytest.mark.asyncio
    async def test_tool_schema_has_no_user_id_parameter(self):
        """JSON-schema tool'а НЕ содержит ``user_id`` (security attribute
        не должен быть параметром)."""
        tool, _client = _make_tool()
        params = tool.to_schema()["function"]["parameters"]
        assert "user_id" not in params["properties"]
        assert "user_id" not in params.get("required", [])
        assert "session_id" not in params["properties"]


@pytest.mark.skip(
    reason="Out of scope for 0.3.5 upgrade, tracked in ISSUE-NB035-4",
)
class TestSnapshotConsistency:
    """Пагинация offset-ом без cursor'а — документируем ограничение.

    Сортировка и выборка строк переехали к владельцу данных; адаптер
    прозрачен: он передаёт ``offset`` как есть и возвращает события
    операции в том же порядке, без собственной пересортировки. Поэтому
    между страницами может «съехать» новое событие, попавшее в начало
    выборки (DESC) после первого вызова, — адаптер не делает вид, что
    пагинация snapshot-стабильна, и не вводит cursor'а.
    """

    @pytest.mark.asyncio
    async def test_pagination_is_offset_based_and_delegated(self):
        """offset уходит в операцию дословно, адаптер не пересортировывает."""
        first = [
            {"id": f"id-{i}", "timestamp": f"2026-01-01T10:0{i}:00+00:00",
             "event_type": "agent.started", "name": "x", "level": "INFO",
             "summary": "s",
             "payload": {}}
            for i in range(5)
        ]
        tool, client = _make_tool(first, next_offset=5)
        with _session("s"):
            r1 = await tool.execute(query=None, limit=5, offset=0)
        data1 = json.loads(r1)
        ids1 = [ev["event_id"] for ev in data1["events"]]
        assert ids1 == ["id-0", "id-1", "id-2", "id-3", "id-4"]
        assert client.arguments["offset"] == 0

        # Между страницами появилось новое событие — на второй странице
        # (offset=5) его уже нет, сдвинулась сама платформа, не адаптер.
        second = [
            {"id": f"id-{i}", "timestamp": f"2026-01-01T10:0{i}:00+00:00",
             "event_type": "agent.started", "name": "x", "level": "INFO",
             "summary": "s",
             "payload": {}}
            for i in range(5, 8)
        ]
        tool, client = _make_tool(second)
        with _session("s"):
            r2 = await tool.execute(query=None, limit=5, offset=5)
        data2 = json.loads(r2)
        ids2 = [ev["event_id"] for ev in data2["events"]]
        assert ids2 == ["id-5", "id-6", "id-7"]
        # offset передан дословно, ответ — то, что вернула операция
        assert client.arguments["offset"] == 5
        assert data2["next_offset"] == 8
        assert data2["has_more"] is False
