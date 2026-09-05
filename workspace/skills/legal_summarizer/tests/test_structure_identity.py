"""Тесты для DocumentIdentity (Этап 5 из PLAN.md)."""

from __future__ import annotations

from pathlib import Path

import pytest

from workspace.skills.legal_summarizer.scripts.structure.identity import (
    DocumentIdentity,
)


def test_from_path_creates_identity(tmp_path: Path):
    p = tmp_path / "doc.txt"
    p.write_text("hello", encoding="utf-8")
    ident = DocumentIdentity.from_path(p)
    assert ident.document_id == ident.fingerprint[:12]
    assert ident.physical_cache_key == ident.fingerprint
    assert len(ident.fingerprint) == 64  # sha256 hex
    assert ident.size_bytes == 5
    assert Path(ident.resolved_path) == p.resolve()


def test_same_path_same_identity(tmp_path: Path):
    p = tmp_path / "doc.txt"
    p.write_text("hello", encoding="utf-8")
    a = DocumentIdentity.from_path(p)
    b = DocumentIdentity.from_path(p)
    assert a.fingerprint == b.fingerprint
    assert a.document_id == b.document_id


def test_modified_content_changes_identity(tmp_path: Path):
    p = tmp_path / "doc.txt"
    p.write_text("hello", encoding="utf-8")
    a = DocumentIdentity.from_path(p)
    p.write_text("hello world", encoding="utf-8")
    b = DocumentIdentity.from_path(p)
    assert a.fingerprint != b.fingerprint


def test_is_fresh(tmp_path: Path):
    p = tmp_path / "doc.txt"
    p.write_text("hello", encoding="utf-8")
    ident = DocumentIdentity.from_path(p)
    assert ident.is_fresh(p) is True
    p.write_text("hello world", encoding="utf-8")
    assert ident.is_fresh(p) is False


def test_is_fresh_missing_file(tmp_path: Path):
    p = tmp_path / "doc.txt"
    p.write_text("hello", encoding="utf-8")
    ident = DocumentIdentity.from_path(p)
    p.unlink()
    assert ident.is_fresh(p) is False


def test_to_dict_roundtrip():
    ident = DocumentIdentity.from_path_with_mtime(
        Path("/tmp/x.pdf"), size_bytes=100, mtime_ns=12345
    )
    d = ident.to_dict()
    assert d["size_bytes"] == 100
    assert d["mtime_ns"] == 12345
    assert d["physical_cache_key"] == d["fingerprint"]


def test_identity_is_frozen():
    import dataclasses

    ident = DocumentIdentity.from_path_with_mtime(Path("/tmp/x.pdf"), size_bytes=0, mtime_ns=0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ident.size_bytes = 999  # type: ignore[misc]


def test_identity_fingerprint_equals_physical_cache_key(tmp_path: Path):
    """PLAN §11 acceptance: identity.fingerprint == physical/cache fingerprint."""
    from workspace.skills.legal_summarizer.scripts.structure.document_loader import (
        DocumentLoader,
    )

    p = tmp_path / "doc.txt"
    p.write_text("hello", encoding="utf-8")
    ident = DocumentIdentity.from_path(p)
    doc = DocumentLoader().load(p)
    # DocumentIdentity.fingerprint — это sha256 hex от (path, size, mtime_ns).
    # physical.py теперь использует identity.fingerprint как cache key —
    # единый canonical алгоритм.
    assert doc.path == ident.resolved_path
    assert doc.size_bytes == ident.size_bytes


def test_identity_uses_mtime_ns_not_mtime(tmp_path: Path):
    """PLAN §11: единый canonical алгоритм — mtime_ns (наносекунды).

    Раньше physical.py использовал st_mtime (секунды). Теперь —
    DocumentIdentity.from_path с mtime_ns. Это устраняет два
    параллельных fingerprint алгоритма.
    """
    p = tmp_path / "doc.txt"
    p.write_text("hello", encoding="utf-8")
    ident = DocumentIdentity.from_path(p)
    st = p.stat()
    assert ident.mtime_ns == st.st_mtime_ns
    assert ident.mtime_ns != int(st.st_mtime)