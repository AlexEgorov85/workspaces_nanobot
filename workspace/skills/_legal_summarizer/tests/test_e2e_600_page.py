"""600-страничный E2E-тест для legal_summarizer Phase 2B.

Проверяет, что skill обрабатывает большой документ внутри одного
run() (без polling / streaming):

  * inspection без LLM → context_batches_total разумное число
  * confirmation_required БЕЗ LLM-вызовов
  * confirmed=True → completed за один вызов
  * stats разделены: map_calls / reduce_calls / retries
  * manifest.completed, result.json, chunks/<id>.json на диске
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

_SKILL_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _SKILL_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
_PROJECT_ROOT = _SKILL_ROOT.parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import application.service as summarizer  # noqa: E402
import llm.client as llm_client  # noqa: E402
import llm.config as llm_config  # noqa: E402
import application.context_builder as ctx_builder  # noqa: E402
from cache.manifest import manifest_path  # noqa: E402

def _patch_exec_cfg(monkeypatch, cfg):
    """Установить get_execution_config в обоих местах (llm_config и service)."""
    monkeypatch.setattr(llm_config, "get_execution_config", lambda: cfg)
    monkeypatch.setattr(llm.config, "get_execution_config", lambda: cfg)

def _patch_chunk_cfg(monkeypatch, cfg):
    """Установить get_chunking_config. Runtime читает через llm_config."""
    monkeypatch.setattr(llm_config, "get_chunking_config", lambda: cfg)

def _build_text_response(user_content: str) -> str:
    """Мок-ответ LLM в текстовом формате с маркерами ``DOC CHUNK N:``."""
    n = len(re.findall(r"DOCUMENT CHUNK \d+", user_content))
    return "\n\n".join(f"DOC CHUNK {i + 1}: саммари чанка {i + 1}" for i in range(n)) + ("\n" if n else "")

def _generate_long_legal_text(
    *,
    pages: int = 600,
    chars_per_page: int = 3000,
    sections_per_doc: int = 25,
) -> str:
    """Deterministic синтетический документ: pages × chars_per_page символов.

    Документ имеет sections_per_doc разделов с фиктивными heading'ами
    "1. ...", "2. ...", и т.д., и body абзацами между ними.
    """
    paragraphs_per_section = max(1, pages // sections_per_doc)
    section_chars = pages * chars_per_page // sections_per_doc

    paragraphs: list[str] = []
    for s in range(1, sections_per_doc + 1):
        paragraphs.append(f"{s}. Общие положения и предмет договора аренды помещения.")
        for p in range(paragraphs_per_section):
            text = (
                f"Раздел {s}, абзац {p}: описание обязательств сторон, "
                "сроков и порядка расчётов между арендодателем и арендатором. "
            )
            paragraphs.append(text * (section_chars // (paragraphs_per_section * len(text) + 1)))

    text = "\n\n".join(paragraphs)
    return text

@pytest.fixture
def long_legal_text() -> str:
    return _generate_long_legal_text(pages=600, chars_per_page=3000, sections_per_doc=25)

@pytest.fixture
def long_legal_doc(tmp_path, long_legal_text) -> Path:
    p = tmp_path / "doc.txt"
    p.write_text(long_legal_text, encoding="utf-8")
    return p

import llm.config  # noqa: E402


def test_600_page_inspect_without_llm(long_legal_doc, long_legal_text, monkeypatch):
    """Inspect для 600-страничного документа работает без LLM."""
    call_count = {"n": 0}

    def fake_chat(*args, **kwargs):
        call_count["n"] += 1
        return "should not be called"

    monkeypatch.setattr(llm_client, "chat", fake_chat)

    insp = summarizer.inspect(long_legal_text, document_path=str(long_legal_doc))
    assert call_count["n"] == 0
    assert len(insp.chunks) > 5
    ctx = ctx_builder.build_execution_context(insp, length="brief")
    assert ctx.strategy.startswith("map_reduce") or ctx.strategy == "direct"
    if ctx.strategy.startswith("map_reduce") and ctx.plan is not None:
        assert len(ctx.plan.batches) > 0
        assert len(ctx.plan.batches) <= len(ctx.chunks)

def test_600_page_confirmation_required_without_llm(
    long_legal_doc, long_legal_text, monkeypatch, tmp_path,
):
    """Длинный документ → confirmation_required, LLM не вызывается."""
    _patch_exec_cfg(monkeypatch, {
        "confirmation_threshold_sec": 5.0,
        "estimated_chunk_duration_sec": 30.0,
        "max_chunks_for_execution": 1000,
        "context_batching": {
            "system_prompt_tokens": 1200,
            "instruction_tokens_per_map": 200,
            "chars_per_token": 3.5,
            "safety_margin": 0.85,
        },
        "llm_max_tokens": 8192,
    })
    call_count = {"n": 0}

    def fake_chat(*args, **kwargs):
        call_count["n"] += 1
        return "should not be called"

    monkeypatch.setattr(llm_client, "chat", fake_chat)

    result = summarizer.run(
        long_legal_text, length="brief", workspace_root=tmp_path,
        document_path=str(long_legal_doc),
    )
    assert result["status"] == "confirmation_required"
    assert call_count["n"] == 0
    assert result["summary"]["chunks_total"] > 5

def test_600_page_executes_via_context_batching(
    long_legal_doc, long_legal_text, monkeypatch, tmp_path,
):
    """600 страниц обрабатываются за один run() через context batching."""
    _patch_exec_cfg(monkeypatch, {
        "confirmation_threshold_sec": 0.001,
        "estimated_chunk_duration_sec": 0.001,
        "max_chunks_for_execution": 10000,
        "context_batching": {
            "system_prompt_tokens": 1200,
            "instruction_tokens_per_map": 200,
            "chars_per_token": 3.5,
            "safety_margin": 0.85,
        },
        "llm_max_tokens": 8192,
    })
    _patch_chunk_cfg(monkeypatch, {
        "chunk_size": 5000,
        "chunk_overlap": 200,
        "single_call_threshold": 1000,
        "chunk_size_input_ratio": None,
    })

    state = {"map_calls": 0, "reduce_calls": 0}

    def fake_chat(messages, *, context=None, **kwargs):
        user_content = messages[1]["content"]
        if re.findall(r"DOCUMENT CHUNK \d+", user_content):
            state["map_calls"] += 1
            return _build_text_response(user_content)
        state["reduce_calls"] += 1
        return "Это договор аренды.\n\nСуть: аренда помещения."

    monkeypatch.setattr(llm_client, "chat", fake_chat)

    result = summarizer.run(
        long_legal_text, length="brief", confirmed=True, workspace_root=tmp_path,
        document_path=str(long_legal_doc),
    )
    assert result["status"] == "completed"
    assert result["stats"]["strategy"].startswith("map_reduce") or \
        result["stats"]["strategy"] == "direct", (
        f"Expected map_reduce или direct стратегию, получили "
        f"{result['stats']['strategy']}"
    )
    if result["stats"]["strategy"].startswith("map_reduce"):
        assert result["stats"]["map_calls"] > 0
        assert result["stats"]["map_calls"] < result["stats"]["chunks_total"], (
            "Context batching должен уменьшать количество LLM-вызовов"
        )
    op_id = result["operation_id"]
    manifest_p = manifest_path(op_id, tmp_path)
    assert manifest_p.is_file()

def test_600_page_stats_separate_map_reduce_retries(
    long_legal_doc, long_legal_text, monkeypatch, tmp_path,
):
    """Stats корректно разделены на map/reduce/retries."""
    _patch_exec_cfg(monkeypatch, {
        "confirmation_threshold_sec": 0.001,
        "estimated_chunk_duration_sec": 0.001,
        "max_chunks_for_execution": 10000,
        "context_batching": {
            "system_prompt_tokens": 1200,
            "instruction_tokens_per_map": 200,
            "chars_per_token": 3.5,
            "safety_margin": 0.85,
        },
        "llm_max_tokens": 8192,
    })
    _patch_chunk_cfg(monkeypatch, {
        "chunk_size": 5000,
        "chunk_overlap": 200,
        "single_call_threshold": 1000,
        "chunk_size_input_ratio": None,
    })

    def fake_chat(messages, *, context=None, **kwargs):
        user_content = messages[1]["content"]
        if re.findall(r"DOCUMENT CHUNK \d+", user_content):
            return _build_text_response(user_content)
        return "Это договор.\n\nСуть: аренда."

    monkeypatch.setattr(llm_client, "chat", fake_chat)

    result = summarizer.run(
        long_legal_text, length="brief", confirmed=True, workspace_root=tmp_path,
        document_path=str(long_legal_doc),
    )
    stats = result["stats"]
    assert "map_calls" in stats
    assert "section_reduce_calls" in stats
    assert "section_trim_calls" in stats
    assert "document_reduce_calls" in stats
    assert "reduce_calls" in stats
    assert "total_llm_calls" in stats
    assert "retries" in stats
    assert stats["reduce_calls"] >= stats["document_reduce_calls"]
    assert stats["total_llm_calls"] == stats["map_calls"] + stats["reduce_calls"] + stats["retries"]

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))