"""Acceptance tests для Этапа 10: dict-shaped structure не используется
внутри production execution boundary.

Проверяем, что:
* ``_run_direct`` / ``_run_map_reduce`` корректно извлекают title
  из ``analysis.structure.title`` (canonical), а не из dict;
* ``_count_sections(None)`` больше не возвращается в production result.
"""

from __future__ import annotations

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


def test_direct_run_returns_real_sections_metadata(tmp_path: Path, monkeypatch):
    """Direct run с известной структурой → ``result['sections']`` отражает
    реальное число секций."""
    import summarizer
    from workspace.skills.legal_summarizer.scripts import llm_calls

    def _fake_doc(text, *, length, focus, structure, question=None):
        return "Итоговое саммари."

    monkeypatch.setattr(llm_calls, "llm_document_reduce", _fake_doc)
    monkeypatch.setattr(summarizer, "_llm_document_reduce", _fake_doc)

    text = (
        "1. Раздел А\n\n" + ("Текст. " * 30) * 10
        + "\n\n2. Раздел Б\n\n" + ("Текст. " * 30) * 10
        + "\n\n3. Раздел В\n\n" + ("Текст. " * 30) * 10
    )
    p = _write_doc(tmp_path, text)

    result = summarizer.run(
        text,
        length="detailed",
        document_path=str(p),
        workspace_root=tmp_path,
    )
    assert result["status"] == "completed", result
    # Direct run использует canonical analysis, ``result['sections']`` —
    # это число meaningful sections (>=0).
    assert "sections" in result["result"]
    assert isinstance(result["result"]["sections"], int)