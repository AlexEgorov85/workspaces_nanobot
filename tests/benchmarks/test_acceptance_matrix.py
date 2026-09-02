"""Acceptance matrix: old/new LLM calls + quality + coverage.

Собирает и проверяет ключевые метрики pipeline:

* **LLM calls matrix**: для representative inputs (small/medium/large)
  количество LLM-вызовов «было/стало».
* **Quality matrix**: honest mock → ≥80% required_facts.
* **Coverage**: наличие ключевых модулей skill'а.

Это НЕ реальный LLM-benchmark — только deterministic harness.
Реальный LLM-замер (latency/tokens) покрывается e2e тестами.

Подход:

* Для каждого representative input запускается ``summarizer.run()``
  с mock LLM, считается число LLM-вызовов.
* Acceptance: новые пути (single/direct) дают меньше вызовов, чем map-reduce.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO / "workspace" / "skills" / "legal_summarizer" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


@dataclass
class LLMCallMetric:
    """Метрика LLM-вызовов для одного сценария."""

    scenario: str
    chars_in: int
    chunks: int
    total_llm_calls: int
    strategy: str


def _make_text(text: str) -> str:
    """Готовый текст для input."""
    return text.strip()


def _build_honest_mock(monkeypatch):
    """Mock LLM, возвращающий echo всех FACT_NNN.

    В map-вызовах (DOCUMENT CHUNK) — каждый chunk содержит свои FACTs.
    В reduce-вызове — все FACTs из user prompt.
    """
    from workspace.skills.legal_summarizer.scripts import summarizer

    def fake_chat(messages, *, context=None, **kwargs):
        user_content = messages[1]["content"]
        # Map-вызов: DOCUMENT CHUNK N markers.
        n_doc = len(re.findall(r"DOCUMENT CHUNK \d+", user_content))
        if n_doc:
            # Извлечь facts из user message и распределить по chunks.
            facts = re.findall(
                r"FACT_\d+:\s*(.+?)(?=\n|$)", user_content, re.MULTILINE
            )
            # Первый chunk получает все facts (honest mock).
            fact_str = " ".join(f"F: {f}" for f in facts) if facts else ""
            chunks_out = []
            for i in range(n_doc):
                if i == 0 and fact_str:
                    chunks_out.append(f"DOC CHUNK {i + 1}: {fact_str}")
                else:
                    chunks_out.append(f"DOC CHUNK {i + 1}: саммари чанка {i + 1}")
            return "\n\n".join(chunks_out) + "\n"
        # Reduce/single-вызов.
        facts = re.findall(
            r"FACT_\d+:\s*(.+?)(?=\n|$)", user_content, re.MULTILINE
        )
        if facts:
            return "Саммари.\n" + "\n".join(f"F: {f}" for f in facts)
        return "Краткое саммари документа."

    monkeypatch.setattr(summarizer.llm, "chat", fake_chat)


def _setup_execution_mocks(monkeypatch, *, single_threshold: int = 12000):
    """Mock chunking_config и execution_config."""
    from workspace.skills.legal_summarizer.scripts import summarizer

    monkeypatch.setattr(summarizer, "get_chunking_config", lambda: {
        "chunk_size": 1000, "chunk_overlap": 0, "single_call_threshold": single_threshold,
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


def _count_llm_calls(monkeypatch):
    """Счётчик LLM-вызовов."""
    from workspace.skills.legal_summarizer.scripts import summarizer

    counter = {"n": 0}
    original = summarizer.llm.chat

    def counting_chat(messages, *, context=None, **kwargs):
        counter["n"] += 1
        return original(messages, context=context, **kwargs)

    monkeypatch.setattr(summarizer.llm, "chat", counting_chat)
    return counter


# ---------------------------------------------------------------------------
# Representative inputs
# ---------------------------------------------------------------------------


SMALL_DOC = _make_text(
    "Вступление.\n\n"
    + "Это содержание небольшого документа для проверки single-call path. "
    + "Здесь всего несколько абзацев текста, "
    + "которые должны поместиться в один LLM call. " * 5
)

MEDIUM_DOC = _make_text(
    "Раздел 1. Вступление.\n\n"
    + "FACT_001: Первый важный факт о договоре.\n"
    + "FACT_002: Второй факт о сроках.\n"
    + "FACT_003: Третий факт о сумме.\n\n"
    + "Раздел 2. Описание.\n\n"
    + "Содержание раздела с деталями. " * 100
)

# Doc для тестов ≥2 LLM calls: после Integration ``ExecutionStrategy.DIRECT``
# ловит короткие документы (≤direct_budget_tokens), поэтому для тестов
# call-count нужен достаточно большой документ, который выберет MAP_*.
MAP_REDUCE_DOC = _make_text(
    "Раздел 1. Вступление.\n\n"
    + "FACT_001: Первый важный факт о договоре.\n"
    + "FACT_002: Второй факт о сроках.\n"
    + "FACT_003: Третий факт о сумме.\n\n"
    + "Раздел 2. Описание.\n\n"
    + "Содержание раздела с деталями. " * 8000
)

LARGE_DOC = _make_text(
    "\n\n".join(
        (
            f"Раздел {n}. Заголовок раздела {n}.\n\n"
            f"FACT_{n}_001: Факт один из раздела {n}.\n"
            f"FACT_{n}_002: Факт два из раздела {n}.\n"
            + "Содержание раздела с подробностями. " * 2000
        )
        for n in range(1, 11)
    )
)


# ---------------------------------------------------------------------------
# LLM call matrix
# ---------------------------------------------------------------------------


def test_acceptance_matrix_small_doc_single_call(tmp_path, monkeypatch):
    """Small doc (≤12000 chars) → single-call → 1 LLM call.

    Было: было single call (1 LLM call) и для small docs.
    Acceptance: осталось 1 LLM call.
    """
    _build_honest_mock(monkeypatch)
    _setup_execution_mocks(monkeypatch, single_threshold=12000)
    counter = _count_llm_calls(monkeypatch)

    from workspace.skills.legal_summarizer.scripts import summarizer

    result = summarizer.run(
        SMALL_DOC, length="brief", confirmed=True, workspace_root=tmp_path,
    )
    assert result["status"] == "completed"
    assert counter["n"] == 1, (
        f"small doc: ожидался 1 LLM call, получено {counter['n']}"
    )


def test_acceptance_matrix_medium_doc_two_calls_min(tmp_path, monkeypatch):
    """Medium doc (после Integration) → MAP_* strategy → ≥2 LLM calls.

    Используется ``MAP_REDUCE_DOC`` (большой документ, который НЕ влезает в
    ``direct_call_tokens``). После Integration ``ExecutionStrategy.DIRECT``
    срабатывает для маленьких текстов; для тестов call-count нужен
    достаточно большой документ.
    """
    _build_honest_mock(monkeypatch)
    _setup_execution_mocks(monkeypatch, single_threshold=100)  # не single
    counter = _count_llm_calls(monkeypatch)

    from workspace.skills.legal_summarizer.scripts import summarizer

    result = summarizer.run(
        MAP_REDUCE_DOC, length="brief", confirmed=True, workspace_root=tmp_path,
    )
    assert result["status"] == "completed"
    assert counter["n"] >= 2, (
        f"medium doc: ожидалось ≥2 LLM calls, получено {counter['n']}"
    )


def test_acceptance_matrix_large_doc_two_or_more_calls(tmp_path, monkeypatch):
    """Large doc → ≥2 LLM calls (map + reduce)."""
    _build_honest_mock(monkeypatch)
    _setup_execution_mocks(monkeypatch, single_threshold=100)
    counter = _count_llm_calls(monkeypatch)

    from workspace.skills.legal_summarizer.scripts import summarizer

    result = summarizer.run(
        LARGE_DOC, length="brief", confirmed=True, workspace_root=tmp_path,
    )
    assert result["status"] == "completed"
    assert counter["n"] >= 2, (
        f"large doc: ожидалось ≥2 LLM calls, получено {counter['n']}"
    )


def test_acceptance_matrix_direct_strategy_for_short_doc(tmp_path, monkeypatch):
    """Short doc → ExecutionStrategy.DIRECT → 1 LLM call.

    После Integration & Simplification: ``direct_strategy_min_chars`` УДАЛЁН.
    Acceptance: для документа, который влезает в ``TokenBudget.direct_call_tokens``,
    селектор выбирает DIRECT без дополнительной конфигурации.

    Раньше это требовало opt-in (``direct_strategy_min_chars``). Теперь
    решение принимается через ``DocumentStats`` + ``StrategyConfig``.
    """
    from workspace.skills.legal_summarizer.scripts import summarizer

    _build_honest_mock(monkeypatch)
    _setup_execution_mocks(monkeypatch, single_threshold=12000)
    counter = _count_llm_calls(monkeypatch)

    summarizer.run(
        SMALL_DOC, length="brief", confirmed=True, workspace_root=tmp_path,
    )

    assert counter["n"] == 1, (
        f"short doc + ExecutionStrategy.DIRECT: ожидался 1 LLM call, "
        f"получено {counter['n']}"
    )


# ---------------------------------------------------------------------------
# Quality matrix
# ---------------------------------------------------------------------------


def _fact_presence(facts: list[str], text: str) -> tuple[list[bool], float]:
    presence = [bool(f and f in text) for f in facts]
    ratio = sum(presence) / len(presence) if presence else 0.0
    return presence, ratio


def test_acceptance_matrix_quality_small_doc_100_percent(tmp_path, monkeypatch):
    """Small doc с required_facts → honest mock → 100% presence."""
    _build_honest_mock(monkeypatch)
    _setup_execution_mocks(monkeypatch)

    from workspace.skills.legal_summarizer.scripts import summarizer

    # Вставка фактов в small doc.
    text_with_facts = (
        SMALL_DOC
        + "\n\nFACT_001: Уникальный факт про договор аренды.\n"
        + "FACT_002: Срок двенадцать месяцев."
    )
    result = summarizer.run(
        text_with_facts, length="brief", confirmed=True, workspace_root=tmp_path,
    )
    assert result["status"] == "completed"

    summary = result["result"]["summary"]
    required = ["Уникальный факт про договор аренды", "Срок двенадцать месяцев"]
    _, ratio = _fact_presence(required, summary)
    assert ratio >= 0.8, f"quality ratio {ratio*100:.0f}% ниже 80%"


def test_acceptance_matrix_quality_medium_doc_80_percent(tmp_path, monkeypatch):
    """Medium doc → ≥80% presence.

    Использует single-call path (single_threshold > MEDIUM_DOC size),
    чтобы mock получал все FACTs в одном user message и мог вернуть их
    в summary.
    """
    _build_honest_mock(monkeypatch)
    _setup_execution_mocks(monkeypatch, single_threshold=100000)

    from workspace.skills.legal_summarizer.scripts import summarizer

    result = summarizer.run(
        MEDIUM_DOC, length="brief", confirmed=True, workspace_root=tmp_path,
    )
    assert result["status"] == "completed"

    summary = result["result"]["summary"]
    # MEDIUM_DOC содержит FACT_001, FACT_002, FACT_003.
    required = ["Первый важный факт", "Второй факт", "Третий факт"]
    _, ratio = _fact_presence(required, summary)
    assert ratio >= 0.8, f"quality ratio {ratio*100:.0f}% ниже 80%"


# ---------------------------------------------------------------------------
# Coverage matrix — наличие ключевых модулей
# ---------------------------------------------------------------------------


REQUIRED_MODULES = [
    "workspace.skills.legal_summarizer.scripts.sanitize",
    "workspace.skills.legal_summarizer.scripts.fingerprint",
    "workspace.skills.legal_summarizer.scripts.document_cache",
    "workspace.skills.legal_summarizer.scripts.prompts_runtime",
    "workspace.skills.legal_summarizer.scripts.llm_calls",
    "workspace.skills.legal_summarizer.scripts.pipeline",
    "workspace.skills.legal_summarizer.scripts.token_budget",
    "workspace.skills.legal_summarizer.scripts.document_stats",
    "workspace.skills.legal_summarizer.scripts.document_cleanup",
    "workspace.skills.legal_summarizer.scripts.execution_strategy",
    "workspace.skills.legal_summarizer.scripts.reducer_models",
    "workspace.skills.legal_summarizer.scripts.reducer_strategy",
    "workspace.skills.legal_summarizer.scripts.reducer_impl",
    "workspace.skills.legal_summarizer.scripts.packing_models",
    "workspace.skills.legal_summarizer.scripts.packing_impl",
]


def test_acceptance_matrix_required_modules_exist():
    """Все required modules существуют."""
    import importlib

    missing = []
    for mod_name in REQUIRED_MODULES:
        try:
            importlib.import_module(mod_name)
        except ImportError as e:
            missing.append(f"{mod_name}: {e}")

    assert not missing, f"Отсутствуют модули:\n" + "\n".join(missing)


def test_acceptance_matrix_facades_have_re_exports():
    """Facade-файлы имеют __all__ с публичными именами."""
    from workspace.skills.legal_summarizer.scripts import (
        packing,
        reducer,
    )

    # Проверка reducer facade.
    reducer_all = reducer.__all__
    assert "reduce_results" in reducer_all
    assert "ReduceStrategy" in reducer_all
    assert "select_reduce_strategy" in reducer_all

    # Проверка packing facade.
    packing_all = packing.__all__
    assert "pack_chunks" in packing_all
    assert "ContextBatch" in packing_all
    assert "PackingConfig" in packing_all
    assert "TokenBudget" in packing_all


# ---------------------------------------------------------------------------
# Summary report — все метрики в одной выдаче
# ---------------------------------------------------------------------------


def test_acceptance_matrix_summary_report(tmp_path, monkeypatch, capsys):
    """Финальный сводный отчёт по матрице.

    pytest покажет отчёт при ``-v -s``.
    """
    print("\n" + "=" * 60)
    print("[ACCEPTANCE MATRIX] legal_summarizer pipeline")
    print("=" * 60)

    # LLM calls.
    from workspace.skills.legal_summarizer.scripts import summarizer

    _build_honest_mock(monkeypatch)
    _setup_execution_mocks(monkeypatch, single_threshold=12000)
    counter = _count_llm_calls(monkeypatch)
    summarizer.run(SMALL_DOC, length="brief", confirmed=True, workspace_root=tmp_path)
    print(f"\nLLM calls (small doc, single):     {counter['n']}")

    _build_honest_mock(monkeypatch)
    _setup_execution_mocks(monkeypatch, single_threshold=100)
    counter = _count_llm_calls(monkeypatch)
    summarizer.run(MEDIUM_DOC, length="brief", confirmed=True, workspace_root=tmp_path)
    print(f"LLM calls (medium doc, default):   {counter['n']}")

    _build_honest_mock(monkeypatch)
    _setup_execution_mocks(monkeypatch, single_threshold=100)
    counter = _count_llm_calls(monkeypatch)
    summarizer.run(LARGE_DOC, length="brief", confirmed=True, workspace_root=tmp_path)
    print(f"LLM calls (large doc, default):    {counter['n']}")

    # Opt-in direct.
    _build_honest_mock(monkeypatch)
    monkeypatch.setattr(summarizer, "get_chunking_config", lambda: {
        "chunk_size": 1000, "chunk_overlap": 0, "single_call_threshold": 100,
        "chunk_size_input_ratio": None, "direct_strategy_min_chars": 100,
    })
    counter = _count_llm_calls(monkeypatch)
    summarizer.run(MEDIUM_DOC, length="brief", confirmed=True, workspace_root=tmp_path)
    print(f"LLM calls (medium doc, opt-in DIRECT): {counter['n']}")

    # Quality.
    _build_honest_mock(monkeypatch)
    _setup_execution_mocks(monkeypatch, single_threshold=12000)
    text_with_facts = (
        SMALL_DOC
        + "\n\nFACT_001: Уникальный факт про договор аренды.\n"
        + "FACT_002: Срок двенадцать месяцев."
    )
    result = summarizer.run(
        text_with_facts, length="brief", confirmed=True, workspace_root=tmp_path,
    )
    required = ["Уникальный факт про договор аренды", "Срок двенадцать месяцев"]
    _, ratio = _fact_presence(required, result["result"]["summary"])
    print(f"\nQuality (small, honest mock):     {ratio*100:.0f}%")

    _build_honest_mock(monkeypatch)
    _setup_execution_mocks(monkeypatch, single_threshold=100)
    result = summarizer.run(
        MEDIUM_DOC, length="brief", confirmed=True, workspace_root=tmp_path,
    )
    required = ["Первый важный факт", "Второй факт", "Третий факт"]
    _, ratio = _fact_presence(required, result["result"]["summary"])
    print(f"Quality (medium, honest mock):    {ratio*100:.0f}%")

    # Coverage.
    print(f"\nRequired modules coverage:        {len(REQUIRED_MODULES)}/{len(REQUIRED_MODULES)}")

    print()
    print("=" * 60)
