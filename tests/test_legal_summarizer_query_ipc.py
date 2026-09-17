"""IPC-тесты для ``legal_summarizer_query`` ↔ ``cli_query.py``.

Покрывает scenarios из ``openspec/changes/.../specs/skills/legal-summarizer-query/spec.md``
через моки ``subprocess.run`` (без реального CLI и без LLM).

Сценарии:

(a) exit 0 + ``status="ok"`` → wrapper возвращает payload без обёртки.
(b) exit 1 + ``status="error"`` + ``error_type="manifest_not_found"`` →
    wrapper возвращает тот же envelope без ``cli_failed``.
(c) exit 1 + stdout="" → wrapper возвращает ``cli_failed``.
(d) exit 1 + stdout="not json" → wrapper возвращает ``cli_failed``.
(e) exit 1 + stdout="[]" (валидный JSON-массив) → ``cli_failed``.
(f) exit 1 + stdout='{"status":"ok"}' → ``cli_failed`` (строгая проверка
    ``status == "error"``, а не просто наличие поля).
(g) exit 1 + stdout='{"foo":"bar"}' (dict без ``status``) → ``cli_failed``.
(h) три manifest-причины пробрасываются без обёртки:
    ``manifest_not_found`` / ``manifest_corrupted`` /
    ``manifest_unsupported_version``.
(i) pass-through полей: при exit 1 + ``{"status":"error",
    "error_type":"manifest_unsupported_version", "operation_id":"abc",
    "version_observed":1, "path":"..."}`` итоговый JSON содержит все 5
    полей с исходными значениями.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from workspace.tools.legal_summarizer_query import (  # noqa: E402
    LegalSummarizerQueryTool,
    LegalSummarizerQueryToolConfig,
)


class _FakeCompleted:
    """Ducktype для :class:`subprocess.CompletedProcess`."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.args: list[str] = []


def _make_tool() -> LegalSummarizerQueryTool:
    return LegalSummarizerQueryTool(config=LegalSummarizerQueryToolConfig())


async def _exec_with_mock(completed: _FakeCompleted) -> str:
    """Запустить ``tool.execute`` с подменой ``subprocess.run``."""
    with mock.patch(
        "workspace.tools.legal_summarizer_query.subprocess.run",
        return_value=completed,
    ):
        return await _make_tool().execute(operation_id="op_x")


def _payload(result: str) -> dict[str, Any]:
    return json.loads(result)


def test_a_success_pass_through() -> None:
    """(a) exit 0 + status="ok" → wrapper возвращает payload как есть."""
    ok = {
        "status": "ok",
        "field": "stats",
        "operation_id": "op_x",
        "article_count": 42,
    }
    completed = _FakeCompleted(0, json.dumps(ok, ensure_ascii=False))
    result = asyncio.run(_exec_with_mock(completed))
    payload = _payload(result)
    assert payload["status"] == "ok"
    assert payload["field"] == "stats"
    assert payload["article_count"] == 42
    assert "error_type" not in payload


def test_b_domain_error_manifest_not_found() -> None:
    """(b) exit 1 + manifest_not_found → wrapper возвращает envelope без обёртки."""
    env = {
        "status": "error",
        "error_type": "manifest_not_found",
        "operation_id": "op_x",
        "path": "/cache/op_x/manifest.json",
        "message": "manifest not found",
    }
    completed = _FakeCompleted(1, json.dumps(env))
    result = asyncio.run(_exec_with_mock(completed))
    payload = _payload(result)
    assert payload == env
    assert payload["error_type"] == "manifest_not_found"


def test_c_nonzero_empty_stdout() -> None:
    """(c) exit 1 + stdout="" → cli_failed."""
    completed = _FakeCompleted(1, "", stderr="boom")
    result = asyncio.run(_exec_with_mock(completed))
    payload = _payload(result)
    assert payload["status"] == "error"
    assert payload["error_type"] == "cli_failed"
    assert "boom" in payload["message"]


