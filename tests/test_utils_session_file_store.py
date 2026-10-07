from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_workspace_path = str(Path(__file__).resolve().parent.parent / "workspace")
if _workspace_path not in sys.path:
    sys.path.insert(0, _workspace_path)
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
_user_site = r"C:\Users\Алексей\AppData\Roaming\Python\Python314\site-packages"
if _user_site not in sys.path:
    sys.path.insert(0, _user_site)


def _store(root: Path, **kw):
    """Хранилище с резолвером, заменённым временным каталогом.

    Контракт после снятия собственной раскладки: каталог сессии приходит
    функцией от резолвера, а не вычисляется хранилищем из корня.
    """
    from lib.utils.session_file_store import SessionFileStore

    return SessionFileStore(lambda key: root / key, **kw)


class TestSafeSessionKey:
    """Хранилище не заводит собственного правила имени каталога.

    Правило объявлено в спецификации openspec-предложения
    ``2026-10-03-session-files`` и живёт в ``session_key.safe_session_key``.
    Вторая копия в хранилище разошлась бы с резолвером и с платформой при
    первой же правке, поэтому проверяем, что импортировано именно то правило.
    """

    def test_store_does_not_name_directories_at_all(self):
        import lib.utils.session_file_store as store_module

        assert not hasattr(store_module, "safe_session_key"), (
            "хранилище снова заводит правило имени каталога: "
            "его берёт резолвер, а каталог отдаёт владелец"
        )

    def test_store_does_not_import_session_key_module(self):
        import lib.utils.session_file_store as store_module

        assert not hasattr(store_module, "SessionDirNameDenied"), (
            "хранилище имену каталога не вычисляет"
        )

    def test_layout_is_the_platform_one(self, tmp_path):
        """Вложение и результат лежат в ``files/`` каталога сессии."""
        store = _store(tmp_path)
        store.save("s1", "data", "t1")
        assert (tmp_path / "s1" / "files" / "results").is_dir()
        assert not (tmp_path / "cache").exists(), (
            "хранилище снова создало свой корень"
        )


class TestPrepareContent:
    def test_json_dict_formatted(self):
        from lib.utils.session_file_store import prepare_content

        content, ext = prepare_content('{"a": 1}')
        assert ext == ".json"
        parsed = json.loads(content)
        assert parsed == {"a": 1}

    def test_json_list_formatted(self):
        from lib.utils.session_file_store import prepare_content

        content, ext = prepare_content("[1, 2, 3]")
        assert ext == ".json"
        assert json.loads(content) == [1, 2, 3]

    def test_plain_text(self):
        from lib.utils.session_file_store import prepare_content

        content, ext = prepare_content("hello world")
        assert ext == ".txt"
        assert content == "hello world"

    def test_invalid_json_returns_txt(self):
        from lib.utils.session_file_store import prepare_content

        content, ext = prepare_content("{invalid}")
        assert ext == ".txt"
        assert content == "{invalid}"

    def test_empty_string(self):
        from lib.utils.session_file_store import prepare_content

        content, ext = prepare_content("")
        assert ext == ".txt"
        assert content == ""

    def test_list_of_dicts_converts_to_csv(self):
        from lib.utils.session_file_store import prepare_content

        content, ext = prepare_content('[{"a": 1, "b": 2}]')
        assert ext == ".csv"
        assert "\ufeff" in content
        assert "a,b" in content
        assert "1,2" in content


class TestTryConvertToCsv:
    @pytest.mark.parametrize("input_value,expected_substrings,expected_is_none", [
        ([{"x": 10, "y": 20}], ["x,y", "10,20"], False),
        ({"results": [{"k": "v"}]}, ["k", "v"], False),
        ({"rows": [["a", 1]], "columns": ["name", "val"]}, ["name,val", "a,1"], False),
        ({"data": {"rows": [["x"]], "columns": ["c"]}}, ["c", "x"], False),
        ({"a": 1}, [], True),
        ([], [], True),
        ([1, 2], [], True),
        (None, [], True),
    ])
    def test_try_convert_to_csv(self, input_value, expected_substrings, expected_is_none):
        from lib.utils.session_file_store import _try_convert_to_csv

        result = _try_convert_to_csv(input_value)
        if expected_is_none:
            assert result is None
        else:
            assert result is not None
            for s in expected_substrings:
                assert s in result, f"expected {s!r} in {result!r}"


