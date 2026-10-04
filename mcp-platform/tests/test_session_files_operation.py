"""Каталог файлов сессии: раскладка и операция ``session_files``.

Операция отвечает на вопрос, который до неё нельзя было задать честно: где
лежат файлы, которые создал агент. Пока корни разошлись — агент писал в
``workspace/data_store/sessions``, платформа в ``mcp-platform/.sessions`` —
«где мои файлы» приходилось выводить из содержимого, а это единственный способ
узнать каталог, не зная его имени.

Проверяется не «каталог создан», а три вещи, на которых держится контракт:
повторный вызов ничего не пересоздаёт, ``ensure=false`` не трогает диск, а
вызов без идентичности отказывается конвейером, не дойдя до операции.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
if str(PLATFORM_ROOT) not in sys.path:
    sys.path.insert(0, str(PLATFORM_ROOT))

from libs.enterprise_common.execution.context import (  # noqa: E402
    McpCallContext,
    ToolExecutionContext,
)
from libs.enterprise_common.session.workspace import (  # noqa: E402
    SESSION_SUBDIRS,
    SessionWorkspace,
)
from servers.enterprise.tools.session_files import (  # noqa: E402
    FILES_SUBDIR,
    create_tool,
)


@pytest.fixture()
def workspace(tmp_path: Path) -> SessionWorkspace:
    return SessionWorkspace(tmp_path / "sessions")


def _ctx(session_id: str, request_id: str = "req-1") -> ToolExecutionContext:
    return ToolExecutionContext(
        call=McpCallContext(
            request_id=request_id, session_id=session_id, user_id="u-1"
        ),
        tool_name="session_files",
        capability="session",
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _call(workspace: SessionWorkspace, ctx: ToolExecutionContext, **kwargs: Any) -> dict:
    return json.loads(create_tool(workspace).handler(ctx, **kwargs))


# -- каталог и раскладка --------------------------------------------------------


def test_creates_the_session_dir_with_the_whole_layout(workspace: SessionWorkspace) -> None:
    answer = _call(workspace, _ctx("sess-1"))

    assert answer["created"] is True
    assert answer["session_id"] == "sess-1"
    assert answer["layout"] == list(SESSION_SUBDIRS)
    for name in SESSION_SUBDIRS:
        assert (Path(answer["root"]) / name).is_dir(), f"нет подкаталога {name}"


def test_files_dir_is_the_place_the_agent_writes(workspace: SessionWorkspace) -> None:
    """``files/`` — единственное место записи агента, и оно отличается от
    платформенных подкаталогов только владельцем, а не именем.

    Если бы оно было обычным подкаталогом без объявления, агент клал бы свои
    файлы рядом со снимками оборота и по содержимому каталога пришлось бы угадывать,
    чьё это.
    """
    answer = _call(workspace, _ctx("sess-1"))

    assert answer["layout"][0] == FILES_SUBDIR
    assert Path(answer["files_dir"]) == Path(answer["root"]) / FILES_SUBDIR
    assert Path(answer["files_dir"]).is_dir()


def test_paths_are_absolute_and_come_from_the_workspace(workspace: SessionWorkspace) -> None:
    """Абсолютными и платформенными: агент пишет в ``files/`` своим же файловым
    инструментом, и спрятать путь ему нечем, а ``resolve()`` разошёлся бы с тем
    путём, по которому пишет сама платформа, стоит только появиться симлинку.
    """
    answer = _call(workspace, _ctx("sess-1"))
    expected = workspace.session_dir("sess-1")

    assert answer["root"] == str(expected)
    assert Path(answer["root"]).is_absolute()
    assert Path(answer["files_dir"]).is_absolute()


def test_repeat_call_keeps_the_same_dir(workspace: SessionWorkspace) -> None:
    """Повторный вызов — не второй каталог.

    Операцию вызывают один раз на сессию и кэшируют ответ, но «один раз» не
    гарантия: вызов может повториться при переподключении, и ``created`` обязан
    честно сказать, что каталог был уже.
    """
    first = _call(workspace, _ctx("sess-1"))
    second = _call(workspace, _ctx("sess-1", request_id="req-2"))

    assert first["root"] == second["root"]
    assert first["created"] is True
    assert second["created"] is False


def test_ensure_false_creates_nothing(workspace: SessionWorkspace) -> None:
    """Спросить, где файлы окажутся, ничего не заводя.

    Иначе «просто узнать» стоило бы каталога на диске — и операция, которой
    пользуются ради сверки пути, сама же этот путь и создавала бы.
    """
    answer = _call(workspace, _ctx("sess-1"), ensure=False)

    assert answer["created"] is False
    assert not workspace.root.exists()
    assert answer["root"] == str(workspace.root / "sess-1")


def test_ensure_false_reports_an_existing_dir_without_claiming_creation(
    workspace: SessionWorkspace,
) -> None:
    _call(workspace, _ctx("sess-1"))

    answer = _call(workspace, _ctx("sess-1"), ensure=False)

    assert answer["created"] is False
    assert Path(answer["root"]).is_dir()


def test_sessions_do_not_share_a_dir(workspace: SessionWorkspace) -> None:
    """У каждой сессии свой каталог: общий каталог означал бы, что файлы одной
    сессии видны в другой."""
    first = _call(workspace, _ctx("sess-1"))
    second = _call(workspace, _ctx("sess-2"))

    assert first["root"] != second["root"]


# -- идентичность ---------------------------------------------------------------


def test_session_is_not_an_argument(workspace: SessionWorkspace) -> None:
    """Идентичность приходит из ``_meta``; аргумент ``session_id`` обошёл бы её
    подмену — и агент спросил бы папку чужой сессии и получил бы её.
    """
    definition = create_tool(workspace)

    assert "session_id" not in definition.input_schema["properties"]
    assert "ensure" in definition.input_schema["properties"]


def _wire(transport: Any, name: str, arguments: dict, meta: Any) -> Any:
    import anyio
    from conftest import call_tool

    return anyio.run(call_tool, transport, name, arguments, meta)


def _transport(workspace: SessionWorkspace) -> Any:
    """Провод с одним вызовом: конвейер настоящий, идентичность настоящая."""
    from libs.enterprise_common.loader import build_server
    from libs.enterprise_common.registry import ToolRegistry

    from conftest import make_layer

    registry = ToolRegistry()
    registry.register(create_tool(workspace))
    return build_server(
        registry,
        name="enterprise-mcp",
        pipeline=make_layer(workspace.root).pipeline,
    )


def test_call_without_identity_is_refused_and_creates_nothing(
    workspace: SessionWorkspace,
) -> None:
    """Без ``params._meta`` вызов не доходит до операции.

    Отказ — существующий контракт конвейера (``identity_missing``), а не новый
    код ради этой операции. И каталог при этом не появляется: иначе отказ
    оставлял бы после себя папку безымянного вызова.
    """
    result = _wire(_transport(workspace), "session_files", {}, {})

    assert result.isError is True
    body = json.loads(result.content[0].text)
    assert body["error"]["code"] == "identity_missing"
    assert "files_dir" not in result.content[0].text, (
        "операция не должна запускаться без идентичности"
    )
    assert not (workspace.root / "sess-1").exists()


def test_call_with_identity_answers_on_the_wire(workspace: SessionWorkspace) -> None:
    result = _wire(_transport(workspace), "session_files", {}, None)

    assert result.isError is False
    answer = json.loads(result.content[0].text)
    assert answer["session_id"] == "sess-1"
    assert answer["created"] is True
    assert Path(answer["files_dir"]).is_dir()


# -- регистрация ----------------------------------------------------------------


def test_build_registers_the_operation() -> None:
    from servers.enterprise import server as enterprise_server

    _, registry, _ = enterprise_server.build()

    assert "session_files" in registry.names()
    assert "session_files" in [
        definition.name for definition in registry.by_category()["session"]
    ]


def test_skills_server_does_not_get_session_files() -> None:
    """Подпроцесс ради одной операции LLM: у него нет ни оборота, ни сессии,
    и каталога ей выдавать не из чего."""
    from servers.enterprise import server as enterprise_server

    _, registry, _ = enterprise_server.build(capabilities=["llm"])

    assert "session_files" not in registry.names()


def test_workspace_is_not_in_the_container() -> None:
    """Хранилище достаётся composition root'ом и в контейнер не кладётся.

    Появись оно в контейнере, в нём же оказался бы путь к файлам сессии, и
    дотянуться до него смогла бы любая capability — а страж границы проверяет
    только capability, и такой путь прошёл бы мимо него.
    """
    from servers.enterprise import server as enterprise_server

    _, registry, container = enterprise_server.build()

    assert "session_files" in registry.names()
    assert "workspace" not in container.services
    assert "session_workspace" not in container.services
