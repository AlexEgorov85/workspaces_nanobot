"""Тесты для :func:`diagnose_manifest`.

Покрывает 5 кейсов из ``tasks.md §1.2``:

* manifest не существует → ``reason="not_found"``;
* manifest повреждён → ``reason="corrupted"``;
* manifest с ``version=1`` → ``reason="unsupported_version"`` +
  ``version_observed=1``;
* manifest без поля ``version`` → ``reason="unsupported_version"`` +
  ``version_observed=None``;
* manifest с ``version="abc"`` → ``reason="unsupported_version"`` +
  ``version_observed=None``.

Дополнительный кейс: ``version=2`` → ``reason="ok"`` + ``version_observed=2``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO / "workspace" / "skills" / "legal_summarizer" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
_PROJ = _REPO
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))

from workspace.skills.legal_summarizer.scripts.cache.manifest import (  # noqa: E402
    diagnose_manifest,
    manifest_path,
)


def _workspace_root(tmp_path: Path) -> Path:
    """tmp-каталог в форме корня репо (где лежит ``workspace/``).

    Функция :func:`manifest_path` строит путь относительно ``workspace_root``
    как ``<root>/workspace/data_store/cache/skills/legal_summarizer/<op>/``.
    Чтобы тест не зависел от боевого кеша, делаем фейковый workspace_root
    и кладём manifest туда напрямую.
    """
    ws = tmp_path / "ws"
    (ws / "workspace" / "data_store" / "cache" / "skills" / "legal_summarizer").mkdir(
        parents=True, exist_ok=True,
    )
    return ws


def _write_manifest(tmp_path: Path, op_id: str, payload: dict) -> Path:
    ws = _workspace_root(tmp_path)
    path = manifest_path(op_id, ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_diagnose_manifest_missing(tmp_path: Path) -> None:
    ws = _workspace_root(tmp_path)
    diag = diagnose_manifest("op_not_existing", ws)
    assert diag["reason"] == "not_found"
    assert diag["version_observed"] is None
    assert "raw" not in diag
    assert diag["path"].replace("\\", "/").endswith("op_not_existing/manifest.json")


def test_diagnose_manifest_corrupted(tmp_path: Path) -> None:
    ws = _workspace_root(tmp_path)
    path = manifest_path("op_corrupt", ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")
    diag = diagnose_manifest("op_corrupt", ws)
    assert diag["reason"] == "corrupted"
    assert diag["version_observed"] is None
    assert "raw" not in diag
    assert diag["path"] == str(path)


def test_diagnose_manifest_version_one(tmp_path: Path) -> None:
    ws = _workspace_root(tmp_path)
    _write_manifest(tmp_path, "op_v1", {"version": 1, "operation_id": "op_v1"})
    diag = diagnose_manifest("op_v1", ws)
    assert diag["reason"] == "unsupported_version"
    assert diag["version_observed"] == 1
    assert "raw" not in diag


def test_diagnose_manifest_no_version_field(tmp_path: Path) -> None:
    ws = _workspace_root(tmp_path)
    _write_manifest(tmp_path, "op_no_ver", {"operation_id": "op_no_ver"})
    diag = diagnose_manifest("op_no_ver", ws)
    assert diag["reason"] == "unsupported_version"
    assert diag["version_observed"] is None


def test_diagnose_manifest_version_abc(tmp_path: Path) -> None:
    ws = _workspace_root(tmp_path)
    _write_manifest(tmp_path, "op_abc", {"version": "abc", "operation_id": "op_abc"})
    diag = diagnose_manifest("op_abc", ws)
    assert diag["reason"] == "unsupported_version"
    assert diag["version_observed"] is None


def test_diagnose_manifest_version_v2_ok(tmp_path: Path) -> None:
    """Happy path: v2 manifest → reason='ok', version_observed=2."""
    ws = _workspace_root(tmp_path)
    _write_manifest(tmp_path, "op_v2_ok", {"version": 2, "operation_id": "op_v2_ok"})
    diag = diagnose_manifest("op_v2_ok", ws)
    assert diag["reason"] == "ok"
    assert diag["version_observed"] == 2
    assert "raw" not in diag


def test_diagnose_manifest_does_not_carry_raw() -> None:
    """Даже на ok-пути ``raw`` в dict отсутствует (минимальный контракт)."""
    ws = _workspace_root(Path("/tmp/_unused_diag_path"))  # фактически не читается
    diag = diagnose_manifest("op_unused", ws)
    assert "raw" not in diag
    assert set(diag.keys()) >= {"reason", "path", "version_observed"}


def test_diagnose_manifest_binary_file(tmp_path: Path) -> None:
    """Бинарный файл (не UTF-8) тоже даёт ``corrupted``, а не падает."""
    ws = _workspace_root(tmp_path)
    path = manifest_path("op_bin", ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfe\x00\x01garbage")
    diag = diagnose_manifest("op_bin", ws)
    assert diag["reason"] == "corrupted"
    assert diag["version_observed"] is None