class TestSessionFileStoreInit:
    def test_constructor_creates_nothing(self, tmp_path):
        """Пустая инициализация не оставляет после себя каталогов.

        Прежний конструктор создавал ``cache/sessions`` и ``cache/archive``
        сразу, то есть дерево сессий росло от того, что канал создан, а не от
        того, что в него писали.
        """
        _store(tmp_path)
        assert list(tmp_path.iterdir()) == []

    def test_custom_limits(self, tmp_path):
        store = _store(tmp_path, max_files=5, max_age_hours=24)
        assert store.max_files == 5
        assert store.max_age_hours == 24


class TestSessionFileStoreGetSessionDir:
    def test_creates_subdirs(self, tmp_path):
        store = _store(tmp_path)
        sdir = store._get_session_dir("my-key")
        assert sdir.exists()
        assert (sdir / "files" / "results").exists()
        assert (sdir / "files" / "attachments").exists()

    def test_returns_path(self, tmp_path):
        store = _store(tmp_path)
        sdir = store._get_session_dir("my-key")
        assert "my-key" in str(sdir)


class TestSessionFileStoreEnsureMetadata:
    def test_creates_metadata(self, tmp_path):
        store = _store(tmp_path)
        store._ensure_metadata("s1")
        meta_path = tmp_path / "s1" / "metadata.json"
        assert meta_path.exists()
        meta = json.loads(meta_path.read_text())
        assert meta["session_key"] == "s1"
        assert meta["status"] == "active"

    def test_idempotent(self, tmp_path):
        store = _store(tmp_path)
        store._ensure_metadata("s1")
        store._ensure_metadata("s1")
        meta_path = tmp_path / "s1" / "metadata.json"
        assert meta_path.exists()


class TestSessionFileStoreSave:
    def test_saves_file(self, tmp_path):
        store = _store(tmp_path)
        info = store.save("s1", '{"ok": true}', "test_tool")
        assert info["session_key"] == "s1"
        assert info["format"] == "json"
        assert info["size_kb"] > 0

    def test_updates_metadata(self, tmp_path):
        store = _store(tmp_path)
        store.save("s1", "data", "tool1", ext=".txt")
        meta_path = tmp_path / "s1" / "metadata.json"
        meta = json.loads(meta_path.read_text())
        assert meta["file_count"] == 1

    def test_multiple_saves_increment_count(self, tmp_path):
        store = _store(tmp_path)
        store.save("s1", "a", "t1")
        store.save("s1", "b", "t1")
        meta_path = tmp_path / "s1" / "metadata.json"
        meta = json.loads(meta_path.read_text())
        assert meta["file_count"] == 2


class TestSessionFileStoreDedupe:
    def test_second_save_returns_existing(self, tmp_path):
        store = _store(tmp_path)
        first = store.save("s1", "same content", "t1", ext=".txt")
        second = store.save("s1", "same content", "t1", ext=".txt")
        assert second["deduped"] is True
        assert second["path"] == first["path"]
        assert first["deduped"] is False

    def test_no_new_file_on_dedupe(self, tmp_path):
        store = _store(tmp_path)
        store.save("s1", "same", "t1", ext=".txt")
        store.save("s1", "same", "t1", ext=".txt")
        results_dir = tmp_path / "s1" / "files" / "results"
        assert len(list(results_dir.iterdir())) == 1

    def test_metadata_count_not_incremented_on_dedupe(self, tmp_path):
        store = _store(tmp_path)
        store.save("s1", "same", "t1", ext=".txt")
        store.save("s1", "same", "t1", ext=".txt")
        meta_path = tmp_path / "s1" / "metadata.json"
        meta = json.loads(meta_path.read_text())
        assert meta["file_count"] == 1

    def test_different_content_saves_separately(self, tmp_path):
        store = _store(tmp_path)
        store.save("s1", "alpha", "t1", ext=".txt")
        store.save("s1", "beta", "t1", ext=".txt")
        results_dir = tmp_path / "s1" / "files" / "results"
        assert len(list(results_dir.iterdir())) == 2

    def test_dedupe_false_always_writes(self, tmp_path):
        store = _store(tmp_path)
        store.save("s1", "twice", "t1", ext=".txt", dedupe=False)
        store.save("s1", "twice", "t1", ext=".txt", dedupe=False)
        results_dir = tmp_path / "s1" / "files" / "results"
        assert len(list(results_dir.iterdir())) == 2

    def test_dedupe_scoped_to_session(self, tmp_path):
        store = _store(tmp_path)
        store.save("s1", "shared", "t1", ext=".txt")
        second = store.save("s2", "shared", "t1", ext=".txt")
        assert second["deduped"] is False


