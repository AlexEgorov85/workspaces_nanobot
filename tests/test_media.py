from __future__ import annotations

import base64
from pathlib import Path

import pytest

from utils.media import (
    data_url_info,
    entry_from_data_url,
    normalize_storage_entry,
    read_for_ui,
    resolve_paths_and_hints,
    serialize,
)


class TestDataUrlInfo:
    def test_parses_mime_and_size(self):
        raw = b"ab"
        url = "data:image/png;base64," + base64.b64encode(raw).decode()
        assert data_url_info(url) == ("image/png", 2)

    def test_default_mime_for_empty(self):
        # пустой mime в data URL — невалидный, кодец вернёт None
        assert data_url_info("data:;base64,YWI=") is None

    def test_non_data_url_returns_none(self):
        assert data_url_info("http://x/y.png") is None
        assert data_url_info(None) is None


class TestSerialize:
    def test_data_url_wraps(self):
        url = "data:image/png;base64,YWI="
        assert serialize([url]) == [{
            "filename": "file.png",
            "file_id": url,
            "mime_type": "image/png",
            "file_size": 2,
        }]

    def test_http_wraps_without_preview(self):
        assert serialize(["https://example.com/i.png"]) == [{
            "filename": "",
            "file_id": "https://example.com/i.png",
            "mime_type": "",
            "file_size": 0,
        }]

    def test_local_file_embeds(self, tmp_path):
        raw = b"%PDF-1.4 x"
        f = tmp_path / "r.pdf"
        f.write_bytes(raw)
        out = serialize([str(f)])[0]
        assert out["filename"] == "r.pdf"
        assert out["mime_type"] == "application/pdf"
        assert out["file_size"] == len(raw)
        assert out["file_id"].startswith("data:application/pdf;base64,")

    def test_missing_file_keeps_path(self, tmp_path):
        missing = tmp_path / "nope.pdf"
        assert serialize([str(missing)]) == [{
            "filename": "nope.pdf",
            "file_id": str(missing),
            "mime_type": "",
            "file_size": 0,
        }]

    def test_empty_and_none(self):
        assert serialize([]) == []
        assert serialize(None) is None

    def test_skips_non_strings(self):
        assert serialize(["data:image/png;base64,YWI=", None, "", 3]) == [
            entry_from_data_url("data:image/png;base64,YWI="),
        ]


class TestEntryFromDataUrl:
    def test_keeps_filename(self):
        url = "data:text/plain;base64,SGVsbG8="
        e = entry_from_data_url(url, "отчёт.txt")
        assert e["filename"] == "отчёт.txt"
        assert e["file_id"] == url
        assert e["mime_type"] == "text/plain"
        assert e["file_size"] == 5

    def test_default_filename_from_mime(self):
        assert entry_from_data_url("data:image/png;base64,YWI=")["filename"] == "file.png"

    def test_non_data_url(self):
        e = entry_from_data_url("https://x/y.png", "y.png")
        assert e["file_id"] == "https://x/y.png"
        assert e["mime_type"] == ""
        assert e["file_size"] == 0


class TestReadForUi:
    def test_aw_file_id(self):
        url = "data:image/png;base64,YWI="
        e = {"filename": "a.png", "file_id": url, "mime_type": "image/png", "file_size": 2}
        assert read_for_ui(e) == (url, "", "a.png")

    def test_legacy_data(self):
        url = "data:image/png;base64,YWI="
        e = {"filename": "a.png", "data": url}
        assert read_for_ui(e) == (url, "", "a.png")

    def test_path_dict(self):
        e = {"filename": "a.pdf", "path": "/cache/s/a.pdf"}
        assert read_for_ui(e) == ("", "/cache/s/a.pdf", "a.pdf")

    def test_http_dict_uses_file_id_as_url_path(self):
        e = {"filename": "", "file_id": "https://example.com/i.png", "mime_type": "", "file_size": 0}
        assert read_for_ui(e) == ("", "https://example.com/i.png", "file")

    def test_string_data_url(self):
        url = "data:image/png;base64,YWI="
        assert read_for_ui(url) == (url, "", "file")


class TestNormalizeStorageEntry:
    def test_legacy_data_dict_becomes_aw_format(self):
        url = "data:image/png;base64,YWI="
        entry = {"filename": "a.png", "data": url}
        out = normalize_storage_entry(entry)
        assert out == {
            "filename": "a.png",
            "file_id": url,
            "mime_type": "image/png",
            "file_size": 2,
        }
        assert "data" not in out

    def test_already_aw_format_untouched(self):
        url = "data:image/png;base64,YWI="
        e = {"filename": "a.png", "file_id": url, "mime_type": "image/png", "file_size": 2}
        assert normalize_storage_entry(e) == e

    def test_path_dict_untouched(self):
        e = {"filename": "a.pdf", "path": "/cache/s/a.pdf"}
        assert normalize_storage_entry(e) == e

    def test_string_untouched(self):
        assert normalize_storage_entry("https://example.com/i.png") == "https://example.com/i.png"

    def test_no_data_key_untouched(self):
        assert normalize_storage_entry({"filename": "a.png"}) == {"filename": "a.png"}


class TestResolvePathsAndHints:
    def test_dict_and_str(self):
        media = [
            {"filename": "a.pdf", "path": "/cache/s/1_a.pdf"},
            "/cache/s/plain.png",
        ]
        paths, hints = resolve_paths_and_hints(media)
        assert paths == ["/cache/s/1_a.pdf", "/cache/s/plain.png"]
        assert hints == [
            "[Attachment: a.pdf (saved at /cache/s/1_a.pdf)]",
            "[Attachment: plain.png (saved at /cache/s/plain.png)]",
        ]

    def test_empty(self):
        assert resolve_paths_and_hints([]) == ([], [])