def test_d_nonzero_not_json() -> None:
    """(d) exit 1 + stdout='not json' → cli_failed."""
    completed = _FakeCompleted(1, "garbage")
    result = asyncio.run(_exec_with_mock(completed))
    payload = _payload(result)
    assert payload["error_type"] == "cli_failed"


def test_e_nonzero_json_array() -> None:
    """(e) exit 1 + stdout='[]' (валидный JSON-массив, не dict) → cli_failed."""
    completed = _FakeCompleted(1, "[]")
    result = asyncio.run(_exec_with_mock(completed))
    payload = _payload(result)
    assert payload["error_type"] == "cli_failed"


def test_f_nonzero_status_ok_strict() -> None:
    """(f) exit 1 + {'status':'ok'} (наличие status, но != 'error') → cli_failed.

    Строгая проверка ``status == 'error'``, а не просто наличия поля.
    """
    completed = _FakeCompleted(1, json.dumps({"status": "ok"}))
    result = asyncio.run(_exec_with_mock(completed))
    payload = _payload(result)
    assert payload["status"] == "error"
    assert payload["error_type"] == "cli_failed"


def test_g_nonzero_dict_no_status() -> None:
    """(g) exit 1 + dict без status → cli_failed."""
    completed = _FakeCompleted(1, json.dumps({"foo": "bar"}))
    result = asyncio.run(_exec_with_mock(completed))
    payload = _payload(result)
    assert payload["error_type"] == "cli_failed"


@pytest.mark.parametrize(
    "error_type",
    ["manifest_not_found", "manifest_corrupted", "manifest_unsupported_version"],
)
def test_h_three_manifest_errors_propagated(error_type: str) -> None:
    """(h) три доменных manifest-причины пробрасываются без обёртки."""
    env = {
        "status": "error",
        "error_type": error_type,
        "operation_id": "op_x",
        "path": "/cache/op_x/manifest.json",
        "version_observed": 1 if error_type == "manifest_unsupported_version" else None,
        "message": f"{error_type} happened",
    }
    completed = _FakeCompleted(1, json.dumps(env))
    result = asyncio.run(_exec_with_mock(completed))
    payload = _payload(result)
    assert payload == env
    assert payload["error_type"] == error_type
    assert payload["status"] == "error"


def test_i_pass_through_preserves_all_fields() -> None:
    """(i) pass-through: 5 полей из CLI-envelope сохранены в результате."""
    env = {
        "status": "error",
        "error_type": "manifest_unsupported_version",
        "operation_id": "abc",
        "version_observed": 1,
        "path": "/cache/abc/manifest.json",
    }
    completed = _FakeCompleted(1, json.dumps(env))
    result = asyncio.run(_exec_with_mock(completed))
    payload = _payload(result)
    assert set(payload.keys()) == set(env.keys())
    assert payload["error_type"] == "manifest_unsupported_version"
    assert payload["operation_id"] == "abc"
    assert payload["version_observed"] == 1
    assert payload["path"] == "/cache/abc/manifest.json"
    assert payload["status"] == "error"


def test_wrapper_timeout_remains_unchanged() -> None:
    """Регрессия: при ``TimeoutExpired`` wrapper всё ещё отдаёт ``timeout``."""
    tool = _make_tool()
    with mock.patch(
        "workspace.tools.legal_summarizer_query.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["x"], timeout=60, stderr="t/o"),
    ):
        result = asyncio.run(tool.execute(operation_id="op_x"))
    payload = _payload(result)
    assert payload["error_type"] == "timeout"


def test_wrapper_empty_response_unchanged() -> None:
    """Регрессия: exit 0 + пустой stdout → ``empty_response`` (не cli_failed)."""
    completed = _FakeCompleted(0, "")
    result = asyncio.run(_exec_with_mock(completed))
    payload = _payload(result)
    assert payload["error_type"] == "empty_response"


def test_wrapper_invalid_json_unchanged() -> None:
    """Регрессия: exit 0 + невалидный JSON → ``invalid_json``."""
    completed = _FakeCompleted(0, "<<<not json>>>")
    result = asyncio.run(_exec_with_mock(completed))
    payload = _payload(result)
    assert payload["error_type"] == "invalid_json"
