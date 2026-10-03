"""Рабочий каталог сессии и хранилище вложений.

Проверяется ровно то, что операция не может сломать: выйти за пределы своей
сессии, прочитать чужой каталог, положить файл по имени с ``..`` и получить
вместо отказа ссылку на то, чего нет.

Остальное — раскладку подкаталогов, санитизацию имён, ``not_found`` на
чужой артефакт — проверяется здесь же, потому что это единственный код,
который отвечает за границу «моё / не моё».
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from libs.enterprise_common.errors import NotFoundError
from libs.enterprise_common.session.artifact_store import (
    Artifact,
    ArtifactError,
    ArtifactStore,
)
from libs.enterprise_common.session.security import (
    MAX_NAME_LENGTH,
    PathDeniedError,
    safe_child,
    safe_name,
)
from libs.enterprise_common.session.workspace import (
    SESSION_SUBDIRS,
    SessionWorkspace,
)


@pytest.fixture()
def workspace(tmp_path: Path) -> SessionWorkspace:
    return SessionWorkspace(tmp_path / "sessions")


# -- безопасные пути ---------------------------------------------------------


@pytest.mark.parametrize(
    "attempt",
    [
        "../../etc/passwd",
        "..\\..\\windows\\system32",
        "subdir/../../outside.txt",
        "/absolute/path.txt",
        "C:\\absolute\\path.txt",
        "",
        ".",
        "..",
    ],
    ids=repr,
)
def test_escape_attempts_are_denied(workspace: SessionWorkspace, attempt: str) -> None:
    with pytest.raises(PathDeniedError):
        workspace.write_text("s1", attempt, "данные", subdir="responses")


def test_symlink_out_of_session_is_denied(workspace: SessionWorkspace, tmp_path: Path) -> None:
    """Проверка по каноническому пути, а не по тексту.

    Ссылка внутри каталога сессии выглядит безобидно по строке: ``link.txt`` не
    содержит ни ``..``, ни разделителя. Защита, разбирающая текст пути, такую
    ссылку пропустила бы.
    """
    workspace.session_dir("s1")
    outside = tmp_path / "secret.txt"
    outside.write_text("чужие данные", encoding="utf-8")
    link = workspace.session_dir("s1", create=False) / "responses" / "link.txt"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError) as exc:  # Windows без прав на symlink
        pytest.skip(f"симлинк недоступен на этой машине: {exc}")
    with pytest.raises(PathDeniedError):
        workspace.read_text("s1", "link.txt", subdir="responses")


def test_inside_subdirectory_is_allowed(workspace: SessionWorkspace) -> None:
    path = workspace.write_text("s1", "nested/dir/file.txt", "ок", subdir="results")
    assert path.is_file()
    assert workspace.read_text("s1", "nested/dir/file.txt", subdir="results") == "ок"


def test_safe_name_rejects_separators_instead_of_silently_rewriting() -> None:
    """Опасное имя отвергается, а не «очищается».

    Очистка ``../../x`` дала бы ``x``, и два разных имени стали бы одним
    файлом. Для имени файла отказ честнее: вызывающий узнает, что имя
    непригодно, и пришлёт другое.
    """
    for attempt in ("a/b", "a\\b", "..", ".", ""):
        with pytest.raises(PathDeniedError):
            safe_name(attempt)


def test_safe_name_keeps_the_name_readable() -> None:
    assert "req-42" in safe_name("req-42 report.json")
    assert safe_name("req-42 report.json").endswith(".json")
    assert "req-42" in safe_name("req-42:report.json")


def test_safe_name_is_length_bounded() -> None:
    assert len(safe_name("x" * (MAX_NAME_LENGTH * 3))) <= MAX_NAME_LENGTH + 16


def test_safe_child_rejects_reserved_names() -> None:
    root = Path("/tmp/root")
    for attempt in ("..", ".", "", "/abs/path"):
        with pytest.raises(PathDeniedError):
            safe_child(root, attempt)


# -- раскладка сессии --------------------------------------------------------


def test_session_layout_is_created_on_demand(workspace: SessionWorkspace) -> None:
    """Создание каталогов — забота платформы, а не операции.

    Операция не содержит кода создания каталогов (это проверяет страж), и
    поэтому каталог должен появиться сам при первом обращении.
    """
    root = workspace.root / "s1"
    assert not root.exists()
    workspace.write_text("s1", "a.json", "{}", subdir="responses")
    for name in SESSION_SUBDIRS:
        assert (root / name).is_dir(), f"нет подкаталога {name}"


def test_layout_is_pinned(workspace: SessionWorkspace) -> None:
    """Состав раскладки проверяется целиком, а не «каждый из SESSION_SUBDIRS
    создан».

    Перечисление по самому объявлению проходит и после того, как подкаталог из
    него исчез, — и пропавший ``files/`` означал бы, что агент пишет рядом со
    снимками оборота, снова без объявленного места.
    """
    assert SESSION_SUBDIRS == (
        "files",
        "calls",
        "responses",
        "results",
        "errors",
        "events",
        "artifacts",
    )


def test_files_is_writable_by_the_agent_alone(workspace: SessionWorkspace) -> None:
    """``files/`` — единственное место записи агента, и оно не хуже прочих.

    Проверяется, что рабочая папка сессии отдаёт его под тем же именем, каким
    объявлено: иначе операция отдала бы агенту путь, а писать было бы некуда.
    """
    assert workspace.subdir("s1", "files").is_dir()
    assert workspace.list_files("s1", subdir="files") == []


def test_sessions_are_isolated(workspace: SessionWorkspace) -> None:
    workspace.write_text("s1", "answer.json", "один", subdir="responses")
    workspace.write_text("s2", "answer.json", "два", subdir="responses")
    assert workspace.read_text("s1", "answer.json", subdir="responses") == "один"
    assert workspace.read_text("s2", "answer.json", subdir="responses") == "два"
    assert len(workspace.list_files("s1", subdir="responses")) == 1


def test_session_id_is_never_used_as_a_path(workspace: SessionWorkspace) -> None:
    """Идентификатор сессии снаружи — и в имя каталога он попадает как есть.

    Отвергается, а не подменяется: два разных идентификатора, свёрнутые
    ``safe_name``-ом в один каталог, означали бы, что файлы одной сессии
    оказались в каталоге другой.
    """
    with pytest.raises(PathDeniedError):
        workspace.session_dir("../../escape")
    with pytest.raises(PathDeniedError):
        workspace.session_dir("s1/../s2")
    assert not (workspace.root.parent / "escape").exists()


def test_unknown_subdir_is_denied(workspace: SessionWorkspace) -> None:
    with pytest.raises(PathDeniedError):
        workspace.subdir("s1", "logs")


def test_json_write_is_canonical(workspace: SessionWorkspace) -> None:
    """Порядок ключей фиксирован: иначе два прогона дают разные хеши файла."""
    first = workspace.write_json("s1", "a.json", {"b": 1, "a": 2}, subdir="results")
    second = workspace.write_json("s1", "a.json", {"a": 2, "b": 1}, subdir="results")
    assert first.read_text(encoding="utf-8") == second.read_text(encoding="utf-8")
    assert json.loads(first.read_text(encoding="utf-8")) == {"a": 2, "b": 1}


def test_handle_is_bound_to_one_session(workspace: SessionWorkspace) -> None:
    handle = workspace.handle("s1")
    handle.write_text("a.txt", "привет", subdir="responses")
    assert handle.read_text("a.txt", subdir="responses") == "привет"
    assert handle.exists("a.txt", subdir="responses")
    assert handle.remove("a.txt", subdir="responses")
    assert not handle.exists("a.txt", subdir="responses")


# -- хранилище вложений ------------------------------------------------------


def test_artifact_roundtrip(workspace: SessionWorkspace) -> None:
    store = ArtifactStore(workspace)
    artifact = store.create("s1", name="report.json", content=b'{"a":1}', tool_name="run_script")
    assert isinstance(artifact, Artifact)
    assert artifact.size == len(b'{"a":1}')
    assert artifact.session_id == "s1"
    assert artifact.uri.startswith("session://artifacts/")
    assert store.read("s1", artifact.artifact_id) == b'{"a":1}'


def test_artifact_metadata_has_no_disk_path(workspace: SessionWorkspace) -> None:
    """Путь на машине платформы наружу не отдаётся: он уедет в журнал.

    Ответ читают не только на этой машине, и абсолютный путь в нём — это
    лишнее и потенциально вредное (он раскрывает раскладку диска).
    """
    store = ArtifactStore(workspace)
    artifact = store.create("s1", name="report.json", content=b"{}")
    payload = artifact.to_json()
    assert set(payload) == {"artifact_id", "name", "content_type", "size", "uri"}
    assert "path" not in payload
    assert str(workspace.root) not in json.dumps(payload)


def test_foreign_artifact_is_not_found(workspace: SessionWorkspace) -> None:
    store = ArtifactStore(workspace)
    artifact = store.create("s1", name="secret.json", content=b"{}")
    with pytest.raises(NotFoundError) as excinfo:
        store.read("s2", artifact.artifact_id)
    # Не `invalid_params`: подтверждение существования чужого файла — утечка.
    assert excinfo.value.code == "not_found"
    assert "secret" not in str(excinfo.value)


def test_dangerous_artifact_name_never_leaves_the_store(workspace: SessionWorkspace) -> None:
    """Имя с разделителем пути отвергается, а файл не появляется нигде.

    Спека допускает и отказ, и срез: важно одно — файл не должен оказаться вне
    каталога артефактов. Здесь выбран отказ, потому что ``safe_name`` не умеет
    отличить ``отчёт/2024`` от ``../../etc``, не разбирая путь по частям.
    """
    store = ArtifactStore(workspace)
    with pytest.raises(PathDeniedError):
        store.create("s1", name="../../escape.json", content=b"{}")
    artifacts = workspace.session_dir("s1", create=False) / "artifacts"
    assert not any(artifacts.rglob("*.json")), "файл создан вопреки отказу"


def test_artifact_goes_to_declared_subdir_and_folder(workspace: SessionWorkspace) -> None:
    """Крупный результат и вложение операции — одно хранилище, разные полки."""
    store = ArtifactStore(workspace)
    artifact = store.create(
        "s1", name="result.json", content=b"{}", request_id="req-7", subdir="results", folder="req-7"
    )
    assert artifact.uri == f"session://results/req-7/{artifact.path.name}"
    assert artifact.path.parent.name == "req-7"
    assert artifact.path.parent.parent.name == "results"


def test_artifact_list_is_session_scoped(workspace: SessionWorkspace) -> None:
    store = ArtifactStore(workspace)
    store.create("s1", name="a.json", content=b"a")
    store.create("s2", name="b.json", content=b"b")
    names = {item["name"] for item in store.list("s1")}
    assert names == {"a.json"}
    assert len(store.list("s2")) == 1


def test_artifact_write_failure_is_artifact_error(tmp_path: Path) -> None:
    """Недоступный каталог — это ``ArtifactError``, а не ``OSError`` наружу."""
    workspace = SessionWorkspace(tmp_path / "sessions")
    store = ArtifactStore(workspace)
    session_dir = workspace.session_dir("s1")
    # Подкаталог артефактов заменяем файлом: каталог недоступен для записи, и
    # операция должна получить доменный отказ, а не `PermissionError`.
    artifacts = session_dir / "artifacts"
    artifacts.rmdir()
    artifacts.write_text("не каталог", encoding="utf-8")
    with pytest.raises(ArtifactError) as excinfo:
        store.create("s1", name="a.json", content=b"{}")
    assert excinfo.value.code == "infrastructure_error"
