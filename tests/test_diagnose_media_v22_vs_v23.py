"""Изолированный тест: поведение utils.media.serialize/deserialize для
существующих и несуществующих файлов в текущей кодовой базе и в v2.2.0.

Цель: подтвердить, что для СУЩЕСТВУЮЩИХ файлов .md/.xlsx кодек работает
одинаково в v2.2.0 и v2.3.0, и проблема «файлы не доходят до таблицы»
не в кодеке.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
WORKSPACE = REPO / "workspace"
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _get_v220_media_py() -> str:
    """Получить workspace/utils/media.py (если есть) или postgres_channel.py из v2.2.0."""
    out = subprocess.run(
        ["git", "show", "v2.2.0:workspace/utils/media.py"],
        cwd=str(REPO), capture_output=True, text=True, check=False,
    ).stdout
    if out.strip():
        return out
    # В v2.2.0 utils/media.py не существовало — код был в postgres_channel.py
    return subprocess.run(
        ["git", "show", "v2.2.0:lib/channels/postgres_channel.py"],
        cwd=str(REPO), capture_output=True, text=True, check=True,
    ).stdout


def test_serialize_existing_files_returns_aw_format(tmp_path):
    """Существующие файлы сериализуются в AW-формат с непустым mime_type."""
    from workspace.utils.media import serialize

    f1 = tmp_path / "test.md"
    f1.write_bytes(b"# hello\n")
    f2 = tmp_path / "test.xlsx"
    f2.write_bytes(b"PK fake xlsx")

    result = serialize([str(f1), str(f2)])

    assert len(result) == 2
    for entry in result:
        assert isinstance(entry, dict)
        assert entry.get("file_id", "").startswith("data:")
        assert entry.get("mime_type"), "mime_type не должен быть пустым для существующего файла"
        assert entry.get("file_size") > 0
        assert entry.get("filename")


def test_serialize_missing_file_returns_dict_with_empty_mime(tmp_path):
    """Несуществующий файл даёт dict с пустым mime_type/file_size, но file_id=путь.

    Это регрессия v2.3.0 vs v2.2.0: в v2.2.0 несуществующий файл клался
    просто строкой-путь, а не dict-обёрткой с пустым mime.
    """
    from workspace.utils.media import serialize

    missing = tmp_path / "does_not_exist.docx"
    result = serialize([str(missing)])

    assert len(result) == 1
    entry = result[0]
    assert isinstance(entry, dict)
    # В v2.2.0 здесь была просто строка-путь. Сейчас — dict с пустым mime.
    assert entry.get("mime_type") == "", (
        "v2.3.0: несуществующий файл даёт пустой mime — UI его не отрендерит. "
        "В v2.2.0 здесь была просто строка-путь."
    )


def test_serialize_mixed_existing_and_missing(tmp_path):
    """Смешанный сценарий: существующие и несуществующий."""
    from workspace.utils.media import serialize

    f1 = tmp_path / "test.md"
    f1.write_bytes(b"# hello")
    missing = tmp_path / "missing.docx"

    result = serialize([str(f1), str(missing)])

    assert len(result) == 2
    assert result[0]["mime_type"], "Первый (существующий) должен иметь mime"
    assert result[1]["mime_type"] == "", "Второй (отсутствующий) — пустой mime"


def _parse_v220_embed_method(v220_src: str) -> str:
    """Извлечь тело метода _embed_media_for_db из v2.2.0 postgres_channel.py."""
    marker = "async def _embed_media_for_db(self, media: list[str])"
    start = v220_src.index(marker)
    next_marker = v220_src.index("async def ", start + 1)
    return v220_src[start:next_marker]


def test_v220_behavior_for_missing_file():
    """Подтвердить поведение v2.2.0: несуществующий файл → просто строка-путь.

    Используем UTF-8 явно, чтобы обойти cp1251 на Windows.
    """
    proc = subprocess.run(
        ["git", "show", "v2.2.0:lib/channels/postgres_channel.py"],
        cwd=str(REPO), capture_output=True, encoding="utf-8",
        errors="replace", check=False,
    )
    v220_src = proc.stdout
    if "_embed_media_for_db" not in v220_src:
        pytest.skip("v2.2.0 без _embed_media_for_db")

    body = _parse_v220_embed_method(v220_src)
    assert "embedded.append(path)" in body, (
        "В v2.2.0 для несуществующего файла вызывался embedded.append(path) — "
        "просто строка-путь. В v2.3.0 это dict с пустым mime."
    )


def test_full_round_trip_existing_files(tmp_path):
    """Полный цикл: создать файл → serialize → deserialize → проверить."""
    from workspace.utils.media import deserialize, serialize
    from utils.session_file_store import SessionFileStore

    f1 = tmp_path / "report.md"
    f1.write_bytes(b"# Report\nContent here.")
    f2 = tmp_path / "data.xlsx"
    f2.write_bytes(b"PK fake xlsx content")

    db_media = serialize([str(f1), str(f2)])
    assert len(db_media) == 2
    for entry in db_media:
        assert entry["mime_type"]
        assert entry["file_size"]

    file_store = SessionFileStore(tmp_path / "cache", attachments_subdir="attachments")
    runtime_media = deserialize(db_media, file_store, session_key="test:1")
    assert len(runtime_media) == 2
    for entry in runtime_media:
        assert isinstance(entry, dict)
        assert entry.get("filename")
        assert entry.get("path")
        decoded_path = Path(entry["path"])
        assert decoded_path.is_file(), f"Deserialized файл не существует: {decoded_path}"
        assert decoded_path.read_bytes() in (b"# Report\nContent here.", b"PK fake xlsx content")


def test_round_trip_keeps_existing_when_some_missing(tmp_path):
    """Сценарий со скрина: .md и .xlsx есть, .docx нет."""
    from workspace.utils.media import deserialize, serialize
    from utils.session_file_store import SessionFileStore

    md = tmp_path / "test.md"
    md.write_bytes(b"# test")
    xlsx = tmp_path / "test.xlsx"
    xlsx.write_bytes(b"PK xlsx")

    db_media = serialize([str(md), str(xlsx), str(tmp_path / "test.docx")])
    assert len(db_media) == 3

    assert db_media[0]["mime_type"] and db_media[0]["file_size"]
    assert db_media[1]["mime_type"] and db_media[1]["file_size"]
    assert db_media[2]["mime_type"] == ""
    assert db_media[2]["file_size"] == 0

    file_store = SessionFileStore(tmp_path / "cache", attachments_subdir="attachments")
    runtime = deserialize(db_media, file_store, session_key="telegram:1")
    assert len(runtime) == 3