"""Тесты ``tools/check_indexes.py``.

Сравнение declared (JSON) vs runtime (PG store):
  * ``_diff`` детерминированно классифицирует расхождение;
  * exit codes согласуются с планом из комментария ``tools/check_indexes.py``:
      ``0`` OK / ``1`` divergence / ``2`` infra error.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_TOOL = str(_REPO / "tools" / "check_indexes.py")


# ============================================================================
# Stubs: PG fetch + JSON read
# ============================================================================


def _store_row(name: str = "audits_index",
               vector_count: int = 429,
               dimension: int = 1024,
               metric: str = "cosine",
               signature: str | None = "abcd" * 16,
               updated_at=None,
               metadata_extra: dict | None = None) -> dict:
    """Имитация строки из public.agent_vector_index_store."""
    meta = {
        "metric": metric,
        "chunk_size": 500,
        "chunk_overlap": 80,
    }
    if signature is not None:
        meta["signature"] = signature
    if metadata_extra:
        meta.update(metadata_extra)
    return {
        "source": name,
        "dimension": dimension,
        "vector_count": vector_count,
        "updated_at": updated_at or "2026-09-14T10:23:00+00:00",
        "metadata": meta,
    }


def _declared_cfg(
    name: str = "audits_index",
    source_table: str = "oarb.audits",
    chunk_size: int = 500,
    chunk_overlap: int = 80,
    dimension: int = 1024,
    extra: dict | None = None,
) -> dict:
    """Имитация секции gateway.vector.index.indexes.<name> в project.json."""
    base = {
        "table": source_table.replace(".", "_"),
        "pk": "id",
        "source_table": source_table,
        "content_columns": ["title", "audit_type"],
        "embedding_columns": ["title_emb", "audit_type_emb"],
        "track_column": "updated_at",
        "chunk_size": chunk_size,
        "chunk_overlap": chunk_overlap,
        "metric": "cosine",
        "enabled": True,
        # поля для signature (см. ``_INDEX_SIGNATURE_FIELDS``):
        "embedding_model": "mxbai-embed-large",
        "dimension": dimension,
    }
    if extra:
        base.update(extra)
    return {name: base}


# ============================================================================
# Тесты чистой _diff
# ============================================================================


def _import_tool():
    """Импортировать ``check_indexes`` как модуль без sys.path-возни."""
    import importlib.util

    if str(_REPO) not in sys.path:
        sys.path.insert(0, str(_REPO))
    spec = importlib.util.spec_from_file_location("check_indexes", _TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_diff_ok_when_declared_matches_runtime() -> None:
    """Все declared индексы имеют CURRENT signature в PG store."""
    mod = _import_tool()
    declared = _declared_cfg()
    # Ровно та же конфигурация → signature CURRENT.
    from lib.services.cache_provider_impl import (
        compute_index_signature,
    )
    sig = compute_index_signature(declared["audits_index"])
    runtime = [_store_row(name="audits_index", signature=sig)]
    out = mod._diff(declared, runtime)
    assert out["status"] == "OK"
    assert out["divergence"]["missing_in_runtime"] == []
    assert out["divergence"]["orphan_in_runtime"] == []
    assert out["divergence"]["stale_or_invalid"] == []


def test_diff_missing_in_runtime() -> None:
    """Объявлено в JSON, но в PG store нет записи → divergence."""
    mod = _import_tool()
    declared = _declared_cfg()
    # runtime пустой → все declared становятся missing.
    out = mod._diff(declared, [])
    assert out["status"] == "DIVERGENCE"
    names = [m["name"] for m in out["divergence"]["missing_in_runtime"]]
    assert names == ["audits_index"]
    assert out["divergence"]["orphan_in_runtime"] == []
    assert out["divergence"]["stale_or_invalid"] == []


def test_diff_orphan_in_runtime() -> None:
    """Есть blob в PG, но в JSON больше не объявлено → divergence."""
    mod = _import_tool()
    runtime = [_store_row(name="legacy_index", vector_count=10)]
    out = mod._diff({}, runtime)
    assert out["status"] == "DIVERGENCE"
    assert out["divergence"]["missing_in_runtime"] == []
    names = [o["name"] for o in out["divergence"]["orphan_in_runtime"]]
    assert names == ["legacy_index"]
    assert out["divergence"]["stale_or_invalid"] == []


def test_diff_stale_signature_diverges() -> None:
    """Blob есть, но signature не совпадает с текущим cfg → STALE → divergence."""
    mod = _import_tool()
    declared = _declared_cfg()
    # stored signature — другой (мусорный hex), но длиной 64 → STALE;
    # cf. verify_index_signature: хранимый sig != текущий.
    stale_sig = "f" * 64
    runtime = [_store_row(name="audits_index", signature=stale_sig)]
    out = mod._diff(declared, runtime)
    assert out["status"] == "DIVERGENCE"
    items = out["divergence"]["stale_or_invalid"]
    assert len(items) == 1
    assert items[0]["name"] == "audits_index"
    assert items[0]["status"] == "STALE"
    assert items[0]["stored_signature"] == "f" * 16
    assert items[0]["current_signature"]  # computed from cfg


def test_diff_invalid_signature_diverges() -> None:
    """Blob есть, signature отсутствует в metadata → INVALID → divergence."""
    mod = _import_tool()
    declared = _declared_cfg()
    runtime = [_store_row(name="audits_index", signature=None)]
    out = mod._diff(declared, runtime)
    assert out["status"] == "DIVERGENCE"
    items = out["divergence"]["stale_or_invalid"]
    assert len(items) == 1
    assert items[0]["status"] == "INVALID"


def test_diff_handles_orphan_signature_unknown_status() -> None:
    """Orphan без cfg → signature_status нельзя вычислить (UNKNOWN, но не divergence)."""
    mod = _import_tool()
    runtime = [_store_row(name="orphan_index", signature=None)]
    out = mod._diff({}, runtime)
    assert out["status"] == "DIVERGENCE"
    # Это ORPHAN (расхождение по orphan), не STALE/INVALID.
    assert out["divergence"]["orphan_in_runtime"][0]["name"] == "orphan_index"
    assert "stale_or_invalid" not in out["divergence"] or out["divergence"]["stale_or_invalid"] == []


def test_diff_infra_error_when_runtime_is_exception() -> None:
    """Если runtime бросил исключение → INFRA_ERROR (не divergence)."""
    mod = _import_tool()
    declared = _declared_cfg()
    out = mod._diff(declared, RuntimeError("PG is down"))
    assert out["status"] == "INFRA_ERROR"
    assert "PG is down" in out["divergence"]["error"]


def test_diff_infra_error_when_declared_is_exception() -> None:
    """Если declared упал → INFRA_ERROR, runtime skipped."""
    mod = _import_tool()
    out = mod._diff(RuntimeError("config parse error"), [_store_row()])
    assert out["status"] == "INFRA_ERROR"
    assert "config parse error" in out["divergence"]["error"]
    assert out["runtime"]["source"] == "error"


# ============================================================================
# Текстовый формат
# ============================================================================


def test_format_text_shows_ok_message() -> None:
    mod = _import_tool()
    declared = _declared_cfg()
    from lib.services.cache_provider_impl import compute_index_signature

    sig = compute_index_signature(declared["audits_index"])
    out = mod._diff(declared, [_store_row(name="audits_index", signature=sig)])
    text = mod._format_text(out)
    assert "OK" in text
    assert "audits_index" in text


def test_format_text_shows_missing_label() -> None:
    mod = _import_tool()
    out = mod._diff(_declared_cfg(), [])
    text = mod._format_text(out)
    assert "MISSING" in text
    assert "audits_index" in text


def test_format_text_shows_orphan_label() -> None:
    mod = _import_tool()
    out = mod._diff({}, [_store_row(name="legacy_index")])
    text = mod._format_text(out)
    assert "ORPHAN" in text
    assert "legacy_index" in text


def test_format_text_shows_stale_label() -> None:
    mod = _import_tool()
    out = mod._diff(_declared_cfg(), [_store_row(name="audits_index", signature="f" * 64)])
    text = mod._format_text(out)
    assert "STALE" in text
    assert "rebuild via" in text  # см. текст reason в _diff


# ============================================================================
# CLI (subprocess): exit codes + JSON mode
# ============================================================================


def _cli_run(*args: str, env_override: dict | None = None) -> subprocess.CompletedProcess:
    """Запустить ``tools/check_indexes.py`` как подпроцесс."""
    return subprocess.run(
        [sys.executable, _TOOL, *args],
        cwd=str(_REPO),
        env=env_override,
        capture_output=True,
        text=True,
    )


def test_cli_exit_2_when_pg_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """PG недоступна → exception в list_runtime_vector_indexes → exit 2.

    Чтобы не поднимать PG, делаем ``monkeypatch.setattr`` так, чтобы
    fetch_fn бросал исключение сразу при вызове. Делаем это на уровне
    helper'а ``list_runtime_vector_indexes`` через ENV-переменную,
    которая доступна тесту; используем просто monkeypatch на сам
    helper в импортированном модуле.
    """
    mod = _import_tool()
    monkeypatch.setattr(
        mod, "_load_runtime",
        lambda: RuntimeError("connection refused"),
    )
    monkeypatch.setattr(
        mod, "_load_declared",
        lambda: _declared_cfg(),
    )
    rc = mod.main([])
    assert rc == 2


def test_cli_exit_1_on_divergence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Declared но runtime пустой → MISSING → exit 1."""
    mod = _import_tool()
    monkeypatch.setattr(
        mod, "_load_declared",
        lambda: _declared_cfg(),
    )
    monkeypatch.setattr(
        mod, "_load_runtime",
        lambda: [],
    )
    rc = mod.main([])
    assert rc == 1


