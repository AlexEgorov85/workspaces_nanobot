"""Smoke-тесты навыка legal_summarizer (Phase 2B — Structure-Aware Context Batching).

Покрывает:
    * run() короткий документ → single
    * run() длинный документ → confirmation_required (без LLM)
    * run() confirmed → completed с context_batches
    * run() изоляция от agent history
    * inspect() без LLM
    * estimate() min/max
    * load_text: расширения + ошибки
    * load_structure: title/begin/end
    * output: prepare_output completed/confirmation_required/failed
    * skill_config: defaults
    * SKILL.md: контракт инструкций для LLM
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SKILL_ROOT = Path(__file__).resolve().parents[1] / "workspace" / "skills" / "legal_summarizer"
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import summarizer  # noqa: E402
from output import _sanitize_value, prepare_output  # noqa: E402


# ---------------------------------------------------------------------------
# summarizer.run() — short doc
# ---------------------------------------------------------------------------


def test_run_short_doc_returns_completed(monkeypatch, tmp_path):
    """Короткий документ → run() сразу возвращает completed без LLM-batches."""
    captured = {}

    def fake_chat(messages, *, context=None, **kwargs):
        captured["messages"] = messages
        captured["context"] = context
        return "Это договор аренды.\n\nСуть: аренда помещения."

    monkeypatch.setattr(summarizer.llm, "chat", fake_chat)

    text = "Договор аренды на 11 месяцев. Арендодатель сдаёт помещение."
    result = summarizer.run(
        text, length="brief", workspace_root=tmp_path,
    )
    assert result["status"] == "completed"
    inner = result["result"]
    assert inner["subject"]
    assert inner["chunks"] == 1
    assert inner["strategy"] == "single"
    assert result["stats"]["map_calls"] == 1
    assert result["stats"]["strategy"] == "single"
    assert captured["context"] is None


def test_run_empty_text_returns_failed(tmp_path):
    result = summarizer.run("", length="brief", workspace_root=tmp_path)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "EMPTY_DOCUMENT"


def test_run_uses_same_operation_id_when_provided(monkeypatch, tmp_path):
    def fake_chat(messages, *, context=None, **kwargs):
        return "Это договор.\n\nСуть."

    monkeypatch.setattr(summarizer.llm, "chat", fake_chat)

    text = "Договор аренды."
    result = summarizer.run(
        text,
        length="brief",
        operation_id="op_test_resume_001",
        workspace_root=tmp_path,
    )
    assert result["operation_id"] == "op_test_resume_001"


def test_invalid_length_falls_back_to_medium(monkeypatch, tmp_path):
    def fake_chat(messages, *, context=None, **kwargs):
        return "Это договор.\n\nСуть."

    monkeypatch.setattr(summarizer.llm, "chat", fake_chat)
    result = summarizer.run("Договор.", length="nonexistent", workspace_root=tmp_path)
    assert result["result"]["length"] == "medium"


def test_subject_extracted_from_first_line(monkeypatch, tmp_path):
    monkeypatch.setattr(
        summarizer.llm, "chat",
        lambda messages, *, context=None, **kwargs: "Это договор подряда.\n\nСуть.",
    )
    result = summarizer.run("Договор подряда.", length="brief", workspace_root=tmp_path)
    assert result["result"]["subject"] == "Это договор подряда."


def test_prompts_load_from_markdown(monkeypatch, tmp_path):
    """Промпты читаются из ``prompts/*.md``."""
    monkeypatch.setattr(
        summarizer.llm, "chat",
        lambda messages, *, context=None, **kwargs: "Это договор аренды.\n\nСуть.",
    )
    summarizer.run("Договор аренды.", length="brief", workspace_root=tmp_path)

    summarize_md = _SKILL_ROOT / "prompts" / "summarize_system.md"
    reduce_md = _SKILL_ROOT / "prompts" / "reduce_system.md"
    section_reduce_md = _SKILL_ROOT / "prompts" / "section_reduce_system.md"
    assert summarize_md.is_file()
    assert reduce_md.is_file()
    assert section_reduce_md.is_file()
    assert "юридическ" in summarize_md.read_text(encoding="utf-8").lower()
    assert "юридическ" in reduce_md.read_text(encoding="utf-8").lower()


# ---------------------------------------------------------------------------
# summarizer.run() — long doc: confirmation + execute
# ---------------------------------------------------------------------------


def test_run_long_doc_returns_confirmation_required(monkeypatch, tmp_path):
    """Длинный документ → confirmation_required БЕЗ LLM-вызовов."""
    call_count = {"n": 0}

    def fake_chat(messages, *, context=None, **kwargs):
        call_count["n"] += 1
        return "Это договор аренды.\n\nСуть."

    monkeypatch.setattr(summarizer.llm, "chat", fake_chat)
    monkeypatch.setattr(
        summarizer,
        "get_chunking_config",
        lambda: {
            "chunk_size": 100,
            "chunk_overlap": 0,
            "single_call_threshold": 50,
            "chunk_size_input_ratio": None,
        },
    )
    monkeypatch.setattr(
        summarizer,
        "get_execution_config",
        lambda: {
            "confirmation_threshold_sec": 5.0,
            "estimated_chunk_duration_sec": 10.0,
            "max_chunks_for_execution": 100,
            "context_batching": {
                "system_prompt_tokens": 0,
                "instruction_tokens_per_map": 0,
                "chars_per_token": 3.5,
                "safety_margin": 0.85,
            },
            "llm_max_tokens": 100,
        },
    )

    paragraph = (
        "Длинный абзац про договор подряда, права, обязанности, "
        "сроки, риски и порядок расчётов. "
    )
    text = "\n\n".join([paragraph] * 200)

    result = summarizer.run(
        text, length="brief", workspace_root=tmp_path,
    )
    assert result["status"] == "confirmation_required"
    assert call_count["n"] == 0
    summary = result["summary"]
    assert summary["chars_in"] > 0
    assert summary["chunks_total"] > 6
    assert "operation_id" in result
    assert "hint" in result


def test_run_long_doc_with_confirm_executes_full_pipeline(monkeypatch, tmp_path):
    """Длинный документ + confirmed=True → выполнение всех batches."""
    import re as _re

    state = {"n": 0}

    def fake_chat(messages, *, context=None, **kwargs):
        state["n"] += 1
        user_content = messages[1]["content"]
        if _re.findall(r"DOCUMENT CHUNK \d+", user_content):
            return _build_text_response(user_content)
        return "Это договор.\n\nСуть."

    monkeypatch.setattr(summarizer.llm, "chat", fake_chat)
    monkeypatch.setattr(
        summarizer,
        "get_chunking_config",
        lambda: {
            "chunk_size": 200,
            "chunk_overlap": 0,
            "single_call_threshold": 100,
            "chunk_size_input_ratio": None,
        },
    )
    monkeypatch.setattr(
        summarizer,
        "get_execution_config",
        lambda: {
            "confirmation_threshold_sec": 0.001,
            "estimated_chunk_duration_sec": 0.001,
            "max_chunks_for_execution": 100,
            "context_batching": {
                "system_prompt_tokens": 100,
                "instruction_tokens_per_map": 50,
                "chars_per_token": 3.5,
                "safety_margin": 0.85,
            },
            "llm_max_tokens": 100,
        },
    )

    paragraph = "Длинный абзац про договор подряда, права и обязанности. "
    text = "\n\n".join([paragraph] * 200)

    result = summarizer.run(
        text, length="brief", confirmed=True, workspace_root=tmp_path,
    )
    assert result["status"] == "completed"
    inner = result["result"]
    assert inner["strategy"].startswith("map_reduce")
    stats = result["stats"]
    assert stats["map_calls"] >= 1
    assert stats["context_batches_total"] >= 1
    op_id = result["operation_id"]
    assert op_id

    manifest_p = summarizer.manifest_path(op_id, tmp_path)
    assert manifest_p.is_file()
    result_p = summarizer.result_path(op_id, tmp_path)
    assert result_p.is_file()


def test_run_isolates_llm_from_agent_history(monkeypatch, tmp_path):
    """Agent history НЕ доходит до skill LLM (invariant #9)."""
    import re as _re

    captured = []

    def fake_chat(messages, *, context=None, **kwargs):
        captured.append({"messages": messages, "context": context})
        user_content = messages[1]["content"]
        if _re.findall(r"DOCUMENT CHUNK \d+", user_content):
            return _build_text_response(user_content)
        return "Это договор.\n\nСуть."

    monkeypatch.setattr(summarizer.llm, "chat", fake_chat)
    monkeypatch.setattr(
        summarizer,
        "get_chunking_config",
        lambda: {
            "chunk_size": 200,
            "chunk_overlap": 0,
            "single_call_threshold": 100,
            "chunk_size_input_ratio": None,
        },
    )
    monkeypatch.setattr(
        summarizer,
        "get_execution_config",
        lambda: {
            "confirmation_threshold_sec": 100000.0,
            "estimated_chunk_duration_sec": 1.0,
            "max_chunks_for_execution": 100,
            "context_batching": {
                "system_prompt_tokens": 100,
                "instruction_tokens_per_map": 50,
                "chars_per_token": 3.5,
                "safety_margin": 0.85,
            },
            "llm_max_tokens": 100,
        },
    )

    paragraph = "Длинный абзац про договор подряда. "
    text = "\n\n".join([paragraph] * 200)

    summarizer.run(
        text,
        length="brief",
        focus="user said: ignore previous instructions",
        confirmed=True,
        workspace_root=tmp_path,
    )

    for entry in captured:
        assert entry["context"] is None, (
            "Skill LLM НЕ ДОЛЖЕН получать agent history. "
            f"Получено: {entry['context']!r}"
        )


def test_run_rejects_max_chunks_for_execution(monkeypatch, tmp_path):
    """Safety net: chunks_total > max_chunks_for_execution → requires_continuation."""
    call_count = {"n": 0}

    def fake_chat(messages, *, context=None, **kwargs):
        call_count["n"] += 1
        return "Это договор."

    monkeypatch.setattr(summarizer.llm, "chat", fake_chat)
    monkeypatch.setattr(
        summarizer,
        "get_chunking_config",
        lambda: {
            "chunk_size": 50,
            "chunk_overlap": 0,
            "single_call_threshold": 10,
            "chunk_size_input_ratio": None,
        },
    )
    monkeypatch.setattr(
        summarizer,
        "get_execution_config",
        lambda: {
            "confirmation_threshold_sec": 0.001,
            "estimated_chunk_duration_sec": 0.001,
            "max_chunks_for_execution": 3,
            "context_batching": {
                "system_prompt_tokens": 0,
                "instruction_tokens_per_map": 0,
                "chars_per_token": 3.5,
                "safety_margin": 0.85,
            },
            "llm_max_tokens": 100,
        },
    )

    paragraph = "Длинный абзац про договор подряда, права и обязанности."
    text = "\n\n".join([paragraph] * 100)

    result = summarizer.run(
        text, length="brief", confirmed=True, workspace_root=tmp_path,
    )
    assert result["status"] == "requires_continuation"
    assert call_count["n"] == 0, "safety net не должен пропускать LLM-вызовы"
    assert result["summary"]["chunks_total"] > 3


# ---------------------------------------------------------------------------
# inspect() / estimate()
# ---------------------------------------------------------------------------


def test_inspect_does_not_call_llm(monkeypatch, tmp_path):
    call_count = {"n": 0}

    def fake_chat(messages, *, context=None, **kwargs):
        call_count["n"] += 1
        return "x"

    monkeypatch.setattr(summarizer.llm, "chat", fake_chat)

    text = "Договор аренды. " * 100
    insp = summarizer.inspect(text)
    assert call_count["n"] == 0
    assert insp.chars_in == len(text.strip())
    assert insp.strategy == "single"


def test_estimate_returns_min_max_seconds(tmp_path):
    from summarizer import Estimate, estimate, Inspection

    insp = Inspection(
        chars_in=1000,
        chunks=[],
        context_batches=[],
        tree=None,
        strategy="map_reduce",
        estimated_llm_calls=11,
    )
    est = estimate(insp)
    assert est.estimated_llm_calls == 11


def test_needs_confirmation_threshold(monkeypatch, tmp_path):
    from summarizer import Inspection, estimate, needs_confirmation

    monkeypatch.setattr(
        summarizer,
        "get_execution_config",
        lambda: {
            "confirmation_threshold_sec": 100.0,
            "estimated_chunk_duration_sec": 10.0,
            "max_chunks_for_execution": 50,
            "context_batching": {
                "system_prompt_tokens": 0,
                "instruction_tokens_per_map": 0,
                "chars_per_token": 3.5,
                "safety_margin": 0.85,
            },
            "llm_max_tokens": 100,
        },
    )

    from workspace.skills.legal_summarizer.scripts.structure.chunks import Chunk
    from workspace.skills.legal_summarizer.scripts.packing import ContextBatch

    cb_small = ContextBatch(
        batch_id="cb_000",
        chunks=tuple(Chunk(
            chunk_id=f"{i:03d}", index=i, text="x" * 100, char_count=100,
            token_estimate=10, page_start=1, page_end=1, section_id="s",
            section_path="1", section_heading="h", block_indices=(i,),
            block_types=("paragraph",),
        ) for i in range(5)),
        total_tokens_estimate=50,
        section_paths=("1",),
        page_range=(1, 1),
    )
    small = Inspection(
        chars_in=100,
        chunks=list(cb_small.chunks),
        context_batches=[cb_small],
        tree=None,
        strategy="map_reduce",
        estimated_llm_calls=2,
    )
    est = estimate(small)
    assert not needs_confirmation(est)

    big_batches = [
        ContextBatch(
            batch_id=f"cb_{i:03d}",
            chunks=tuple(Chunk(
                chunk_id=f"{i*5+j:03d}", index=i*5+j, text="x" * 100, char_count=100,
                token_estimate=10, page_start=1, page_end=1, section_id="s",
                section_path="1", section_heading="h", block_indices=(i*5+j,),
                block_types=("paragraph",),
            ) for j in range(5)),
            total_tokens_estimate=50,
            section_paths=("1",),
            page_range=(1, 1),
        )
        for i in range(20)
    ]
    big = Inspection(
        chars_in=1000,
        chunks=[c for b in big_batches for c in b.chunks],
        context_batches=big_batches,
        tree=None,
        strategy="map_reduce",
        estimated_llm_calls=21,
    )
    est = estimate(big)
    assert needs_confirmation(est)


# ---------------------------------------------------------------------------
# load_text / load_structure
# ---------------------------------------------------------------------------


def test_load_text_txt_success(tmp_path):
    p = tmp_path / "contract.txt"
    p.write_text("Договор аренды.\n\nАрендодатель сдаёт помещение.\n", encoding="utf-8")
    text = summarizer.load_text(p)
    assert "Договор аренды" in text


def test_load_text_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        summarizer.load_text(tmp_path / "missing.pdf")


def test_load_text_empty_file_raises(tmp_path):
    p = tmp_path / "empty.txt"
    p.write_text("   \n\n  \n", encoding="utf-8")
    with pytest.raises(ValueError, match="не содержит извлекаемого текста"):
        summarizer.load_text(p)


def test_load_text_unknown_extension_raises(tmp_path):
    p = tmp_path / "data.bin"
    p.write_bytes(b"\x00\x01\x02" * 16)
    with pytest.raises(ValueError):
        summarizer.load_text(p)


def test_load_structure_returns_title_and_text(tmp_path):
    from docx import Document

    p = tmp_path / "contract.docx"
    d = Document()
    d.core_properties.title = "Договор поставки №7"
    d.add_paragraph("1. Поставщик передаёт товар покупателю.")
    d.add_paragraph("2. Покупатель оплачивает товар в течение 5 дней.")
    d.save(str(p))

    struct = summarizer.load_structure(p)
    assert "Договор поставки №7" == struct["title"]
    assert "Поставщик" in struct["text"]


def test_summarize_injects_title_into_prompt(monkeypatch, tmp_path):
    captured = {}

    def fake_chat(messages, *, context=None, **kwargs):
        captured.setdefault("calls", []).append(messages)
        return "Это договор.\n\nСуть."

    monkeypatch.setattr(summarizer.llm, "chat", fake_chat)
    struct = {"title": "Договор аренды X", "begin": "", "end": ""}
    summarizer.run(
        "Договор аренды на 11 месяцев между сторонами.",
        length="brief",
        structure=struct,
        workspace_root=tmp_path,
    )
    user_content = captured["calls"][0][1]["content"]
    assert "НАЗВАНИЕ ДОКУМЕНТА: Договор аренды X" in user_content


def test_summarize_no_auto_stream_for_small_doc(monkeypatch, tmp_path):
    """Короткий документ -> обычный single (Phase 2B не использует auto-stream)."""
    monkeypatch.setattr(
        summarizer.llm, "chat",
        lambda *a, **kw: "Краткое саммари договора.",
    )
    text = "Договор аренды на 11 месяцев между ООО Ромашка и ИП Лебедев."
    result = summarizer.run(text, length="brief", workspace_root=tmp_path)
    assert result["status"] == "completed"
    assert "stream" not in result


def test_max_chunks_allow_small_doc(monkeypatch, tmp_path):
    """Для маленького документа max_chunks_for_execution не блокирует."""
    monkeypatch.setattr(
        summarizer.llm, "chat", lambda *a, **kw: "Это договор.\n\nСуть.",
    )
    monkeypatch.setattr(
        summarizer,
        "get_execution_config",
        lambda: {
            "confirmation_threshold_sec": 0.001,
            "estimated_chunk_duration_sec": 0.001,
            "max_chunks_for_execution": 5,
            "context_batching": {
                "system_prompt_tokens": 0,
                "instruction_tokens_per_map": 0,
                "chars_per_token": 3.5,
                "safety_margin": 0.85,
            },
            "llm_max_tokens": 100,
        },
    )
    small_text = "Договор аренды.\n\nСрок 11 месяцев, оплата помесячно."
    result = summarizer.run(small_text, length="brief", confirmed=True, workspace_root=tmp_path)
    assert result["status"] == "completed"


# ---------------------------------------------------------------------------
# output / prepare_output
# ---------------------------------------------------------------------------


def test_prepare_output_completed():
    result = {
        "status": "completed",
        "operation_id": "op_test_001",
        "result": {
            "subject": "Договор аренды.",
            "summary": "...",
            "length": "medium",
            "chars_in": 100,
            "chunks": 3,
            "context_batches": 2,
            "sections": 5,
            "strategy": "map_reduce_hierarchical",
        },
        "stats": {"actual_llm_calls": 4},
    }
    out = prepare_output(result)
    assert out["mode"] == "summarize"
    assert out["status"] == "completed"
    assert out["operation_id"] == "op_test_001"
    assert out["subject"] == "Договор аренды."
    assert out["chunks"] == 3
    assert out["context_batches"] == 2
    assert out["sections"] == 5
    assert out["strategy"] == "map_reduce_hierarchical"


def test_prepare_output_failed():
    result = {
        "status": "failed",
        "operation_id": "op_test_002",
        "error": {
            "code": "EMPTY_DOCUMENT",
            "message": "Документ не содержит текста",
        },
    }
    out = prepare_output(result)
    assert out["status"] == "failed"
    assert out["error"]["code"] == "EMPTY_DOCUMENT"


def test_prepare_output_confirmation_required():
    result = {
        "status": "confirmation_required",
        "operation_id": "op_test_003",
        "summary": {
            "chars_in": 1000,
            "chunks_total": 20,
            "context_batches_total": 10,
            "estimated_llm_calls": 11,
        },
        "estimate": {
            "min_seconds": 320,
            "max_seconds": 480,
            "confirmation_threshold_sec": 120,
        },
        "hint": "Передайте --confirm.",
    }
    out = prepare_output(result)
    assert out["status"] == "confirmation_required"
    assert out["summary"]["chunks_total"] == 20
    assert out["summary"]["context_batches_total"] == 10
    assert out["estimate"]["max_seconds"] == 480


def test_sanitize_handles_datetime():
    from datetime import datetime
    out = _sanitize_value({"d": datetime(2024, 1, 15, 10, 30)})
    assert out["d"] == "2024-01-15T10:30:00"


# ---------------------------------------------------------------------------
# skill_config
# ---------------------------------------------------------------------------


def test_skill_config_chunking_defaults_match_project_json():
    import skill_config

    cfg = skill_config.get_chunking_config()
    assert cfg["chunk_size"] == 100000
    assert cfg["chunk_overlap"] == 2000
    assert cfg["single_call_threshold"] == 20000
    assert cfg["chunk_size_input_ratio"] == 0.5


def test_skill_config_cli_matches_project_json():
    import skill_config

    cli = skill_config.get_cli_config()
    assert cli["max_retries"] == 3
    assert cli["timeout_sec"] == 120
    assert skill_config.get_default_length() == "medium"


# ---------------------------------------------------------------------------
# SKILL.md contract
# ---------------------------------------------------------------------------


class TestSkillMarkdownContract:
    @pytest.fixture(scope="class")
    def skill_text(self) -> str:
        path = _SKILL_ROOT / "SKILL.md"
        return path.read_text(encoding="utf-8")

    def test_cli_invocation_in_first_lines(self, skill_text: str):
        head = "\n".join(skill_text.splitlines()[:30])
        assert "cli.py" in head
        assert "--file" in head

    def test_summarize_is_not_a_summary(self, skill_text: str):
        lower = skill_text.lower()
        assert "summarize" in lower
        assert "не саммари" in lower or "не делает саммари" in lower or \
               "не делает llm" in lower

    def test_mentions_confirm_protocol(self, skill_text: str):
        assert "confirmation_required" in skill_text
        assert "--confirm" in skill_text

    def test_mentions_focus_argument(self, skill_text: str):
        assert "--focus" in skill_text

    def test_mentions_safety_net(self, skill_text: str):
        assert "max_chunks_for_execution" in skill_text or "requires_continuation" in skill_text

    def test_forbidden_summarize_direct_call(self, skill_text: str):
        assert (
            "office_files.extract_metadata" in skill_text
            or "office_files.summarize" in skill_text
        )
        assert "❌" in skill_text

    def test_description_mentions_cli(self, skill_text: str):
        assert skill_text.startswith("---")
        end = skill_text.find("\n---\n", 4)
        assert end > 0
        front = skill_text[4:end]
        assert "cli.py" in front
        assert "--file" in front

    def test_workspace_path_section_present(self, skill_text: str):
        assert "Абсолютный путь" in skill_text or "абсолютный путь" in skill_text
        assert "data_store/cache/sessions" in skill_text


# ---------------------------------------------------------------------------
# Retry + continue-with-skip + think-strip (Phase 2B hardening)
# ---------------------------------------------------------------------------


def test_strip_think_blocks_removes_cot():
    """``<think>...</think>`` от моделей с CoT вырезаются из текста."""
    text = "<think>\nЭто рассуждение, которое не нужно.\n</think>\nЭто саммари."
    cleaned = summarizer._strip_think_blocks(text)
    assert "<think>" not in cleaned
    assert "рассуждение" not in cleaned
    assert cleaned == "Это саммари."


def test_strip_think_blocks_no_think_returns_unchanged():
    text = "Обычное саммари без CoT."
    assert summarizer._strip_think_blocks(text) == text

def test_strip_think_blocks_multiple():
    text = "<think>A</think>Полезно.<think>B</think>Конец."
    assert summarizer._strip_think_blocks(text) == "Полезно.Конец."


def test_strip_think_blocks_unclosed_drops_to_blank_line():
    """Незакрытый ``<think>`` (DeepSeek/Qwen забывают ``</think>``):
    отрезается до первого абзацного разрыва, реальный ответ сохраняется.
    """
    text = "<think>\nвнутреннее рассуждение модели\n\nЭто итоговый ответ."
    cleaned = summarizer._strip_think_blocks(text)
    assert "<think>" not in cleaned
    assert "рассуждение" not in cleaned
    assert cleaned == "Это итоговый ответ."


def test_strip_think_blocks_unclosed_no_blank_drops_all():
    """Незакрытый ``<think>`` без пустой строки (весь текст — рассуждение):
    отрезается до конца (пусто лучше, чем сырой ``<think>`` в result.json)."""
    text = "<think>\nтолько рассуждение без ответа"
    cleaned = summarizer._strip_think_blocks(text)
    assert cleaned == ""


def test_strip_think_blocks_mixed_closed_and_unclosed():
    """Смешанный случай: закрытый + незакрытый блоки в одном тексте."""
    text = "<think>закрытое рассуждение</think>вступление<think>открытое\n\nответ"
    cleaned = summarizer._strip_think_blocks(text)
    assert "рассуждение" not in cleaned
    assert "<think>" not in cleaned
    assert cleaned == "вступлениеответ"


def test_prepare_output_partial_exposes_failed_batches():
    """``status=partial`` пробрасывает ``partial``/``failed_batches``/``hint``."""
    result = {
        "status": "partial",
        "operation_id": "op_x",
        "result": {
            "subject": "Субъект",
            "summary": "Текст.",
            "length": "medium",
            "chars_in": 1000,
            "chunks": 5,
            "context_batches": 3,
            "sections": 0,
            "strategy": "map_reduce_flat",
            "partial": True,
        },
        "stats": {
            "chars_in": 1000,
            "chunks_total": 5,
            "context_batches_total": 3,
            "map_calls": 2,
            "failed_batches": ["cb_000"],
            "partial": True,
        },
    }
    out = prepare_output(result)
    assert out["status"] == "partial"
    assert out["partial"] is True
    assert out["failed_batches"] == ["cb_000"]
    assert "hint" in out
    assert "cb_000" in out["hint"]
    assert "Перезапустите" in out["hint"]


def _one_chunk_per_batch_pack(chunks, budget):
    """Тестовый ``pack_chunks``: каждый чанк → отдельный батч (cb_NNN).

    В тест-окружении ``context_window_tokens`` дефолт 65536, и реальный
    ``pack_chunks`` кладёт ВСЕ чанки в один батч. Для тестов retry /
    continue-with-skip нужны ≥2 батча — мок даёт по батчу на чанк.
    """
    from workspace.skills.legal_summarizer.scripts.packing import (
        ContextBatch,
        _BATCH_OVERHEAD_TOKENS,
    )
    return [
        ContextBatch(
            batch_id=f"cb_{i:03d}",
            chunks=(c,),
            total_tokens_estimate=c.token_estimate + _BATCH_OVERHEAD_TOKENS,
            section_paths=(c.section_path,),
            page_range=(c.page_start, c.page_end),
        )
        for i, c in enumerate(chunks)
    ]


import re as _re_map

def _build_text_response(user_content: str) -> str:
    """Мок-ответ LLM в текстовом формате с маркерами ``DOC CHUNK N:``.

    Считает DOCUMENT CHUNK N в промпте, генерит ``DOC CHUNK 1: саммари 1``,
    ``DOC CHUNK 2: саммари 2`` и т.д. Парсер regex'ом достаёт пары по
    позиции → chunk_id (см. ``prompts.parse_batch_response``).
    """
    n = len(_re_map.findall(r"DOCUMENT CHUNK \d+", user_content))
    return "\n\n".join(f"DOC CHUNK {i + 1}: саммари чанка {i + 1}" for i in range(n)) + ("\n" if n else "")


def test_quick_estimate_txt_estimates_without_full_load(monkeypatch, tmp_path):
    """``quick_estimate`` для txt даёт оценку за секунды без полной
    экстракции (полная экстракция больших PDF = минуты; инцидент 2026-08-28)."""
    import summarizer as _summ

    # Низкий порог чтобы триггернуть needs_confirmation на умеренном txt
    monkeypatch.setattr(_summ, "get_execution_config", lambda: {
        "confirmation_threshold_sec": 1.0,
        "estimated_chunk_duration_sec": 5.0,
        "max_chunks_for_execution": 100,
        "context_batching": {
            "system_prompt_tokens": 0, "instruction_tokens_per_map": 0,
            "chars_per_token": 3.5, "safety_margin": 0.85,
        },
        "llm_max_tokens": 100,
    })
    monkeypatch.setattr(_summ, "get_chunking_config", lambda: {
        "chunk_size": 1000, "chunk_overlap": 0, "single_call_threshold": 100,
        "chunk_size_input_ratio": None,
    })

    # txt ~30k символов → с низким порогом нужен confirm
    p = tmp_path / "big.txt"
    p.write_text("Длинный договор. " * 3000, encoding="utf-8")
    qe = _summ.quick_estimate(p)
    assert qe["chars_in"] > 10000
    est = qe["estimate"]
    assert est.chunks_count > 1
    assert est.estimated_duration_max_sec > est.confirmation_threshold_sec
    # Не утечка LLM-call counts в оценку
    assert not hasattr(est, "estimated_llm_calls") or est.estimated_llm_calls >= 1


def test_running_marker_emitted_before_long_run(monkeypatch, tmp_path, capsys):
    """cli.py при старте длинного прогона печатает в stdout маркер
    ``status=running`` с ``poll_interval_hint_sec`` — чтобы агент не
    опрашивал каждые 30 сек вслепую (~14 LLM-вызовов) а ждал по
    подсказанному интервалу (~3-4 вызова). Проверяем что маркер
    появляется в stdout ДО запуска run()."""
    import io as _io
    import cli as _cli

    import summarizer as _summ

    monkeypatch.setattr(_summ, "get_chunking_config", lambda: {
        "chunk_size": 100000, "chunk_overlap": 0, "single_call_threshold": 100,
        "chunk_size_input_ratio": None,
    })
    monkeypatch.setattr(_summ, "get_execution_config", lambda: {
        "confirmation_threshold_sec": 0.001,
        "estimated_chunk_duration_sec": 20,
        "max_chunks_for_execution": 100,
        "context_batching": {"system_prompt_tokens": 0, "instruction_tokens_per_map": 0,
                              "chars_per_token": 3.5, "safety_margin": 0.85},
        "llm_max_tokens": 100,
    })

    # Большой текст → оценка > threshold → пройдём confirmation gate
    text = "Длинный договор. " * 15000  # ~1.4M chars

    # Перехватываем запуск run() чтобы маркер точно вышел ДО него
    run_called = {"n": 0}
    real_run = _summ.run
    def fake_run(t, **kwargs):
        run_called["n"] += 1
        # Проверяем что маркер УЖЕ напечатан (capsysbuf содержит его)
        captured = capsys.readouterr()
        assert '"status": "running"' in captured.out, (
            f"running marker not in stdout before run(); got: {captured.out[:300]}"
        )
        # Парсим маркер и проверяем поля
        import json as _json
        start = captured.out.find("{")
        end = captured.out.find("\n}", start) + 2
        if end < 2:
            depth = 0
            end = start
            for i, ch in enumerate(captured.out[start:], start):
                if ch == "{": depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
        marker = _json.loads(captured.out[start:end])
        assert marker["status"] == "running"
        assert marker["mode"] == "summarize"
        assert marker["estimated_total_sec"] > 0
        assert 250 <= marker["poll_interval_hint_sec"] <= 300
        assert "write_stdin" in marker["hint"]
        return real_run(t, **kwargs)

    monkeypatch.setattr(_summ, "run", fake_run)

    # Подменяем chat чтобы run() не делал реальных LLM-вызовов
    monkeypatch.setattr(_summ.llm, "chat",
                        lambda messages, **kw: "ok")
    monkeypatch.setattr(_summ, "inspect", lambda text, **kw: type("I", (), {
        "chunks": [], "context_batches": [], "tree": None,
        "strategy": "single", "estimated_llm_calls": 1,
        "chars_in": len(text),
    })())

    # Запуск через argv
    monkeypatch.setattr("sys.argv", ["cli.py", "--file", str(tmp_path/"x.txt"),
                                     "--confirm"])
    (tmp_path/"x.txt").write_text(text, encoding="utf-8")
    _cli.main()

    assert run_called["n"] == 1


def test_confirmation_required_payload_hides_llm_call_count():
    """``confirmation_required`` НЕ должен содержать ``estimated_llm_calls``
    и НЕ должен упоминать «вызовов LLM» в hint — пользователю важно время
    (инцидент 2026-08-28: агент зеркалил «20 вызовов LLM» в ответ)."""
    result = {
        "status": "confirmation_required",
        "operation_id": "op_x",
        "summary": {"chars_in": 1000, "chunks_total": 20, "context_batches_total": 10},
        "estimate": {
            "chars_in": 1000,
            "chunks_total": 20,
            "context_batches_total": 10,
            "estimated_duration_min_sec": 300.0,
            "estimated_duration_max_sec": 480.0,
            "confirmation_threshold_sec": 120,
        },
        "hint": "Документ требует примерно 300–480 сек. Перезапустите с --confirm.",
    }
    out = prepare_output(result)
    assert out["status"] == "confirmation_required"
    assert "estimated_llm_calls" not in out["summary"]
    assert "estimated_llm_calls" not in out["estimate"]
    assert "вызовов" not in out["hint"]


def test_prepare_output_strips_llm_call_counts_from_stats():
    """``completed``/``partial`` НЕ отдают агенту счётчики LLM-вызовов
    (map_calls/total_llm_calls/...) — только время/размер/стратегию."""
    result = {
        "status": "completed",
        "operation_id": "op_x",
        "result": {
            "subject": "S", "summary": "T", "length": "medium",
            "chars_in": 1000, "chunks": 5, "context_batches": 2,
            "sections": 0, "strategy": "map_reduce_flat",
        },
        "stats": {
            "chars_in": 1000, "chunks_total": 5, "context_batches_total": 2,
            "duration_sec": 120.5, "strategy": "map_reduce_flat",
            "map_calls": 10, "reduce_calls": 3, "total_llm_calls": 13,
            "retries": 1, "sections_total": 0, "meaningful_sections": 0,
        },
    }
    out = prepare_output(result)
    stats = out["stats"]
    assert "duration_sec" in stats
    assert "strategy" in stats
    assert "chars_in" in stats
    # LLM-call counts скрыты
    assert "map_calls" not in stats
    assert "reduce_calls" not in stats
    assert "total_llm_calls" not in stats
    assert "section_reduce_calls" not in stats
    assert "section_trim_calls" not in stats
    assert "document_reduce_calls" not in stats
    # retries оставлен (ретраи — надёжность, не «объём работы»)
    assert "retries" in stats


def test_run_reduce_output_with_think_blocks_is_cleaned(monkeypatch, tmp_path):
    """Reduce LLM вернул ``<think>...`` — subject/summary чистятся до записи."""
    def fake_chat(messages, *, context=None, **kwargs):
        return "<think>\nПлан: написать summary.\n</think>\nЭто договор аренды.\n\nСуть: аренда."

    monkeypatch.setattr(summarizer.llm, "chat", fake_chat)
    text = "Договор аренды на 11 месяцев. Арендодатель сдаёт помещение."
    result = summarizer.run(text, length="brief", workspace_root=tmp_path)
    assert result["status"] == "completed"
    assert "<think>" not in result["result"]["subject"]
    assert "<think>" not in result["result"]["summary"]
    assert "План" not in result["result"]["summary"]
    assert "Это договор" in result["result"]["summary"]


def test_batch_parse_error_retries_then_succeeds(monkeypatch, tmp_path):
    """LLM-JSON флакает на первых попытках → retry → батч доезжает → completed.

    Без retry-цикла первый же невалидный ответ привёл бы к
    ``LLM_PARSE_ERROR`` и ``status=failed``. Здесь chat возвращает мусор
    для первых двух map-вызовов (3-я попытка успешна) — run() всё равно
    завершается ``completed``.
    """
    import re as _re
    state = {"n": 0}

    def fake_chat(messages, *, context=None, **kwargs):
        state["n"] += 1
        user_content = messages[1]["content"]
        if _re.findall(r"DOCUMENT CHUNK \d+", user_content):
            if state["n"] <= 2:
                return "DOC CHUNK 1: invalid attempt"
            return _build_text_response(user_content)
        return "Это договор.\n\nСуть: подряд."

    monkeypatch.setattr(summarizer.llm, "chat", fake_chat)
    # 1 чанк на батч — чтобы retry-тест видел ≥2 батча (иначе в тест-окружении
    # pack_chunks кладёт всё в один cb_000).
    monkeypatch.setattr(summarizer, "pack_chunks", _one_chunk_per_batch_pack)
    monkeypatch.setattr(summarizer, "get_chunking_config", lambda: {
        "chunk_size": 200, "chunk_overlap": 0, "single_call_threshold": 100,
        "chunk_size_input_ratio": None,
    })
    monkeypatch.setattr(summarizer, "get_execution_config", lambda: {
        "confirmation_threshold_sec": 0.001, "estimated_chunk_duration_sec": 0.001,
        "max_chunks_for_execution": 100,
        "context_batching": {
            "system_prompt_tokens": 100, "instruction_tokens_per_map": 50,
            "chars_per_token": 3.5, "safety_margin": 0.85,
        },
        "llm_max_tokens": 100,
    })

    paragraph = "Длинный абзац про договор подряда, права и обязанности. "
    text = "\n\n".join([paragraph] * 200)
    result = summarizer.run(
        text, length="brief", confirmed=True, workspace_root=tmp_path,
    )
    assert result["status"] == "completed"
    assert result["stats"]["map_calls"] >= 2


def test_map_phase_runs_batches_concurrently(monkeypatch, tmp_path):
    """Параллельная map-фаза: при concurrency=3 и 10 батчах peak in-flight
    должен быть ≤3, а не 1 (как было при sequential). Это проверяет что
    asyncio.gather + Semaphore реально работают."""
    import re as _re
    import threading
    import time as _time

    state = {"in_flight": 0, "peak": 0, "lock": threading.Lock()}

    def fake_chat(messages, *, context=None, **kwargs):
        with state["lock"]:
            state["in_flight"] += 1
            if state["in_flight"] > state["peak"]:
                state["peak"] = state["in_flight"]
        try:
            # Имитируем I/O latency чтобы event loop успевал запустить
            # следующие task'и до завершения текущего (без sleep to_thread
            # завершается мгновенно и Semaphore не успевает разойтись).
            _time.sleep(0.05)
            user_content = messages[1]["content"]
            if _re.findall(r"DOCUMENT CHUNK \d+", user_content):
                return _build_text_response(user_content)
            return "Это договор.\n\nСуть: подряд."
        finally:
            with state["lock"]:
                state["in_flight"] -= 1

    monkeypatch.setattr(summarizer.llm, "chat", fake_chat)
    monkeypatch.setattr(summarizer, "pack_chunks", _one_chunk_per_batch_pack)
    monkeypatch.setattr(summarizer, "get_chunking_config", lambda: {
        "chunk_size": 200, "chunk_overlap": 0, "single_call_threshold": 100,
        "chunk_size_input_ratio": None,
    })
    monkeypatch.setattr(summarizer, "get_execution_config", lambda: {
        "confirmation_threshold_sec": 0.001, "estimated_chunk_duration_sec": 0.001,
        "max_chunks_for_execution": 100,
        "max_concurrent_batches": 3,  # лимит
        "context_batching": {
            "system_prompt_tokens": 100, "instruction_tokens_per_map": 50,
            "chars_per_token": 3.5, "safety_margin": 0.85,
        },
        "llm_max_tokens": 100,
    })

    paragraph = "Длинный абзац про договор подряда, права и обязанности. "
    text = "\n\n".join([paragraph] * 60)  # ~30 батчей
    result = summarizer.run(
        text, length="brief", confirmed=True, workspace_root=tmp_path,
    )
    assert result["status"] == "completed"
    assert state["peak"] >= 2, (
        f"peak in-flight должен быть ≥2 при concurrency=3 и "
        f"{result['stats']['map_calls']} батчах; получили {state['peak']}"
    )
    assert state["peak"] <= 3, (
        f"peak in-flight превысил max_concurrent_batches=3: {state['peak']}"
    )


def test_batch_parse_error_exhausts_returns_partial(monkeypatch, tmp_path):
    """Первый батч исчерпывает retry (3 parse-error) → помечается failed,
    остальные батчи успевают → ``status=partial`` с ``failed_batches``."""
    import re as _re
    state = {"n": 0}

    def fake_chat(messages, *, context=None, **kwargs):
        state["n"] += 1
        user_content = messages[1]["content"]
        if _re.findall(r"DOCUMENT CHUNK \d+", user_content):
            # Первые 3 map-вызова (3 retry первого батча) — невалидный
            # текст (нет маркеров DOC CHUNK N:). С 4-го вызова — валидный
            # (второй и последующие батчи).
            if state["n"] <= 3:
                return "Это просто текст без маркеров"
            return _build_text_response(user_content)
        return "Это договор.\n\nСуть: подряд."

    monkeypatch.setattr(summarizer.llm, "chat", fake_chat)
    # 1 чанк на батч — чтобы partial-тест видел ≥2 батча (иначе в тест-окружении
    # pack_chunks кладёт всё в один cb_000).
    monkeypatch.setattr(summarizer, "pack_chunks", _one_chunk_per_batch_pack)
    monkeypatch.setattr(summarizer, "get_chunking_config", lambda: {
        "chunk_size": 200, "chunk_overlap": 0, "single_call_threshold": 100,
        "chunk_size_input_ratio": None,
    })
    monkeypatch.setattr(summarizer, "get_execution_config", lambda: {
        "confirmation_threshold_sec": 0.001, "estimated_chunk_duration_sec": 0.001,
        "max_chunks_for_execution": 100,
        # concurrency=1 → sequential map (иначе при concurrency=4 первые 4
        # батча стартуют одновременно и 3 из них получают invalid JSON).
        "max_concurrent_batches": 1,
        "context_batching": {
            "system_prompt_tokens": 100, "instruction_tokens_per_map": 50,
            "chars_per_token": 3.5, "safety_margin": 0.85,
        },
        "llm_max_tokens": 100,
    })

    paragraph = "Длинный абзац про договор подряда, права и обязанности. "
    text = "\n\n".join([paragraph] * 200)
    result = summarizer.run(
        text, length="brief", confirmed=True, workspace_root=tmp_path,
    )
    assert result["status"] == "partial"
    assert result["result"]["partial"] is True
    failed = result["stats"]["failed_batches"]
    assert len(failed) == 1
    assert failed[0].startswith("cb_")
    assert result["stats"]["partial"] is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))