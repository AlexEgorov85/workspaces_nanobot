"""Тесты #6: ``--question`` через document-level cache.

Проверяется:

1. После успешного ``--length detailed`` прогона → document-cache заполнен.
2. ``--question`` с тем же файлом → cache hit → НЕТ нового llm_batch,
   один llm_document_reduce call для synthesis.
3. ``--question`` с file, у которого НЕТ document-cache → fallthrough
   на обычный pipeline (новый map-reduce).
4. ``--question`` без ``document_path`` → fallthrough (regression-guard).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SKILL_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def _write_doc(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "doc.txt"
    p.write_text(text, encoding="utf-8")
    return p


def _build_doc_with_marker(sections: int = 4) -> str:
    """Документ с маркером 'контрагентство' в одной из секций."""
    parts = []
    for i in range(1, sections + 1):
        body = "Общие положения и порядок действий раздела. " * 30
        if i == 2:
            body += " Специальное правило: контрагентство применений."
        parts.append(f"{i}. Раздел {i}\n\n{body * 20}\n\n")
    return "".join(parts)


def _install_recording_llm(monkeypatch) -> dict:
    """Mock LLM: возвращает предсказуемые строки, считает вызовы."""
    import llm.calls as llm_calls
    recorded = {"batch": 0, "section_reduce": 0, "doc_reduce": 0}

    def _fake_batch(chunks, *, chunks_total, structure, length, question=None):
        recorded["batch"] += 1
        return {c.chunk_id: f"chunk_summary_{c.chunk_id}" for c in chunks}

    def _fake_section(path, heading, text, *, length, question=None):
        recorded["section_reduce"] += 1
        return f"section_summary[{heading}]"

    def _fake_doc(text, *, length, focus, structure, question=None):
        recorded["doc_reduce"] += 1
        # Возвращаем строку с маркером вопроса — для assertion.
        if question and "контрагентство" in question:
            return f"answer_with_marker_{recorded['doc_reduce']}"
        return f"final_answer_{recorded['doc_reduce']}"

    monkeypatch.setattr(llm_calls, "llm_batch", _fake_batch)
    monkeypatch.setattr(llm_calls, "llm_section_reduce", _fake_section)
    monkeypatch.setattr(llm_calls, "llm_document_reduce", _fake_doc)
    return recorded


def test_first_run_writes_document_cache(tmp_path, monkeypatch):
    """Первый run (--length) пишет document-cache."""
    import application.service as summarizer
    _install_recording_llm(monkeypatch)

    text = _build_doc_with_marker(sections=4)
    p = _write_doc(tmp_path, text)

    result = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result["status"] in ("completed", "partial"), result

    # document-cache должен быть заполнен (через _write_document_snapshot_after_pipeline).
    from cache.manifest import is_document_cache_complete
    from document.identity import DocumentIdentity
    document_id = DocumentIdentity.from_path(p).document_id
    assert is_document_cache_complete(document_id, tmp_path)


def test_question_after_cache_hit_uses_document_cache(tmp_path, monkeypatch):
    """``--question`` после document-cache hit: НЕТ llm_batch.

    Используем длинный документ (несколько секций с большим объёмом),
    чтобы ``run_map_reduce`` отработал (а не ``run_direct``). Для txt
    без headings стратегия скорее всего будет ``map_flat``, и тогда
    в первом run ``llm_batch`` вызывается.
    """
    import application.service as summarizer
    recorded = _install_recording_llm(monkeypatch)

    # Большой текст → map стратегия.
    parts = []
    for i in range(1, 7):
        parts.append(
            f"{i}. Раздел {i}\n\n"
            + ("Текст. " * 50) * 200
            + "\n\n"
        )
    text = "".join(parts)
    p = _write_doc(tmp_path, text)

    # 1. Первый run: --length detailed → cache miss → llm_batch (map).
    result1 = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result1["status"] in ("completed", "partial")
    # На map (flat/hierarchical) llm_batch должен вызваться.
    assert recorded["batch"] >= 1, (
        f"first run must call llm_batch for map strategy, "
        f"got batch={recorded['batch']}"
    )

    # 2. Второй run: --question → cache hit → НЕ вызывает llm_batch.
    recorded["batch"] = 0
    recorded["doc_reduce"] = 0

    result2 = summarizer.run(
        text, question="Текст.",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result2["status"] == "completed", result2

    # Главный assertion — НЕ было llm_batch вызовов (нет нового map).
    assert recorded["batch"] == 0, (
        f"--question via document-cache must NOT call llm_batch, "
        f"got {recorded['batch']} batch calls"
    )
    # Но был один llm_document_reduce (synthesis).
    assert recorded["doc_reduce"] == 1


def test_question_strategy_label(tmp_path, monkeypatch):
    """strategy в результате = ``document_cache_question`` при cache hit."""
    import application.service as summarizer
    _install_recording_llm(monkeypatch)

    text = _build_doc_with_marker(sections=4)
    p = _write_doc(tmp_path, text)

    summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    result = summarizer.run(
        text, question="контрагентство",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result["status"] == "completed"
    assert result["result"]["strategy"] == "document_cache_question"
    assert result["stats"].get("document_cache_hit") is True


def test_question_no_document_cache_falls_through(tmp_path, monkeypatch):
    """Если document-cache не complete → fallthrough на обычный pipeline.

    NB: для txt без headings стратегия = direct, и ``llm_batch`` не
    вызывается (его вызывает только map-reduce). Поэтому здесь просто
    проверяем, что результат получен и strategy != ``document_cache_question``.
    """
    import application.service as summarizer
    recorded = _install_recording_llm(monkeypatch)

    text = _build_doc_with_marker(sections=4)
    p = _write_doc(tmp_path, text)

    # Сразу --question БЕЗ предварительного --length → нет document cache.
    result = summarizer.run(
        text, question="контрагентство",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result["status"] in ("completed", "partial")
    # Главный assertion: strategy НЕ document_cache_question.
    assert result["result"]["strategy"] != "document_cache_question"
    assert result["stats"].get("document_cache_hit") is not True


def test_question_without_document_path_raises_inspect_error(
    tmp_path, monkeypatch,
):
    """``--question`` без ``document_path`` → fallthrough на inspect(),
    который raise'ит ValueError (это **существующее** поведение inspect,
    не моя регрессия). Регрессионный guard для document-cache ветки:
    она НЕ должна активироваться без document_path, чтобы не замаскировать
    эту ошибку под "completed".
    """
    import application.service as summarizer
    _install_recording_llm(monkeypatch)

    text = _build_doc_with_marker(sections=4)
    p = _write_doc(tmp_path, text)

    # С document_path=None инспектор падает — это ОЖИДАЕМОЕ существующее
    # поведение. Важно: document-cache ветка НЕ возвращает None-обёрнутый
    # фейк, который бы замаскировал эту ошибку под completed.
    with pytest.raises(ValueError, match="document_path"):
        summarizer.run(
            text, question="контрагентство",
            document_path=None, workspace_root=tmp_path,
            confirmed=True,
        )


def test_question_broken_snapshot_falls_through(tmp_path, monkeypatch):
    """Если snapshot повреждён → fallthrough без падения."""
    import application.service as summarizer
    import cache.manifest as cm
    _install_recording_llm(monkeypatch)

    text = _build_doc_with_marker(sections=4)
    p = _write_doc(tmp_path, text)

    # Создаём cache.
    summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )

    # Ломаем analysis.json.
    from document.identity import DocumentIdentity
    document_id = DocumentIdentity.from_path(p).document_id
    analysis_path = cm.document_analysis_path(document_id, tmp_path)
    analysis_path.write_text("{not valid json", encoding="utf-8")

    # Второй run не должен упасть.
    result = summarizer.run(
        text, question="контрагентство",
        document_path=str(p), workspace_root=tmp_path,
        confirmed=True,
    )
    assert result["status"] in ("completed", "partial")