def test_cli_exit_0_on_full_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """Declared + runtime совпадают, signature CURRENT → exit 0."""
    mod = _import_tool()
    declared = _declared_cfg()
    from lib.services.cache_provider_impl import compute_index_signature

    sig = compute_index_signature(declared["audits_index"])
    monkeypatch.setattr(mod, "_load_declared", lambda: declared)
    monkeypatch.setattr(mod, "_load_runtime", lambda: [_store_row(name="audits_index", signature=sig)])
    rc = mod.main([])
    assert rc == 0


def test_cli_json_mode_outputs_valid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """--json режим → валидный JSON, который можно парсить."""
    mod = _import_tool()
    monkeypatch.setattr(mod, "_load_declared", lambda: _declared_cfg())
    monkeypatch.setattr(mod, "_load_runtime", lambda: [])

    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = mod.main(["--json"])
    parsed = json.loads(buf.getvalue())
    assert parsed["status"] == "DIVERGENCE"
    assert "missing_in_runtime" in parsed["divergence"]
    assert rc == 1


def test_cli_text_mode_human_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Без --json → человеко-читаемый текст с понятными метками."""
    mod = _import_tool()
    monkeypatch.setattr(mod, "_load_declared", lambda: _declared_cfg())
    monkeypatch.setattr(mod, "_load_runtime", lambda: [])

    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = mod.main([])
    text = buf.getvalue()
    assert "MISSING" in text
    assert "audits_index" in text
    assert rc == 1
