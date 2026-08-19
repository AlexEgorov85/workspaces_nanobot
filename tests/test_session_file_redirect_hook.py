"""Регрессионные тесты SessionFileRedirectHook: write- и media-редирект.

Покрывают сценарий со скрина пользователя: агент создаёт файл через
``write_file`` (redirect в ``data_store/cache/sessions/<key>/``), но в
``message({"media": [...]})`` прикладывает относительный путь или
«абсолютный» путь чужого workspace — ``utils.media.serialize`` не находил
файл (``Media file not found, keeping path``). Хук должен переписать
media-пути в реальные пути session-папки, симметрично write-редиректу.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_WORKSPACE = _PROJECT_ROOT / "workspace"
for p in (str(_PROJECT_ROOT), str(_WORKSPACE)):
    if p not in sys.path:
        sys.path.insert(0, p)

_SESSION_KEY = "postgres:06504481-76e0-4bbd-af48-95bc2b0e4a3d"
_SAFE_KEY = "postgres_06504481-76e0-4bbd-af48-95bc2b0e4a3d"


class _Ctx:
    def __init__(self, session_key: str) -> None:
        self.session_key = session_key


class _ToolCall:
    def __init__(self, name: str, arguments: dict | None = None) -> None:
        self.name = name
        self.arguments = arguments


def _make_hook(workspace_dir: Path):
    from workspace.hooks.session_file_redirect_hook import SessionFileRedirectHook

    return SessionFileRedirectHook(workspace_dir=str(workspace_dir))


def _run_before_execute_tool(hook, session_key, tool_name, params):
    ctx = _Ctx(session_key)
    tc = _ToolCall(tool_name, dict(params))
    asyncio.run(hook.before_execute_tool(ctx, tc, None, params))
    return params, tc


@pytest.fixture()
def session_workspace(tmp_path):
    """Workspace с одним реальным файлом в session-папке (как после write-редиректа)."""
    session_sub = (
        tmp_path
        / "data_store"
        / "cache"
        / "sessions"
        / _SAFE_KEY
    )
    session_sub.mkdir(parents=True)
    (session_sub / "presentation_minimal.html").write_bytes(b"<h1>minimal</h1>")
    (session_sub / "attachments").mkdir()
    (session_sub / "attachments" / "93cf_uuid.png").write_bytes(b"png")
    return tmp_path


def test_media_relative_path_redirects_to_session_file(session_workspace):
    """Относительный путь ``presentation_minimal.html`` → реальный session-путь."""
    hook = _make_hook(session_workspace)
    params, tc = _run_before_execute_tool(
        hook, _SESSION_KEY, "message", {"content": "ok", "media": ["presentation_minimal.html"]}
    )

    expected = str(
        session_workspace / "data_store" / "cache" / "sessions" / _SAFE_KEY / "presentation_minimal.html"
    )
    assert params["media"] == [expected]
    assert tc.arguments["media"] == [expected]


def test_media_stale_absolute_path_redirects_by_basename(session_workspace):
    """«Абсолютный» путь чужого workspace (нет на диске) → замена по basename."""
    hook = _make_hook(session_workspace)
    stale = "/home/datalab/nfs/workspaces_nanobot-release-v2.3.1/workspace/presentation_minimal.html"
    params, _ = _run_before_execute_tool(
        hook, _SESSION_KEY, "message", {"media": [stale]}
    )

    expected = str(
        session_workspace / "data_store" / "cache" / "sessions" / _SAFE_KEY / "presentation_minimal.html"
    )
    assert params["media"] == [expected]


def test_media_attachments_subfolder_resolved(session_workspace):
    """basename из ``attachments/`` сессии тоже находится."""
    hook = _make_hook(session_workspace)
    params, _ = _run_before_execute_tool(
        hook, _SESSION_KEY, "message", {"media": ["93cf_uuid.png"]}
    )

    expected = str(
        session_workspace / "data_store" / "cache" / "sessions" / _SAFE_KEY / "attachments" / "93cf_uuid.png"
    )
    assert params["media"] == [expected]


def test_media_urls_and_data_left_untouched(session_workspace):
    """http/data:-элементы не перенаправляются."""
    hook = _make_hook(session_workspace)
    media = ["https://example.com/a.png", "data:image/png;base64,YWI="]
    params, _ = _run_before_execute_tool(hook, _SESSION_KEY, "message", {"media": media})

    assert params["media"] == media


def test_media_existing_session_relative_path_left(session_workspace):
    """Путь, который уже резолвится в существующий файл, не трогаем."""
    hook = _make_hook(session_workspace)
    existing = f"data_store/cache/sessions/{_SAFE_KEY}/presentation_minimal.html"
    params, _ = _run_before_execute_tool(hook, _SESSION_KEY, "message", {"media": [existing]})

    assert params["media"] == [existing]


def test_media_missing_file_left_for_serialize_warning(session_workspace):
    """Нет файла нигде — оставляем как есть (serialize выдаст warning)."""
    hook = _make_hook(session_workspace)
    missing = "report.docx"
    params, _ = _run_before_execute_tool(hook, _SESSION_KEY, "message", {"media": [missing]})

    assert params["media"] == [missing]


def test_message_without_media_is_noop(session_workspace):
    """message без параметра media не мутируется."""
    hook = _make_hook(session_workspace)
    params, _ = _run_before_execute_tool(hook, _SESSION_KEY, "message", {"content": "hi"})

    assert "media" not in params


def test_write_tools_still_redirected(tmp_path):
    """Write-редирект не сломан: write_file перенаправляет путь в session-папку."""
    hook = _make_hook(tmp_path)
    params, _ = _run_before_execute_tool(
        hook, "cli:1", "write_file", {"path": "report.md"}
    )

    expected = str(tmp_path / "data_store" / "cache" / "sessions" / "cli_1" / "report.md")
    assert params["path"] == expected


def _resolve_via_message_tool(workspace, media):
    """Пропустить media через реальный ``MessageTool._resolve_media`` (как в цикле агента)."""
    from nanobot.agent.tools.message import MessageTool

    mt = MessageTool(workspace=str(workspace), restrict_to_workspace=False)
    return mt._resolve_media(list(media))


def test_e2e_relative_path_reaches_serialize(session_workspace):
    """Сквозной сценарий из лога: write_file (redirect) → message(media=[относительный путь])
    → хук → MessageTool._resolve_media → utils.media.serialize находит файл и кодирует data URL.
    Без фикса serialize писал 'Media file not found, keeping path'."""
    from workspace.utils.media import serialize

    hook = _make_hook(session_workspace)
    params, _ = _run_before_execute_tool(
        hook, _SESSION_KEY, "message", {"content": "ok", "media": ["presentation_minimal.html"]}
    )

    resolved = _resolve_via_message_tool(session_workspace, params["media"])

    expected = str(
        session_workspace / "data_store" / "cache" / "sessions" / _SAFE_KEY / "presentation_minimal.html"
    )
    assert resolved == [expected]

    db_media = serialize(resolved)
    assert len(db_media) == 1
    entry = db_media[0]
    assert entry["file_id"].startswith("data:text/html;base64,")
    assert entry["mime_type"] == "text/html"
    assert entry["file_size"] == len(b"<h1>minimal</h1>")


def test_e2e_stale_absolute_path_reaches_serialize(session_workspace):
    """Тот же путь, но агент передал «абсолютный» путь чужого workspace — хук находит по basename."""
    from workspace.utils.media import serialize

    hook = _make_hook(session_workspace)
    stale = "/home/datalab/nfs/workspaces_nanobot-release-v2.3.1/workspace/presentation_minimal.html"
    params, _ = _run_before_execute_tool(
        hook, _SESSION_KEY, "message", {"media": [stale]}
    )

    resolved = _resolve_via_message_tool(session_workspace, params["media"])

    db_media = serialize(resolved)
    assert len(db_media) == 1
    assert db_media[0]["file_id"].startswith("data:text/html;base64,")
    assert db_media[0]["mime_type"] == "text/html"
    assert db_media[0]["file_size"] > 0


@pytest.fixture()
def allowed_cache_workspace(tmp_path):
    """Workspace с файлом прямо в ``data_store/cache/`` (allowed-write,
    редирект НЕ сработал), но session-папки нет. Воспроизводит live-сценарий,
    где агент пишет ``data_store/cache/report.md`` и прикладывает ``report.md``."""
    allowed = tmp_path / "data_store" / "cache"
    allowed.mkdir(parents=True)
    (allowed / "report.md").write_bytes(b"# report\n\ncontent")
    return tmp_path


def test_media_basename_falls_back_to_allowed_data_store_cache(allowed_cache_workspace):
    """basename из message → поиск в ``data_store/cache/`` (разрешённая
    write-папка), когда session-папки нет. Без этого fallback'а
    ``MessageTool._resolve_media`` мапит ``report.md`` в корень workspace,
    файла там нет, serialize пишет warning «Media file not found»."""
    hook = _make_hook(allowed_cache_workspace)
    params, _ = _run_before_execute_tool(
        hook, "postgres:chat-x", "message", {"media": ["report.md"]}
    )

    expected = str(allowed_cache_workspace / "data_store" / "cache" / "report.md")
    assert params["media"] == [expected]


def test_e2e_data_store_cache_fallback_reaches_serialize(allowed_cache_workspace):
    """Сквозной e2e для fallback'а: hook подменяет basename на реальный путь
    в ``data_store/cache/`` → MessageTool._resolve_media → serialize →
    валидный data URL с mime и size > 0."""
    from workspace.utils.media import serialize

    hook = _make_hook(allowed_cache_workspace)
    params, _ = _run_before_execute_tool(
        hook, "postgres:chat-x", "message", {"media": ["report.md"]}
    )

    resolved = _resolve_via_message_tool(allowed_cache_workspace, params["media"])
    db_media = serialize(resolved)
    assert len(db_media) == 1
    assert db_media[0]["mime_type"] == "text/markdown"
    assert db_media[0]["file_size"] > 0
    assert db_media[0]["file_id"].startswith("data:text/markdown;base64,")
