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


class TestSafeSessionKey:
    def test_keeps_plain_key(self):
        from utils.session_file_store import safe_session_key

        assert safe_session_key("hello") == "hello"

    def test_replaces_invalid_chars(self):
        from utils.session_file_store import safe_session_key

        assert safe_session_key("a:b/c") == "a_b_c"

    def test_all_invalid_replaced(self):
        from utils.session_file_store import safe_session_key

        assert safe_session_key('\\/:*?"<>|') == "_"

    def test_empty_string(self):
        from utils.session_file_store import safe_session_key

        assert safe_session_key("") == ""

    def test_alphanumeric_unchanged(self):
        from utils.session_file_store import safe_session_key

        assert safe_session_key("abc123-_.~") == "abc123-_.~"


class TestCsvVal:
    def test_none_returns_empty(self):
        from utils.session_file_store import _csv_val

        assert _csv_val(None) == ""

    def test_str_returns_str(self):
        from utils.session_file_store import _csv_val

        assert _csv_val("hello") == "hello"

    def test_int_converted(self):
        from utils.session_file_store import _csv_val

        assert _csv_val(42) == "42"

    def test_float_converted(self):
        from utils.session_file_store import _csv_val

        assert _csv_val(3.14) == "3.14"


class TestPrepareContent:
    def test_json_dict_formatted(self):
        from utils.session_file_store import prepare_content

        content, ext = prepare_content('{"a": 1}')
        assert ext == ".json"
        parsed = json.loads(content)
        assert parsed == {"a": 1}

    def test_json_list_formatted(self):
        from utils.session_file_store import prepare_content

        content, ext = prepare_content("[1, 2, 3]")
        assert ext == ".json"
        assert json.loads(content) == [1, 2, 3]

    def test_plain_text(self):
        from utils.session_file_store import prepare_content

        content, ext = prepare_content("hello world")
        assert ext == ".txt"
        assert content == "hello world"

    def test_invalid_json_returns_txt(self):
        from utils.session_file_store import prepare_content

        content, ext = prepare_content("{invalid}")
        assert ext == ".txt"
        assert content == "{invalid}"

    def test_empty_string(self):
        from utils.session_file_store import prepare_content

        content, ext = prepare_content("")
        assert ext == ".txt"
        assert content == ""

    def test_list_of_dicts_converts_to_csv(self):
        from utils.session_file_store import prepare_content

        content, ext = prepare_content('[{"a": 1, "b": 2}]')
        assert ext == ".csv"
        assert "\ufeff" in content
        assert "a,b" in content
        assert "1,2" in content


class TestTryConvertToCsv:
    def test_list_of_dicts(self):
        from utils.session_file_store import _try_convert_to_csv

        result = _try_convert_to_csv([{"x": 10, "y": 20}])
        assert result is not None
        assert "x,y" in result
        assert "10,20" in result

    def test_dict_with_results(self):
        from utils.session_file_store import _try_convert_to_csv

        result = _try_convert_to_csv({"results": [{"k": "v"}]})
        assert result is not None
        assert "k" in result
        assert "v" in result

    def test_dict_with_rows_columns(self):
        from utils.session_file_store import _try_convert_to_csv

        result = _try_convert_to_csv({"rows": [["a", 1]], "columns": ["name", "val"]})
        assert result is not None
        assert "name,val" in result
        assert "a,1" in result

    def test_dict_with_nested_data(self):
        from utils.session_file_store import _try_convert_to_csv

        result = _try_convert_to_csv({"data": {"rows": [["x"]], "columns": ["c"]}})
        assert result is not None
        assert "c" in result
        assert "x" in result

    def test_plain_dict_returns_none(self):
        from utils.session_file_store import _try_convert_to_csv

        result = _try_convert_to_csv({"a": 1})
        assert result is None

    def test_empty_list_returns_none(self):
        from utils.session_file_store import _try_convert_to_csv

        result = _try_convert_to_csv([])
        assert result is None

    def test_list_of_non_dicts_returns_none(self):
        from utils.session_file_store import _try_convert_to_csv

        result = _try_convert_to_csv([1, 2])
        assert result is None

    def test_none_returns_none(self):
        from utils.session_file_store import _try_convert_to_csv

        result = _try_convert_to_csv(None)
        assert result is None


class TestSessionFileStoreInit:
    def test_creates_directories(self, tmp_path):
        from utils.session_file_store import SessionFileStore

        store = SessionFileStore(tmp_path)
        assert (tmp_path / "cache" / "sessions").exists()
        assert (tmp_path / "cache" / "archive").exists()

    def test_default_limits(self, tmp_path):
        from utils.session_file_store import SessionFileStore

        store = SessionFileStore(tmp_path)
        assert store.max_files == 0
        assert store.max_age_hours == 0

    def test_custom_limits(self, tmp_path):
        from utils.session_file_store import SessionFileStore

        store = SessionFileStore(tmp_path, max_files=5, max_age_hours=24)
        assert store.max_files == 5
        assert store.max_age_hours == 24


