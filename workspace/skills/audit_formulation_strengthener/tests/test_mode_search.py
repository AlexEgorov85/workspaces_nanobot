"""Тесты режима ``search`` — map-reduce по чанкам ВНД."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_SKILL_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(_SKILL_SCRIPTS))


def test_search_no_vnd_paths() -> None:
    """Пустой список ВНД → ошибка ``no_vnd``."""
    from modes import search

    result, report_text = search.run(violation="текст", vnd_paths=[])
    assert result["status"] == "error"
    assert result["data"]["error_type"] == "no_vnd"
    assert report_text is None


def test_search_vnd_not_found(tmp_path: Path) -> None:
    """Несуществующий путь → ошибка ``vnd_not_found``."""
    from modes import search

    result, _ = search.run(
        violation="текст",
        vnd_paths=[str(tmp_path / "no_such_file.pdf")],
    )
    assert result["status"] == "error"
    assert result["data"]["error_type"] == "vnd_not_found"


def test_search_estimate_only(
    mock_prepare_vnd,
) -> None:
    """``--estimate-only`` → нет LLM-вызова."""
    from modes import search

    with pytest.MonkeyPatch.context() as m:
        m.setattr(search, "prepare_vnd", mock_prepare_vnd)
        result, report_text = search.run(
            violation="текст",
            vnd_paths=["vnd1.txt", "vnd2.txt"],
            estimate_only=True,
        )

    assert result["status"] == "success"
    data = result["data"]
    assert data["vnd_files"] == 2
    assert data["vnd_chunks_total"] == 3
    assert data["map_batches_planned"] == 3
    assert data["synthesis_llm_calls_planned"] == 1
    assert report_text is None


def test_search_full_pipeline_with_mocks(
    mock_prepare_vnd, mock_llm_search_map
) -> None:
    """Полный map-reduce прогон: 3 чанка, фильтрация, re-rank."""
    from modes import search

    with pytest.MonkeyPatch.context() as m:
        m.setattr(search, "prepare_vnd", mock_prepare_vnd)
        m.setattr(search, "call_llm_json", mock_llm_search_map)
        result, _ = search.run(
            violation="Срок хранения ПДн — 1 год",
            vnd_paths=["vnd1.txt", "vnd2.txt"],
        )

    assert result["status"] == "success"
    data = result["data"]

    # Всего чанков обработано — 3.
    assert data["vnd_chunks_total"] == 3
    assert data["chunks_processed"] == 3
    assert data["chunks_failed"] == 0

    # Чанк с score=0.05 ("нерелевантный") отфильтрован (< MIN=0.3).
    findings = data["vnd_findings"]
    scores = [f["relevance_score"] for f in findings]
    assert all(s >= 0.3 for s in scores), f"Scores below threshold leaked: {scores}"
    assert 0.05 not in scores

    # Top-K отсортирован по score убывание.
    assert scores == sorted(scores, reverse=True), f"Not sorted: {scores}"
    assert len(findings) <= search.TOP_K_CANDIDATES

    # Первый finding — chunk с score 0.92 (прямое_противоречие).
    assert findings[0]["relation_type"] == "прямое_противоречие"
    assert findings[0]["relevance_score"] == 0.92


def test_search_resolve_violation_from_analyze(
    mock_prepare_vnd, mock_llm_search_map
) -> None:
    """``analyze_result`` используется как нормализованная формулировка."""
    from modes import search

    analyze_result = {
        "status": "success",
        "data": {"normalized": "НОРМАЛИЗОВАННЫЙ ТЕКСТ"},
    }

    with pytest.MonkeyPatch.context() as m:
        m.setattr(search, "prepare_vnd", mock_prepare_vnd)
        m.setattr(search, "call_llm_json", mock_llm_search_map)
        result, _ = search.run(
            violation="исходный текст",
            vnd_paths=["vnd1.txt"],
            analyze_result=analyze_result,
        )

    assert result["status"] == "success"
    assert result["data"]["normalized_violation_used"] == "НОРМАЛИЗОВАННЫЙ ТЕКСТ"


def test_search_resolve_violation_from_file(
    mock_prepare_vnd, mock_llm_search_map, tmp_path: Path
) -> None:
    """``analyze_result_path`` загружается из файла."""
    from modes import search

    analyze_file = tmp_path / "analyze.json"
    analyze_file.write_text(
        '{"status": "success", "data": {"normalized": "ИЗ ФАЙЛА"}}',
        encoding="utf-8",
    )

    with pytest.MonkeyPatch.context() as m:
        m.setattr(search, "prepare_vnd", mock_prepare_vnd)
        m.setattr(search, "call_llm_json", mock_llm_search_map)
        result, _ = search.run(
            violation="исходный",
            vnd_paths=["vnd1.txt"],
            analyze_result_path=str(analyze_file),
        )

    assert result["status"] == "success"
    assert result["data"]["normalized_violation_used"] == "ИЗ ФАЙЛА"


def test_search_one_chunk_failure_does_not_crash(
    mock_prepare_vnd,
) -> None:
    """Если LLM падает на одном чанке — остальные продолжают обрабатываться."""
    from modes import search
    from llm_client import JsonParseError

    def selective_failure(system, user, operation):
        # Падаем на втором чанке (первый в списке — chunk_index=0).
        if "Раздел 6.1" in system:
            raise JsonParseError("bad json")
        return {
            "relation_type": "контекст",
            "relevance_score": 0.5,
            "why_matches": "ok",
        }

    with pytest.MonkeyPatch.context() as m:
        m.setattr(search, "prepare_vnd", mock_prepare_vnd)
        m.setattr(search, "call_llm_json", selective_failure)
        result, _ = search.run(
            violation="текст",
            vnd_paths=["vnd1.txt"],
        )

    assert result["status"] == "success"
    data = result["data"]
    assert data["chunks_failed"] >= 1
    assert data["chunks_processed"] == 3
    # Должно быть как минимум 1 success finding.
    assert len(data["vnd_findings"]) >= 1


def test_search_invalid_relation_type_normalized(
    mock_prepare_vnd,
) -> None:
    """Невалидный relation_type → ``контекст``."""
    from modes import search

    def bad_relation(system, user, operation):
        return {
            "relation_type": "что-то непонятное",
            "relevance_score": 0.4,
            "why_matches": "...",
        }

    with pytest.MonkeyPatch.context() as m:
        m.setattr(search, "prepare_vnd", mock_prepare_vnd)
        m.setattr(search, "call_llm_json", bad_relation)
        result, _ = search.run(
            violation="текст",
            vnd_paths=["vnd1.txt"],
        )

    assert result["status"] == "success"
    assert result["data"]["vnd_findings"][0]["relation_type"] == "контекст"
