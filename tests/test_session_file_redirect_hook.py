"""Тесты SessionFileRedirectHook: запись только в ``files/``, отказ вместо «куда-нибудь».

Покрывают разделение, которое ввела фаза 3 предложения
``2026-10-03-session-files``:

* создание (``write``/``create_file``/``write_file``) — **всегда** в ``files/``
  каталога сессии, с сохранённой структурой пути и без исключений по белому
  списку: иначе модель создала бы файл сессии вне каталога сессии;
* правка (``edit``) — путь не переписывается вовсе, а отвечает инструмент;
* недоступный резолвер — отказ с названной причиной, а не запись «как есть»;
* поиск вложения для ``message`` — ``files/``, ``files/attachments/``,
  ``files/results/``, и больше ничего.

Каталог сессии в тестах — от настоящего резолвера: хук берёт путь у него, и
подмена «резолвера заглушкой» проверила бы заглушку, а не хук.
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
    def __init__(self, session_key: str | None) -> None:
        self.session_key = session_key
        self.metadata = {}


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


def _install_resolver(workspace_dir: Path, enterprise_mcp=None):
    """Опубликовать резолвер процесса — так же, как это делает composition root."""
    from lib.services.session_files import SessionFileResolver, install_session_file_resolver

    install_session_file_resolver(
        SessionFileResolver(enterprise_mcp=enterprise_mcp, workspace_dir=workspace_dir)
    )


def _files_dir(workspace_dir: Path) -> Path:
    return workspace_dir / "data_store" / "sessions" / _SAFE_KEY / "files"


@pytest.fixture(autouse=True)
def _no_leaked_resolver():
    """Публикация резолвера — глобальная для процесса: после теста её снимаем.

    Иначе резолвер из одного теста уехал бы в следующий файл прогона и тот
    проверял бы чужой workspace, а не свой.
    """
    yield
    from lib.services.session_files import install_session_file_resolver

    install_session_file_resolver(None)


@pytest.fixture()
def session_workspace(tmp_path):
    """Workspace с готовым каталогом сессии: как после записи агента."""
    files_dir = _files_dir(tmp_path)
    (files_dir / "attachments").mkdir(parents=True)
    (files_dir / "results").mkdir(parents=True)
    (files_dir / "presentation_minimal.html").write_bytes(b"<h1>minimal</h1>")
    (files_dir / "attachments" / "93cf_uuid.png").write_bytes(b"png")
    (files_dir / "results" / "q3.json").write_bytes(b"{}")
    _install_resolver(tmp_path)
    return tmp_path


# ----------------------------------------------------------------------
# Создание: всегда в files/, структура пути сохраняется
# ----------------------------------------------------------------------


@pytest.mark.parametrize("tool", ["write", "create_file", "write_file"])
def test_create_tools_land_in_files_dir(tmp_path, tool):
    """Все три инструмента создания пишут в ``files/`` сессии."""
    _install_resolver(tmp_path)
    hook = _make_hook(tmp_path)
    params, tc = _run_before_execute_tool(hook, "cli:1", tool, {"path": "report.md"})

    expected = str(tmp_path / "data_store" / "sessions" / "cli_1" / "files" / "report.md")
    assert params["path"] == expected
    assert tc.arguments["path"] == expected
    assert Path(expected).parent.is_dir(), "каталог записи должен быть создан"


def test_relative_structure_is_preserved(tmp_path):
    """``lib/services/new_module.py`` → ``files/lib/services/new_module.py``."""
    _install_resolver(tmp_path)
    hook = _make_hook(tmp_path)
    params, _ = _run_before_execute_tool(
        hook, "cli:1", "write_file", {"path": "lib/services/new_module.py"}
    )

    expected = str(
        tmp_path
        / "data_store" / "sessions" / "cli_1" / "files"
        / "lib" / "services" / "new_module.py"
    )
    assert params["path"] == expected


def test_relative_structure_of_nested_dirs(tmp_path):
    """``report/2026/q3.md`` → ``files/report/2026/q3.md``."""
    _install_resolver(tmp_path)
    hook = _make_hook(tmp_path)
    params, _ = _run_before_execute_tool(
        hook, "cli:1", "write_file", {"path": "report/2026/q3.md"}
    )

    expected = str(
        tmp_path / "data_store" / "sessions" / "cli_1" / "files"
        / "report" / "2026" / "q3.md"
    )
    assert params["path"] == expected


@pytest.mark.parametrize(
    "path",
    [
        "AGENTS.md",           # белый список: раньше редиректа не было
        "sql/query.sql",       # белый список: префикс
        "data_store/cache/report.md",  # раньше write-папка агента
        "lib/services/mod.py",  # каталог проекта
    ],
)
def test_whitelist_does_not_exempt_creation(tmp_path, path):
    """Белый список — только про ``edit``: создание уходит в ``files/`` всегда."""
    _install_resolver(tmp_path)
    hook = _make_hook(tmp_path)
    params, _ = _run_before_execute_tool(hook, "cli:1", "write_file", {"path": path})

    assert params["path"].startswith(
        str(tmp_path / "data_store" / "sessions" / "cli_1" / "files")
    )
    assert "data_store" in params["path"]  # внутри files/, а не в прежней write-папке
    assert not (tmp_path / path).exists(), "в репозитории не должно появиться нового файла"


def test_collision_gets_suffix(tmp_path):
    """Существующий файл не перезаписывается: добавляется суффикс."""
    _install_resolver(tmp_path)
    hook = _make_hook(tmp_path)
    files_dir = tmp_path / "data_store" / "sessions" / "cli_1" / "files"
    files_dir.mkdir(parents=True)
    (files_dir / "report.md").write_bytes(b"# already there")

    params, _ = _run_before_execute_tool(hook, "cli:1", "write", {"path": "report.md"})

    assert Path(params["path"]).name == "report__1.md"


def test_dotdot_does_not_escape_files_dir(tmp_path):
    """``..`` отбрасывается: запись не поднимается над каталогом сессии."""
    _install_resolver(tmp_path)
    hook = _make_hook(tmp_path)
    params, _ = _run_before_execute_tool(
        hook, "cli:1", "write_file", {"path": "../../escape.md"}
    )

    files_dir = tmp_path / "data_store" / "sessions" / "cli_1" / "files"
    assert Path(params["path"]) == files_dir / "escape.md"


def test_windows_style_path_is_accepted(tmp_path):
    """Обратные слэши разбираются так же, как прямые."""
    _install_resolver(tmp_path)
    hook = _make_hook(tmp_path)
    params, _ = _run_before_execute_tool(
        hook, "cli:1", "write_file", {"path": r"report\2026\q3.md"}
    )

    expected = str(
        tmp_path / "data_store" / "sessions" / "cli_1" / "files"
        / "report" / "2026" / "q3.md"
    )
    assert params["path"] == expected


def test_create_without_path_is_noop(tmp_path):
    """Нет пути — нечего перенаправлять."""
    _install_resolver(tmp_path)
    hook = _make_hook(tmp_path)
    params, _ = _run_before_execute_tool(hook, "cli:1", "write_file", {"content": "x"})

    assert params == {"content": "x"}


# ----------------------------------------------------------------------
# Правка: путь не переписывается
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "lib/services/context_compaction.py",  # файл проекта из сценария спеки
        "lib/services/new_module.py",          # файла нет — отвечает инструмент
        "report.md",                           # не белый, не проект
    ],
)
def test_edit_leaves_path_untouched(tmp_path, path):
    """``edit`` — работа с репозиторием; путь не трогаем в любом случае."""
    _install_resolver(tmp_path)
    hook = _make_hook(tmp_path)
    params, tc = _run_before_execute_tool(hook, "cli:1", "edit", {"path": path})

    assert params["path"] == path
    assert tc.arguments["path"] == path
    assert not (tmp_path / "data_store").exists(), (
        "правка не должна создавать каталог сессии"
    )


# ----------------------------------------------------------------------
# Отказ вместо записи «как есть»
# ----------------------------------------------------------------------


def test_missing_session_key_is_refused(tmp_path):
    """Оборот без ``session_key`` — отказ, а не каталог служебного имени."""
    from workspace.hooks.session_file_redirect_hook import SessionFileRedirectBlocked

    _install_resolver(tmp_path)
    hook = _make_hook(tmp_path)

    with pytest.raises(SessionFileRedirectBlocked) as excinfo:
        _run_before_execute_tool(hook, None, "write_file", {"path": "report.md"})

    assert "session_key" in str(excinfo.value)
    assert not (tmp_path / "data_store" / "sessions" / "__nosession__").exists()


def test_missing_resolver_is_refused(tmp_path):
    """Резолвер не опубликован — запись отменена с названной причиной."""
    from lib.services.session_files import install_session_file_resolver
    from workspace.hooks.session_file_redirect_hook import SessionFileRedirectBlocked

    install_session_file_resolver(None)
    hook = _make_hook(tmp_path)

    with pytest.raises(SessionFileRedirectBlocked) as excinfo:
        _run_before_execute_tool(hook, "cli:1", "write_file", {"path": "report.md"})

    assert "резолвер" in str(excinfo.value)
    assert not (tmp_path / "report.md").exists(), (
        "молчаливый возврат к записи «как есть» вернул бы дефект, который фаза убирает"
    )


def test_unavailable_session_dir_is_refused(tmp_path):
    """Платформа не отдала каталог — отказ, а не запись в произвольный каталог."""

    class _Boom:
        async def session_files(self):
            raise RuntimeError("платформа молчит")

    from workspace.hooks.session_file_redirect_hook import SessionFileRedirectBlocked

    _install_resolver(tmp_path, enterprise_mcp=_Boom())
    hook = _make_hook(tmp_path)

    with pytest.raises(SessionFileRedirectBlocked) as excinfo:
        _run_before_execute_tool(hook, "cli:1", "write_file", {"path": "report.md"})

    assert "недоступен" in str(excinfo.value)


def test_unusable_session_key_is_refused(tmp_path):
    """Ключ, пригодный только для служебного имени, — отказ, а не псевдосессия."""
    from workspace.hooks.session_file_redirect_hook import SessionFileRedirectBlocked

    _install_resolver(tmp_path)
    hook = _make_hook(tmp_path)

    with pytest.raises(SessionFileRedirectBlocked):
        _run_before_execute_tool(hook, "a/b", "write_file", {"path": "report.md"})

    assert not (tmp_path / "data_store" / "sessions" / "a_b").exists()


def test_refusal_is_converted_by_the_existing_patch(tmp_path):
    """Отказ обязан быть подклассом ``RepeatGuardBlocked``.

    Превращать его в синтетический результат инструмента умеет уже
    существующий патч ``repeat_guard_block``: он ловит ``RepeatGuardBlocked``.
    Новый тип отказа патч бы не поймал, и вместо ответа модели ушёл бы обрыв
    оборота — то есть тихо хуже, чем отказ.
    """
    from lib.hooks.repeat_guard_hook import RepeatGuardBlocked
    from workspace.hooks.session_file_redirect_hook import SessionFileRedirectBlocked

    assert issubclass(SessionFileRedirectBlocked, RepeatGuardBlocked)


def test_hook_raises_rather_than_being_swallowed(tmp_path):
    """Диспетчер хуков глотает исключения без ``reraise`` — отказ обязан дойти."""
    _install_resolver(tmp_path)
    hook = _make_hook(tmp_path)
    assert hook._reraise is True


# ----------------------------------------------------------------------
# Поиск вложений для message
# ----------------------------------------------------------------------


def test_media_relative_path_redirects_to_session_file(session_workspace):
    """Относительный путь ``presentation_minimal.html`` → реальный путь сессии."""
    hook = _make_hook(session_workspace)
    params, tc = _run_before_execute_tool(
        hook, _SESSION_KEY, "message", {"content": "ok", "media": ["presentation_minimal.html"]}
    )

    expected = str(_files_dir(session_workspace) / "presentation_minimal.html")
    assert params["media"] == [expected]
    assert tc.arguments["media"] == [expected]


def test_media_stale_absolute_path_redirects_by_basename(session_workspace):
    """«Абсолютный» путь чужого workspace (нет на диске) → замена по basename."""
    hook = _make_hook(session_workspace)
    stale = "/home/datalab/nfs/workspaces_nanobot-release-v2.3.1/workspace/presentation_minimal.html"
    params, _ = _run_before_execute_tool(hook, _SESSION_KEY, "message", {"media": [stale]})

    assert params["media"] == [str(_files_dir(session_workspace) / "presentation_minimal.html")]


def test_media_attachments_subfolder_resolved(session_workspace):
    """basename из ``files/attachments/`` находится."""
    hook = _make_hook(session_workspace)
    params, _ = _run_before_execute_tool(hook, _SESSION_KEY, "message", {"media": ["93cf_uuid.png"]})

    assert params["media"] == [str(_files_dir(session_workspace) / "attachments" / "93cf_uuid.png")]


def test_media_results_subfolder_resolved(session_workspace):
    """basename из ``files/results/`` находится."""
    hook = _make_hook(session_workspace)
    params, _ = _run_before_execute_tool(hook, _SESSION_KEY, "message", {"media": ["q3.json"]})

    assert params["media"] == [str(_files_dir(session_workspace) / "results" / "q3.json")]


def test_media_urls_and_data_left_untouched(session_workspace):
    """http/data:-элементы не перенаправляются."""
    hook = _make_hook(session_workspace)
    media = ["https://example.com/a.png", "data:image/png;base64,YWI="]
    params, _ = _run_before_execute_tool(hook, _SESSION_KEY, "message", {"media": media})

    assert params["media"] == media


def test_media_existing_session_relative_path_left(session_workspace):
    """Путь, который уже резолвится в существующий файл, не трогаем."""
    hook = _make_hook(session_workspace)
    existing = f"data_store/sessions/{_SAFE_KEY}/files/presentation_minimal.html"
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


def test_media_never_falls_back_to_former_write_dir(tmp_path):
    """Поиска по прежней write-папке больше нет: иначе он приложил бы чужой файл.

    Раньше fallback искал basename в папке, куда агент писал в обход редиректа.
    Теперь такой путь недостижим для записи, и искать в нём — значит приложить
    файл, который к сессии не относится.
    """
    allowed = tmp_path / "data_store" / "cache"
    allowed.mkdir(parents=True)
    (allowed / "report.md").write_bytes(b"# report")
    _install_resolver(tmp_path)
    hook = _make_hook(tmp_path)

    params, _ = _run_before_execute_tool(
        hook, "postgres:chat-x", "message", {"media": ["report.md"]}
    )

    assert params["media"] == ["report.md"]


def test_media_without_resolver_is_not_refused(tmp_path):
    """Поиск только читает: недоступный резолвер — не отказ отправки сообщения."""
    from lib.services.session_files import install_session_file_resolver

    install_session_file_resolver(None)
    hook = _make_hook(tmp_path)
    params, _ = _run_before_execute_tool(hook, "cli:1", "message", {"media": ["report.md"]})

    assert params["media"] == ["report.md"]


def test_media_search_does_not_create_session_dir(tmp_path):
    """Поиск не создаёт каталог сессии для оборота, который к ней не обращался."""
    _install_resolver(tmp_path)
    hook = _make_hook(tmp_path)

    _run_before_execute_tool(hook, "cli:1", "message", {"media": ["report.md"]})

    assert not (tmp_path / "data_store" / "sessions" / "cli_1").exists()


# ----------------------------------------------------------------------
# Сквозные сценарии через реальный MessageTool и serialize
# ----------------------------------------------------------------------


def _resolve_via_message_tool(workspace, media):
    """Пропустить media через реальный ``MessageTool._resolve_media`` (как в цикле агента)."""
    from nanobot.agent.tools.message import MessageTool

    mt = MessageTool(workspace=str(workspace), restrict_to_workspace=False)
    return mt._resolve_media(list(media))


def test_e2e_relative_path_reaches_serialize(session_workspace):
    """write → message(media=[относительный путь]) → хук → ``_resolve_media``
    → ``serialize`` находит файл и кодирует data URL. Без фикса serialize писал
    'Media file not found, keeping path'."""
    from lib.utils.media import serialize

    hook = _make_hook(session_workspace)
    params, _ = _run_before_execute_tool(
        hook, _SESSION_KEY, "message", {"content": "ok", "media": ["presentation_minimal.html"]}
    )

    resolved = _resolve_via_message_tool(session_workspace, params["media"])

    expected = str(_files_dir(session_workspace) / "presentation_minimal.html")
    assert resolved == [expected]

    db_media = serialize(resolved)
    assert len(db_media) == 1
    entry = db_media[0]
    assert entry["file_id"].startswith("data:text/html;base64,")
    assert entry["mime_type"] == "text/html"
    assert entry["file_size"] == len(b"<h1>minimal</h1>")


def test_e2e_stale_absolute_path_reaches_serialize(session_workspace):
    """Тот же сценарий, но агент передал «абсолютный» путь чужого workspace."""
    from lib.utils.media import serialize

    hook = _make_hook(session_workspace)
    stale = "/home/datalab/nfs/workspaces_nanobot-release-v2.3.1/workspace/presentation_minimal.html"
    params, _ = _run_before_execute_tool(hook, _SESSION_KEY, "message", {"media": [stale]})

    resolved = _resolve_via_message_tool(session_workspace, params["media"])

    db_media = serialize(resolved)
    assert len(db_media) == 1
    assert db_media[0]["file_id"].startswith("data:text/html;base64,")
    assert db_media[0]["mime_type"] == "text/html"
    assert db_media[0]["file_size"] > 0
