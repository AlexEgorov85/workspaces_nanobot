"""Страж границы чтения: ``document_read`` читает только ``files/`` своей сессии.

До фазы 7 ``_resolve_path`` делал ``Path(path).expanduser()`` + ``is_file()`` —
любой абсолютный путь на хосте читался, ограничением был список расширений. После
отключения ``exec`` это оставалось единственным чтением мимо границы, поэтому путь
стал относительным, а база — каталогом ``files/`` сессии от резолвера.

Тесты бьют по каждому способу выйти из ``files/`` и по обоим отказам инфраструктуры
(нет ``session_key``, нет резолвера). Проверка стражей этого проекта: страж, который
ни разу не срабатывал, неотличим от стража, который ничего не проверяет.

Каталог сессии — от настоящего ``SessionFileResolver``, потому что tool берёт путь
у него: подмена заглушкой проверила бы заглушку.
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

#: Содержимое файла за пределами сессии. Если оно появится в ответе tool'а,
#: граница не держит: значит путь разрешился мимо ``files/``.
_OUTSIDE_SECRET = "SECRET-OUTSIDE-SESSION"


def _files_dir(workspace_dir: Path) -> Path:
    return workspace_dir / "data_store" / "sessions" / _SAFE_KEY / "files"


def _install_resolver(workspace_dir: Path) -> None:
    from lib.services.session_files import SessionFileResolver, install_session_file_resolver

    install_session_file_resolver(
        SessionFileResolver(enterprise_mcp=None, workspace_dir=workspace_dir)
    )


def _call(path: str, *, session_key: str | None = _SESSION_KEY):
    """Вызвать tool в контексте оборота (как это делает диспетчер инструментов)."""
    from nanobot.agent.tools.context import RequestContext, request_context

    from workspace.tools.document_read import DocumentReadTool

    ctx = RequestContext(channel="postgres", chat_id="c1", session_key=session_key)
    with request_context(ctx):
        return asyncio.run(DocumentReadTool().execute(path=path))


@pytest.fixture(autouse=True)
def _no_leaked_resolver():
    """Публикация резолвера — глобальная для процесса: после теста её снимаем."""
    yield
    from lib.services.session_files import install_session_file_resolver

    install_session_file_resolver(None)


@pytest.fixture()
def session_workspace(tmp_path):
    """Workspace с готовым каталогом сессии и файлом-вложением."""
    files_dir = _files_dir(tmp_path)
    (files_dir / "attachments").mkdir(parents=True)
    (files_dir / "attachments" / "note.txt").write_text("вложение сессии", encoding="utf-8")
    (tmp_path / "secret.txt").write_text(_OUTSIDE_SECRET, encoding="utf-8")
    _install_resolver(tmp_path)
    return tmp_path


# ----------------------------------------------------------------------
# Разрешённое чтение
# ----------------------------------------------------------------------


def test_relative_attachment_is_read(session_workspace):
    """Вложение пользователя читается относительным путём от ``files/``."""
    result = _call("attachments/note.txt")
    assert "вложение сессии" in str(result)
    assert not getattr(result, "is_error", False), result


def test_file_at_files_root_is_read(session_workspace):
    """Файл прямо в ``files/`` — тоже своя сессия, а не только ``attachments/``."""
    (_files_dir(session_workspace) / "report.txt").write_text("отчёт", encoding="utf-8")
    assert "отчёт" in str(_call("report.txt"))


# ----------------------------------------------------------------------
# Отказы границы
# ----------------------------------------------------------------------


def test_absolute_path_outside_session_is_refused(session_workspace):
    """Абсолютный путь на хосте не читается — прежде это проходило.

    Конкретная причина отказа зависит от ОС (на Windows путь с буквой диска
    отвергается раньше, чем «абсолютный»), поэтому проверяется инвариант: отказ
    назван границей, а не «файла нет», и содержимое не утекло.
    """
    outside = session_workspace / "secret.txt"
    result = _call(str(outside))
    text = str(result)
    assert getattr(result, "is_error", False), text
    assert _OUTSIDE_SECRET not in text, "содержимое файла вне сессии утекло в ответ"
    assert "недопустим" in text
    assert "читается только содержимое files/" in text


def test_parent_escape_is_refused(session_workspace):
    """``..`` за пределы ``files/`` отвергается тем же примитивом, что и на платформе."""
    result = str(_call("../../../secret.txt"))
    assert _OUTSIDE_SECRET not in result
    assert "files/" in result, "отказ должен называть границу, а не «файла нет»"


def test_drive_letter_path_is_refused(session_workspace):
    """Путь с буквой диска не считается относительным (``PurePosixPath`` так думает)."""
    result = str(_call("C:/Users/someone/secret.txt"))
    assert "буквой диска" in result or "абсолютный путь" in result


def test_files_dir_itself_is_refused(session_workspace):
    """Сам каталог — не файл внутри него."""
    assert "выход за пределы" in str(_call(".")) or "недопустим" in str(_call("."))


def test_empty_path_is_refused(session_workspace):
    """Пустой путь — отказ, а не чтение текущего каталога."""
    assert "недопустим" in str(_call("")) or "required" in str(_call(""))


# ----------------------------------------------------------------------
# Отказы инфраструктуры: нет ключа оборота / нет резолвера
# ----------------------------------------------------------------------


def test_turn_without_session_key_is_refused(session_workspace):
    """Без ``session_key`` каталога сессии не существует — чтение отменено."""
    result = str(_call("attachments/note.txt", session_key=None))
    assert "session_key" in result
    assert "вложение сессии" not in result


def test_missing_resolver_is_refused(session_workspace):
    """Нет опубликованного резолвера — отказ с названной причиной, а не «где-то ещё»."""
    from lib.services.session_files import install_session_file_resolver

    install_session_file_resolver(None)
    result = str(_call("attachments/note.txt"))
    assert "резолвер" in result
    assert "вложение сессии" not in result


def test_read_does_not_create_session_dir(tmp_path):
    """Чтение не создаёт каталог сессии: нечего создавать — нечего и создавать."""
    _install_resolver(tmp_path)
    _call("attachments/note.txt")
    assert not (tmp_path / "data_store" / "sessions" / _SAFE_KEY).exists()


# ----------------------------------------------------------------------
# Схема: обещание модели не должно обещать лишнего
# ----------------------------------------------------------------------


def test_schema_describes_relative_path_only():
    """Описание ``path`` в схеме больше не обещает ни абсолютный путь, ни workspace."""
    from workspace.tools.document_read import DocumentReadTool

    params = DocumentReadTool().parameters
    text = params["properties"]["path"]["description"]
    lowered = text.lower()
    assert "files/" in text, text
    assert "относительно" in lowered, text
    assert "workspace" not in lowered, text
    assert "абсолютный либо" not in lowered and "либо относительный" not in lowered, text


def test_tool_description_mentions_relative_path():
    """Описание самого tool'а тоже говорит про относительный путь."""
    from workspace.tools.document_read import DocumentReadTool

    lowered = DocumentReadTool().description.lower()
    assert "относительный" in lowered
    assert "абсолютный отвергается" in lowered