class TestSessionFileStoreGetSessionDir:
    def test_creates_subdirs(self, tmp_path):
        from utils.session_file_store import SessionFileStore

        store = SessionFileStore(tmp_path)
        sdir = store._get_session_dir("my-key")
        assert sdir.exists()
        assert (sdir / "results").exists()

    def test_returns_path(self, tmp_path):
        from utils.session_file_store import SessionFileStore

        store = SessionFileStore(tmp_path)
        sdir = store._get_session_dir("my-key")
        assert "my-key" in str(sdir)


class TestSessionFileStoreEnsureMetadata:
    def test_creates_metadata(self, tmp_path):
        from utils.session_file_store import SessionFileStore

        store = SessionFileStore(tmp_path)
        store._ensure_metadata("s1")
        meta_path = tmp_path / "cache" / "sessions" / "s1" / "metadata.json"
        assert meta_path.exists()
        meta = json.loads(meta_path.read_text())
        assert meta["session_key"] == "s1"
        assert meta["status"] == "active"

    def test_idempotent(self, tmp_path):
        from utils.session_file_store import SessionFileStore

        store = SessionFileStore(tmp_path)
        store._ensure_metadata("s1")
        store._ensure_metadata("s1")
        meta_path = tmp_path / "cache" / "sessions" / "s1" / "metadata.json"
        assert meta_path.exists()


class TestSessionFileStoreSave:
    def test_saves_file(self, tmp_path):
        from utils.session_file_store import SessionFileStore

        store = SessionFileStore(tmp_path)
        info = store.save("s1", '{"ok": true}', "test_tool")
        assert info["session_key"] == "s1"
        assert info["format"] == "json"
        assert info["size_kb"] > 0

    def test_updates_metadata(self, tmp_path):
        from utils.session_file_store import SessionFileStore

        store = SessionFileStore(tmp_path)
        store.save("s1", "data", "tool1", ext=".txt")
        meta_path = tmp_path / "cache" / "sessions" / "s1" / "metadata.json"
        meta = json.loads(meta_path.read_text())
        assert meta["file_count"] == 1

    def test_multiple_saves_increment_count(self, tmp_path):
        from utils.session_file_store import SessionFileStore

        store = SessionFileStore(tmp_path)
        store.save("s1", "a", "t1")
        store.save("s1", "b", "t1")
        meta_path = tmp_path / "cache" / "sessions" / "s1" / "metadata.json"
        meta = json.loads(meta_path.read_text())
        assert meta["file_count"] == 2


class TestSessionFileStoreCleanup:
    def test_noop_when_no_limits(self, tmp_path):
        from utils.session_file_store import SessionFileStore

        store = SessionFileStore(tmp_path)
        store.save("s1", "data", "t1")
        store.cleanup("s1")
        results_dir = tmp_path / "cache" / "sessions" / "s1" / "results"
        assert len(list(results_dir.iterdir())) == 1

    def test_removes_by_count(self, tmp_path):
        from utils.session_file_store import SessionFileStore

        store = SessionFileStore(tmp_path, max_files=2)
        store.save("s1", "1", "t1")
        store.save("s1", "2", "t1")
        store.save("s1", "3", "t1")
        results_dir = tmp_path / "cache" / "sessions" / "s1" / "results"
        assert len(list(results_dir.iterdir())) == 2

    def test_removes_by_age(self, tmp_path):
        from utils.session_file_store import SessionFileStore

        store = SessionFileStore(tmp_path, max_age_hours=1)
        store.save("s1", "old", "t1")

        (tmp_path / "cache" / "sessions" / "s1" / "results" / "20200101_000000_t1_00000000.txt").write_text("old")
        store.cleanup("s1")
        results_dir = tmp_path / "cache" / "sessions" / "s1" / "results"
        assert len(list(results_dir.iterdir())) == 1

    def test_updates_metadata_after_cleanup(self, tmp_path):
        from utils.session_file_store import SessionFileStore

        store = SessionFileStore(tmp_path, max_files=1)
        store.save("s1", "keep", "t1")
        store.save("s1", "remove", "t1")
        meta_path = tmp_path / "cache" / "sessions" / "s1" / "metadata.json"
        meta = json.loads(meta_path.read_text())
        assert meta["file_count"] == 1


class TestSessionFileStoreArchiveSession:
    def test_archives_session(self, tmp_path):
        from utils.session_file_store import SessionFileStore

        store = SessionFileStore(tmp_path)
        store.save("s1", "data", "t1")
        result = store.archive_session("s1")
        assert result is True
        assert not (tmp_path / "cache" / "sessions" / "s1").exists()

    def test_archive_no_session_returns_false(self, tmp_path):
        from utils.session_file_store import SessionFileStore

        store = SessionFileStore(tmp_path)
        result = store.archive_session("nonexistent")
        assert result is False

    def test_archive_idempotent(self, tmp_path):
        from utils.session_file_store import SessionFileStore

        store = SessionFileStore(tmp_path)
        store.save("s1", "data", "t1")
        store.archive_session("s1")
        result = store.archive_session("s1")
        assert result is False
