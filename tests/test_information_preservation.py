"""Information preservation tests.

Проверяет, что критические факты из исходного документа сохранены
в summary (без LLM — keyword presence test на mock-ответе).

Подход: документ содержит N «фактов» (строки вида ``FACT_001: ...``).
LLM mock возвращает summary, который содержит подмножество фактов.
Тест проверяет что:
    * Все факты попадают в исходный текст (sanity).
    * Для «честного» mock (возвращает текст с большинством фактов) —
      summary содержит ≥80% фактов.
    * Для «плохого» mock (возвращает текст без фактов) — тест на устойчивость
      detection logic (но это не regression — мы лишь фиксируем baseline).

Тест НЕ оценивает реальное качество LLM, а фиксирует контракт:
«если LLM вернул summary, мы можем проверить наличие keywords».
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO / "workspace" / "skills" / "legal_summarizer" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


def _extract_facts(text: str) -> list[str]:
    """Извлечь «факты» из текста (формат ``FACT_001: ...``)."""
    pattern = re.compile(r"FACT_(\d+):\s*(.+?)(?=\n|$)", re.MULTILINE)
    return [m.group(2).strip() for m in pattern.finditer(text)]


def _fact_presence(facts: list[str], text: str) -> tuple[list[bool], float]:
    """Для каждого факта — есть ли его содержание в ``text``."""
    presence = [bool(f and f in text) for f in facts]
    ratio = sum(presence) / len(presence) if presence else 0.0
    return presence, ratio


# ---------------------------------------------------------------------------
# Sanity: факты извлекаются корректно
# ---------------------------------------------------------------------------


def test_info_preservation_extract_facts_from_text():
    """``_extract_facts`` находит все маркеры ``FACT_NNN: ...``."""
    text = (
        "Вступление документа.\n"
        "FACT_001: Договор аренды заключается в письменной форме.\n"
        "FACT_002: Срок аренды — 12 месяцев.\n"
        "FACT_003: Арендная плата составляет 50000 рублей в месяц.\n"
        "Заключение."
    )
    facts = _extract_facts(text)
    assert len(facts) == 3
    assert "Договор аренды заключается в письменной форме." in facts
    assert "Срок аренды — 12 месяцев." in facts


def test_info_preservation_no_facts_in_text_returns_empty():
    """Текст без маркеров → пустой список."""
    text = "Это обычный текст без фактов."
    facts = _extract_facts(text)
    assert facts == []


# ---------------------------------------------------------------------------
# Fact presence в mock-summary
# ---------------------------------------------------------------------------


def test_info_preservation_high_presence_passes_threshold():
    """Summary содержит ≥80% фактов → acceptance.

    Mock-summary содержит все 5 фактов из 5 → 100% presence.
    """
    facts = [
        "Договор аренды",
        "Срок 12 месяцев",
        "Плата 50000 рублей",
        "Стороны ООО Ромашка и ИП Иванов",
        "Дата начала 1 января 2026",
    ]
    summary = (
        "Саммари договора.\n"
        "Стороны ООО Ромашка и ИП Иванов. Договор аренды заключён. "
        "Срок 12 месяцев. Плата 50000 рублей. Дата начала 1 января 2026 года."
    )
    presence, ratio = _fact_presence(facts, summary)
    assert all(presence), f"Не все факты в summary: {presence}"
    assert ratio == 1.0
    assert ratio >= 0.8


def test_info_preservation_partial_presence_below_threshold():
    """Summary содержит <80% фактов → ratio ниже порога.

    Mock-summary содержит 2 из 5 фактов → 40%.
    """
    facts = [
        "Договор аренды",
        "Срок 12 месяцев",
        "Плата 50000 рублей",
        "Стороны ООО Ромашка",
        "Дата начала",
    ]
    summary = (
        "Саммари: Договор аренды. Срок 12 месяцев."
    )
    presence, ratio = _fact_presence(facts, summary)
    assert sum(presence) == 2
    assert ratio == 0.4
    assert ratio < 0.8


def test_info_preservation_acceptance_boundary_80_percent():
    """Boundary — 4 из 5 фактов (80%) — ровно на пороге."""
    facts = [
        "Факт один",
        "Факт два",
        "Факт три",
        "Факт четыре",
        "Факт пять",
    ]
    summary = "Содержит: Факт один, Факт два, Факт три, Факт четыре"
    presence, ratio = _fact_presence(facts, summary)
    assert sum(presence) == 4
    assert ratio == 0.8
    assert ratio >= 0.8


# ---------------------------------------------------------------------------
# Integration: end-to-end с моком LLM и проверкой keywords
# ---------------------------------------------------------------------------


def test_info_preservation_e2e_mock_passes_keywords(tmp_path, monkeypatch):
    """Integration: документ с FACT_NNN → run() → summary содержит факты.

    Mock LLM «повторяет» факты в summary. Тест проверяет acceptance 80%.
    """
    import summarizer

    monkeypatch.setattr(summarizer, "get_chunking_config", lambda: {
        "chunk_size": 100000, "chunk_overlap": 0, "single_call_threshold": 100000,
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

    facts = [
        "Договор аренды заключается в письменной форме",
        "Срок аренды составляет 12 месяцев",
        "Арендная плата 50000 рублей в месяц",
        "Стороны ООО Ромашка и ИП Иванов",
        "Дата начала 1 января 2026 года",
    ]
    text_parts = ["Вступление."]
    for i, f in enumerate(facts, 1):
        text_parts.append(f"FACT_{i:03d}: {f}.")
    text_parts.append("Заключение.")
    text = "\n\n".join(text_parts)

    # Mock: возвращает summary с большинством фактов.
    honest_summary = "Саммари договора.\n" + "\n".join(facts[:4]) + "\n" + facts[4]

    def fake_chat(messages, *, context=None, **kwargs):
        return honest_summary

    monkeypatch.setattr(summarizer.llm, "chat", fake_chat)

    result = summarizer.run(
        text, length="brief", confirmed=True, workspace_root=tmp_path,
    )
    assert result["status"] == "completed"

    summary = result["result"]["summary"]
    presence, ratio = _fact_presence(facts, summary)
    # Acceptance: ≥80% фактов в summary.
    assert ratio >= 0.8, (
        f"Только {sum(presence)}/{len(presence)} фактов в summary: "
        f"{[f for f, p in zip(facts, presence) if not p]}"
    )


def test_info_preservation_e2e_partial_summary_below_threshold(
    tmp_path, monkeypatch,
):
    """Плохой LLM (только 2 из 5 фактов) → ratio ниже 80%.

    Фиксирует baseline: если LLM возвращает summary с <80% фактов,
    это ratio ниже acceptance. Тест не regression-detection, а документирует
    поведение ``_fact_presence``.
    """
    import summarizer

    monkeypatch.setattr(summarizer, "get_chunking_config", lambda: {
        "chunk_size": 100000, "chunk_overlap": 0, "single_call_threshold": 100000,
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

    facts = [
        "Договор аренды заключается в письменной форме",
        "Срок аренды составляет 12 месяцев",
        "Арендная плата 50000 рублей в месяц",
        "Стороны ООО Ромашка и ИП Иванов",
        "Дата начала 1 января 2026 года",
    ]
    text_parts = ["Вступление."]
    for i, f in enumerate(facts, 1):
        text_parts.append(f"FACT_{i:03d}: {f}.")
    text = "\n\n".join(text_parts)

    # Mock: возвращает summary только с 2 фактами.
    bad_summary = "Краткое саммари: " + facts[0] + ". " + facts[1] + "."

    def fake_chat(messages, *, context=None, **kwargs):
        return bad_summary

    monkeypatch.setattr(summarizer.llm, "chat", fake_chat)

    result = summarizer.run(
        text, length="brief", confirmed=True, workspace_root=tmp_path,
    )
    assert result["status"] == "completed"

    summary = result["result"]["summary"]
    presence, ratio = _fact_presence(facts, summary)
    # 2/5 = 40% — ниже порога 80%.
    assert ratio < 0.8


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_info_preservation_empty_facts_list():
    """Пустой список фактов → ratio=0 (без деления на 0)."""
    facts = []
    summary = "Любой summary."
    presence, ratio = _fact_presence(facts, summary)
    assert presence == []
    assert ratio == 0.0


def test_info_preservation_fact_with_substring_in_summary():
    """Substring match — факт является подстрокой summary."""
    facts = ["ключевое слово"]
    summary = "Текст содержит ключевое слово где-то в середине."
    presence, ratio = _fact_presence(facts, summary)
    assert presence == [True]
    assert ratio == 1.0


def test_info_preservation_fact_absent_from_summary():
    """Факт отсутствует → presence=False."""
    facts = ["уникальный факт X"]
    summary = "Саммари без этого факта."
    presence, ratio = _fact_presence(facts, summary)
    assert presence == [False]
    assert ratio == 0.0
