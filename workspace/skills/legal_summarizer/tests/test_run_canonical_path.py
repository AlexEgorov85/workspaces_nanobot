"""Canonical run() path test (PLAN §13c, §32).

Проверяет, что ``summarizer.run`` использует единственный canonical
путь: inspect → ``select_strategy`` → ``_run_direct`` / ``_run_map_reduce``
(без legacy ``_legacy_run_map_reduce``, без doc_cache).

Мокируют только ``llm.chat`` — весь остальной конвейер (chunking,
execution plan, manifest, partials) выполняется реально.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def _write_doc(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "doc.txt"
    p.write_text(text, encoding="utf-8")
    return p


def _fake_llm(messages, *, context=None, **kwargs):
    """Универсальный mock ``llm.chat``: batch / section / document reduce."""
    user = messages[-1]["content"]

    batch_markers = re.findall(r"(?m)^\s*DOCUMENT CHUNK \d+\s*\n", user)
    if batch_markers:
        n = len(batch_markers)
        return "\n\n".join(
            f"DOCUMENT CHUNK {i + 1}: краткое саммари части {i + 1}" 
            for i in range(n)
        )

    if "Объединённые краткие описания частей раздела" in user:
        return "Итоговое описание раздела."

    return "Итоговое саммари документа."


def test_run_direct_short_doc(tmp_path: Path, monkeypatch):
    """Короткий документ → strategy 'direct', один document_reduce call."""
    import summarizer

    monkeypatch.setattr("llm.chat", _fake_llm)

    text = "1. Общие положения\n\nКороткий текст договора для прямого пути."
    p = _write_doc(tmp_path, text)

    result = summarizer.run(
        text,
        length="detailed",
        document_path=str(p),
        workspace_root=tmp_path,
    )
    assert result["status"] == "completed"
    assert result["result"]["strategy"] == "direct"
    assert result["stats"]["document_reduce_calls"] == 1
    assert result["stats"]["map_calls"] == 0
    assert result["result"]["summary"]


def test_run_map_flat_long_doc(tmp_path: Path, monkeypatch):
    """Длинный документ без секций → strategy 'map_reduce_flat'."""
    import summarizer

    monkeypatch.setattr("llm.chat", _fake_llm)

    long_block = ("Текст договора. " * 120) * 60
    text = "1. Пункт\n\n" + long_block
    p = _write_doc(tmp_path, text)

    result = summarizer.run(
        text,
        length="detailed",
        document_path=str(p),
        workspace_root=tmp_path,
        confirmed=True,
    )
    assert result["status"] in ("completed", "partial")
    assert result["result"]["strategy"] == "map_reduce_flat"
    assert result["stats"]["map_calls"] >= 1
    assert result["stats"]["document_reduce_calls"] == 1
    assert result["result"]["summary"]


def test_run_no_legacy_re_exports():
    """run() не зависит от удалённых legacy re-export'ов summarizer."""
    import summarizer

    assert not hasattr(summarizer, "_legacy_run_map_reduce")
    assert not hasattr(summarizer, "_doc_cache_dir")
    assert not hasattr(summarizer, "load_structure")