class TestSessionFileStoreCleanup:
    def test_noop_when_no_limits(self, tmp_path):
        store = _store(tmp_path)
        store.save("s1", "data", "t1")
        store.cleanup("s1")
        results_dir = tmp_path / "s1" / "files" / "results"
        assert len(list(results_dir.iterdir())) == 1

    def test_removes_by_count(self, tmp_path):
        store = _store(tmp_path, max_files=2)
        store.save("s1", "1", "t1")
        store.save("s1", "2", "t1")
        store.save("s1", "3", "t1")
        results_dir = tmp_path / "s1" / "files" / "results"
        assert len(list(results_dir.iterdir())) == 2

    def test_removes_by_age(self, tmp_path):
        store = _store(tmp_path, max_age_hours=1)
        store.save("s1", "old", "t1")

        (tmp_path / "s1" / "files" / "results" / "20200101_000000_t1_00000000.txt").write_text("old")
        store.cleanup("s1")
        results_dir = tmp_path / "s1" / "files" / "results"
        assert len(list(results_dir.iterdir())) == 1

    def test_updates_metadata_after_cleanup(self, tmp_path):
        store = _store(tmp_path, max_files=1)
        store.save("s1", "keep", "t1")
        store.save("s1", "remove", "t1")
        meta_path = tmp_path / "s1" / "metadata.json"
        meta = json.loads(meta_path.read_text())
        assert meta["file_count"] == 1


class TestSaveAttachment:
    """save_attachment — единое хранилище вложений через SessionFileStore."""

    @staticmethod
    def _data_url(payload: bytes, mime: str = "text/plain") -> str:
        import base64
        return f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"

    def test_data_url_writes_into_attachments_dir(self, tmp_path):
        store = _store(tmp_path)
        url = self._data_url(b"hello", "text/plain")
        info = store.save_attachment("s1", url, filename="hi.txt")
        assert info is not None
        dest = Path(info["path"])
        assert dest.exists()
        assert dest.read_bytes() == b"hello"
        assert dest.parent.name == "attachments"
        assert info["filename"] == "hi.txt"

    def test_falls_back_to_mime_extension_when_no_filename(self, tmp_path):
        store = _store(tmp_path)
        url = self._data_url(b"abc", "image/png")
        info = store.save_attachment("s1", url)
        assert info is not None
        dest = Path(info["path"])
        assert dest.suffix == ".png"

    def test_updates_metadata(self, tmp_path):
        store = _store(tmp_path)
        store.save_attachment("s1", self._data_url(b"abc"))
        meta_path = tmp_path / "s1" / "metadata.json"
        meta = json.loads(meta_path.read_text())
        assert meta["file_count"] == 1
        assert meta["total_bytes"] == 3

    def test_external_url_returns_none(self, tmp_path):
        store = _store(tmp_path)
        assert store.save_attachment("s1", "https://example.com/x.pdf") is None

    def test_missing_local_path_returns_none(self, tmp_path):
        store = _store(tmp_path)
        assert store.save_attachment("s1", str(tmp_path / "nope.bin")) is None

    def test_invalid_data_url_returns_none(self, tmp_path):
        store = _store(tmp_path)
        assert store.save_attachment("s1", "data:text/plain;base64,!!!notbase64!!!") is None
