"""Интеграционные тесты IPC-контракта ``cli_query.py`` ↔ ``legal_summarizer_query``.

Закрывает acceptance criterion ``tasks.md §4.1(h)``: «три новых
manifest-причины пробрасываются (manifest_not_found /
manifest_corrupted / manifest_unsupported_version) — каждый как
отдельный кейс с реальным файлом во временной директории».

В отличие от ``tests/test_legal_summarizer_query_ipc.py`` (unit-тесты
wrapper'а с моками ``subprocess.run``), здесь тесты идут **полным
путём**:

    manifest.json на диске
        ↓
    cli_query.py (реальный subprocess)
        ↓ exit 1
    structured JSON в stdout
        ↓
    legal_summarizer_query wrapper (с реальным subprocess.run)
        ↓
    pass-through JSON-строка без обёртки ``cli_failed``

Для минимизации сложности (избежать двойного subprocess) некоторые
сценарии делают только ``cli_query.py`` subprocess и проверяют,
что CLI возвращает корректный ``error_type`` + ``exit 1`` на
соответствующий файл. Полный путь wrapper+cli покрыт в
``test_full_path_through_wrapper``.

Требует ``config.json`` со включённым skill'ом ``legal_summarizer``
(не требует реального LLM — путь только до диагностики manifest).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO / "workspace" / "skills" / "legal_summarizer" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

_CLI = _REPO / "workspace" / "skills" / "legal_summarizer" / "scripts" / "cli_query.py"


def _workspace_root(tmp_path: Path) -> Path:
    """tmp-каталог в форме корня репо (где лежит ``workspace/``).

    ``manifest_path`` строит ``<root>/workspace/data_store/cache/skills/legal_summarizer/<op>/``.
    Чтобы тест не зависел от боевого кеша, делаем фейковый workspace_root.
    Также копируем ``cli_query.py`` **и весь его ``cache/`` подкаталог**
    в ``<root>/workspace/skills/legal_summarizer/scripts/``:

    * без копии wrapper возвращает ``cli_not_found`` (он ищет CLI по
      абсолютному пути ``workspace_root/workspace/skills/.../cli_query.py``);
    * без ``cache/`` (только cli_query.py) импорт ``from cache.manifest``
      в скопированном CLI ломается с ``ModuleNotFoundError: cache``, и
      CLI падает до диагностики.

    После такой копии CLI получает свой собственный ``cache.manifest``
    (включая ``diagnose_manifest``) и изолирован от боевого кеша.
    """
    import shutil

    (tmp_path / "workspace" / "data_store" / "cache" / "skills" / "legal_summarizer").mkdir(
        parents=True, exist_ok=True,
    )
    scripts_dest = tmp_path / "workspace" / "skills" / "legal_summarizer" / "scripts"
    scripts_dest.mkdir(parents=True, exist_ok=True)
    if not (scripts_dest / "cli_query.py").is_file():
        shutil.copy2(_CLI, scripts_dest / "cli_query.py")
    cache_src = _SCRIPTS / "cache"
    cache_dest = scripts_dest / "cache"
    if cache_src.is_dir() and not cache_dest.is_dir():
        shutil.copytree(cache_src, cache_dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return tmp_path


def _write_manifest(tmp_path: Path, op_id: str, payload) -> None:
    """Записать manifest-файл. Если ``payload is bytes`` — записать как байты."""
    ws = _workspace_root(tmp_path)
    op_dir = ws / "workspace" / "data_store" / "cache" / "skills" / "legal_summarizer" / op_id
    op_dir.mkdir(parents=True, exist_ok=True)
    path = op_dir / "manifest.json"
    if isinstance(payload, bytes):
        path.write_bytes(payload)
    elif isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _run_cli(workspace_root: Path, op_id: str) -> subprocess.CompletedProcess:
    """Запустить cli_query.py как реальный subprocess."""
    return subprocess.run(
        [
            sys.executable,
            str(_CLI),
            "--operation-id", op_id,
            "--field", "stats",
            "--workspace-root", str(workspace_root),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(workspace_root),
        timeout=30,
        check=False,
    )


# ---------------------------------------------------------------------------
# cli_query.py как самостоятельный subprocess: проверяем, что CLI
# действительно выдаёт нужный error_type и exit=1 для каждой причины.
# ---------------------------------------------------------------------------


def test_cli_emits_manifest_not_found(tmp_path: Path) -> None:
    """(h.1) Файла нет → CLI возвращает ``manifest_not_found`` + ``exit 1``."""
    ws = _workspace_root(tmp_path)
    completed = _run_cli(ws, "op_no_such_file")
    assert completed.returncode == 1
    payload = json.loads(completed.stdout)
    assert payload["status"] == "error"
    assert payload["error_type"] == "manifest_not_found"
    assert payload["operation_id"] == "op_no_such_file"
    assert payload["path"].replace("\\", "/").endswith("op_no_such_file/manifest.json")
    assert payload["version_observed"] is None


def test_cli_emits_manifest_corrupted(tmp_path: Path) -> None:
    """(h.2) Битый JSON → CLI возвращает ``manifest_corrupted`` + ``exit 1``."""
    ws = _workspace_root(tmp_path)
    _write_manifest(tmp_path, "op_corrupt", "{not valid json")
    completed = _run_cli(ws, "op_corrupt")
    assert completed.returncode == 1
    payload = json.loads(completed.stdout)
    assert payload["status"] == "error"
    assert payload["error_type"] == "manifest_corrupted"
    assert payload["operation_id"] == "op_corrupt"
    assert payload["path"].replace("\\", "/").endswith("op_corrupt/manifest.json")
    assert payload["version_observed"] is None


def test_cli_emits_manifest_corrupted_for_binary(tmp_path: Path) -> None:
    """Бинарный файл (UTF-8 fail) → ``manifest_corrupted``, не падает."""
    ws = _workspace_root(tmp_path)
    _write_manifest(tmp_path, "op_binary", b"\xff\xfe\x00\x01garbage")
    completed = _run_cli(ws, "op_binary")
    assert completed.returncode == 1
    payload = json.loads(completed.stdout)
    assert payload["error_type"] == "manifest_corrupted"


def test_cli_emits_manifest_unsupported_version_v1(tmp_path: Path) -> None:
    """(h.3a) ``version=1`` → ``manifest_unsupported_version`` + ``version_observed=1``."""
    ws = _workspace_root(tmp_path)
    _write_manifest(tmp_path, "op_v1", {"version": 1, "operation_id": "op_v1"})
    completed = _run_cli(ws, "op_v1")
    assert completed.returncode == 1
    payload = json.loads(completed.stdout)
    assert payload["status"] == "error"
    assert payload["error_type"] == "manifest_unsupported_version"
    assert payload["version_observed"] == 1
    assert payload["operation_id"] == "op_v1"


def test_cli_emits_manifest_unsupported_version_abc(tmp_path: Path) -> None:
    """(h.3b) ``version="abc"`` (non-integer) → ``manifest_unsupported_version``
    + ``version_observed=None``."""
    ws = _workspace_root(tmp_path)
    _write_manifest(tmp_path, "op_abc", {"version": "abc", "operation_id": "op_abc"})
    completed = _run_cli(ws, "op_abc")
    assert completed.returncode == 1
    payload = json.loads(completed.stdout)
    assert payload["error_type"] == "manifest_unsupported_version"
    assert payload["version_observed"] is None


def test_cli_emits_manifest_unsupported_version_missing(tmp_path: Path) -> None:
    """(h.3c) Без поля version → ``manifest_unsupported_version`` + ``version_observed=None``."""
    ws = _workspace_root(tmp_path)
    _write_manifest(tmp_path, "op_no_ver", {"operation_id": "op_no_ver"})
    completed = _run_cli(ws, "op_no_ver")
    assert completed.returncode == 1
    payload = json.loads(completed.stdout)
    assert payload["error_type"] == "manifest_unsupported_version"
    assert payload["version_observed"] is None


# ---------------------------------------------------------------------------
# Полный путь wrapper ↔ CLI: проверяем, что legal_summarizer_query
# пробрасывает доменную ошибку as-is, без собственного envelope.
# ---------------------------------------------------------------------------


def _run_wrapper(workspace_root: Path, op_id: str) -> str:
    """Запустить wrapper ``legal_summarizer_query`` против реального CLI."""
    import asyncio
    from workspace.tools.legal_summarizer_query import (
        LegalSummarizerQueryTool,
        LegalSummarizerQueryToolConfig,
    )

    config = LegalSummarizerQueryToolConfig(workspace_root=str(workspace_root))
    tool = LegalSummarizerQueryTool(config=config)
    # wrapper тратит до 60 секунд по умолчанию; здесь subprocess CLI быстрый.
    return asyncio.run(
        tool.execute(operation_id=op_id, field="stats", max_chunk_summary_chars=1500)
    )


def test_full_path_through_wrapper_manifest_not_found(tmp_path: Path) -> None:
    """Полный путь wrapper+CLI: ``manifest_not_found`` пробрасывается as is."""
    ws = _workspace_root(tmp_path)
    result = _run_wrapper(ws, "op_x_missing")
    payload = json.loads(result)
    assert payload["status"] == "error"
    assert payload["error_type"] == "manifest_not_found"
    assert "cli_failed" not in str(payload).lower(), (
        "wrapper не должен ставить cli_failed поверх domain error"
    )


def test_full_path_through_wrapper_manifest_corrupted(tmp_path: Path) -> None:
    """Полный путь wrapper+CLI: ``manifest_corrupted`` пробрасывается as is."""
    ws = _workspace_root(tmp_path)
    _write_manifest(tmp_path, "op_x_corrupt", "{not valid json")
    result = _run_wrapper(ws, "op_x_corrupt")
    payload = json.loads(result)
    assert payload["error_type"] == "manifest_corrupted"


def test_full_path_through_wrapper_manifest_unsupported_version(tmp_path: Path) -> None:
    """Полный путь wrapper+CLI: ``manifest_unsupported_version`` пробрасывается."""
    ws = _workspace_root(tmp_path)
    _write_manifest(tmp_path, "op_x_v1", {"version": 1, "operation_id": "op_x_v1"})
    result = _run_wrapper(ws, "op_x_v1")
    payload = json.loads(result)
    assert payload["error_type"] == "manifest_unsupported_version"
    assert payload["version_observed"] == 1
