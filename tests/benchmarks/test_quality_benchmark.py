"""Quality benchmark с golden required_facts dataset.

Покрывает:

* **golden_documents.json** — набор small documents с обязательными фактами.
* **run_quality_check.py** — runner, который прогоняет документы через
  pipeline (с mock LLM) и проверяет факт-presence.
* **pytest integration** — каждый документ из golden → required_facts.

Mock LLM «повторяет» все факты в summary → acceptance ratio = 100%.

Это НЕ реальный LLM-benchmark (зависит от модели); это **deterministic
test harness** для проверки fact-extraction и presence-checker.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO / "workspace" / "skills" / "legal_summarizer" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


# Golden dataset (внутри test file для простоты).
GOLDEN_DOCUMENTS: list[dict[str, Any]] = [
    {
        "name": "short_contract",
        "text": (
            "Вступление.\n\n"
            "FACT_001: Договор аренды заключается в письменной форме.\n"
            "FACT_002: Срок аренды — 12 месяцев.\n"
            "FACT_003: Арендная плата 50000 рублей в месяц.\n\n"
            "Заключение."
        ),
        "required_facts": [
            "Договор аренды",
            "12 месяцев",
            "50000 рублей",
        ],
    },
    {
        "name": "rental_terms",
        "text": (
            "Условия аренды помещения.\n\n"
            "FACT_001: Арендодатель — ООО Ромашка.\n"
            "FACT_002: Арендатор — ИП Иванов Иван Иванович.\n"
            "FACT_003: Помещение расположено по адресу: г. Москва, ул. Ленина, 1.\n"
            "FACT_004: Дата начала аренды — 1 января 2026 года.\n"
            "FACT_005: Дата окончания — 31 декабря 2026 года.\n\n"
            "Подписи сторон."
        ),
        "required_facts": [
            "ООО Ромашка",
            "ИП Иванов",
            "г. Москва",
            "1 января 2026",
            "31 декабря 2026",
        ],
    },
    {
        "name": "service_agreement",
        "text": (
            "Договор оказания услуг.\n\n"
            "FACT_001: Заказчик — ООО ТехноСервис.\n"
            "FACT_002: Исполнитель — ИП Петров П.П.\n"
            "FACT_003: Стоимость услуг — 100000 рублей.\n"
            "FACT_004: Срок выполнения — 30 рабочих дней.\n\n"
            "Реквизиты."
        ),
        "required_facts": [
            "ООО ТехноСервис",
            "ИП Петров",
            "100000 рублей",
            "30 рабочих дней",
        ],
    },
]


def _extract_required_facts(text: str) -> list[str]:
    """Извлечь «факты» из текста (формат ``FACT_NNN: ...``)."""
    pattern = re.compile(r"FACT_(\d+):\s*(.+?)(?=\n|$)", re.MULTILINE)
    return [m.group(2).strip() for m in pattern.finditer(text)]


def _fact_presence(facts: list[str], text: str) -> tuple[list[bool], float]:
    """Для каждого факта — есть ли его содержание в ``text``."""
    presence = [bool(f and f in text) for f in facts]
    ratio = sum(presence) / len(presence) if presence else 0.0
    return presence, ratio


# ---------------------------------------------------------------------------
# Golden dataset: structural validation
# ---------------------------------------------------------------------------


def test_golden_dataset_has_unique_names():
    """Все golden documents имеют уникальные имена."""
    names = [d["name"] for d in GOLDEN_DOCUMENTS]
    assert len(names) == len(set(names)), (
        f"Дублирующиеся имена в golden dataset: {names}"
    )


def test_golden_dataset_all_have_required_facts():
    """Каждый golden document имеет required_facts."""
    for d in GOLDEN_DOCUMENTS:
        assert "required_facts" in d, f"{d['name']}: нет required_facts"
        assert isinstance(d["required_facts"], list)
        assert len(d["required_facts"]) > 0, (
            f"{d['name']}: required_facts пуст"
        )


def test_golden_dataset_texts_contain_required_facts():
    """Каждый required_fact присутствует в тексте документа."""
    for d in GOLDEN_DOCUMENTS:
        for f in d["required_facts"]:
            assert f in d["text"], (
                f"{d['name']}: required_fact {f!r} отсутствует в тексте"
            )


def test_golden_dataset_extracted_facts_match():
    """Extracted FACT_NNN из текста совпадают с required_facts."""
    for d in GOLDEN_DOCUMENTS:
        extracted = _extract_required_facts(d["text"])
        # Каждый extracted fact содержит хотя бы один required fact как substring.
        # (Это гарантирует что required_facts извлекаемы из текста.)
        for required in d["required_facts"]:
            found = any(required in ext for ext in extracted)
            assert found, (
                f"{d['name']}: required {required!r} не извлекается из текста"
            )


# ---------------------------------------------------------------------------
# Quality runner: mock LLM + presence check
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_honest_llm(monkeypatch):
    """Mock LLM, который возвращает summary с фактами документа."""
    from workspace.skills.legal_summarizer.scripts import summarizer

    def fake_chat(messages, *, context=None, **kwargs):
        # Найти FACT_NNN в user message и вернуть в summary.
        user_content = messages[1]["content"]
        facts = re.findall(r"FACT_\d+:\s*(.+?)(?=\n|$)", user_content, re.MULTILINE)
        # Сформировать summary со всеми фактами (honest mock).
        summary_lines = ["Саммари документа (honest mock)."]
        for i, f in enumerate(facts, 1):
            summary_lines.append(f"Факт {i}: {f}.")
        return "\n".join(summary_lines)

    monkeypatch.setattr(summarizer.llm, "chat", fake_chat)


@pytest.fixture
def mock_bad_llm(monkeypatch):
    """Mock LLM, который возвращает пустой summary (без фактов)."""
    from workspace.skills.legal_summarizer.scripts import summarizer

    def fake_chat(messages, *, context=None, **kwargs):
        return "Саммари без каких-либо конкретных фактов из документа."

    monkeypatch.setattr(summarizer.llm, "chat", fake_chat)


@pytest.fixture
def execution_mocks(monkeypatch):
    """Mock chunking_config и execution_config для детерминизма."""
    from workspace.skills.legal_summarizer.scripts import summarizer

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


# ---------------------------------------------------------------------------
# Quality tests с honest mock (LLM возвращает все факты)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("doc", GOLDEN_DOCUMENTS, ids=lambda d: d["name"])
def test_quality_benchmark_honest_mock_passes_acceptance(
    doc, tmp_path, mock_honest_llm, execution_mocks,
):
    """Acceptance: honest mock → ≥80% required_facts в summary."""
    from workspace.skills.legal_summarizer.scripts import summarizer

    result = summarizer.run(
        doc["text"], length="brief", confirmed=True, workspace_root=tmp_path,
    )
    assert result["status"] == "completed"

    summary = result["result"]["summary"]
    presence, ratio = _fact_presence(doc["required_facts"], summary)
    missing = [f for f, p in zip(doc["required_facts"], presence) if not p]
    assert ratio >= 0.8, (
        f"{doc['name']}: {ratio*100:.0f}% facts present "
        f"(missing: {missing})"
    )


# ---------------------------------------------------------------------------
# Quality tests с bad mock (LLM возвращает пустой summary)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("doc", GOLDEN_DOCUMENTS, ids=lambda d: d["name"])
def test_quality_benchmark_bad_mock_detects_degradation(
    doc, tmp_path, mock_bad_llm, execution_mocks,
):
    """Bad mock → ratio=0 (фиксирует detection baseline)."""
    from workspace.skills.legal_summarizer.scripts import summarizer

    result = summarizer.run(
        doc["text"], length="brief", confirmed=True, workspace_root=tmp_path,
    )
    assert result["status"] == "completed"

    summary = result["result"]["summary"]
    presence, ratio = _fact_presence(doc["required_facts"], summary)
    assert ratio < 0.5, (
        f"{doc['name']}: bad mock не должен проходить quality check, "
        f"но ratio={ratio:.2f}"
    )


# ---------------------------------------------------------------------------
# Quality summary report (для ручного анализа)
# ---------------------------------------------------------------------------


def test_quality_benchmark_summary_report(tmp_path, mock_honest_llm, execution_mocks):
    """Сводный отчёт по всем golden документам.

    pytest покажет отчёт при ``-v -s``.
    """
    from workspace.skills.legal_summarizer.scripts import summarizer

    print("\n[quality benchmark] Сводный отчёт по golden dataset:")
    print(f"  Документов: {len(GOLDEN_DOCUMENTS)}")
    print()

    total_facts = 0
    total_present = 0

    for doc in GOLDEN_DOCUMENTS:
        result = summarizer.run(
            doc["text"], length="brief", confirmed=True, workspace_root=tmp_path,
        )
        summary = result["result"]["summary"]
        presence, ratio = _fact_presence(doc["required_facts"], summary)
        total_facts += len(doc["required_facts"])
        total_present += sum(presence)
        print(f"  {doc['name']}: {ratio*100:.0f}% ({sum(presence)}/{len(presence)})")

    overall = total_present / total_facts if total_facts else 0
    print()
    print(f"  Overall: {overall*100:.0f}% ({total_present}/{total_facts})")
    assert overall >= 0.8, (
        f"Overall quality {overall*100:.0f}% ниже 80% acceptance"
    )


# ---------------------------------------------------------------------------
# Edge: empty required_facts
# ---------------------------------------------------------------------------


def test_quality_benchmark_empty_required_facts_passes(tmp_path, mock_honest_llm, execution_mocks):
    """Пустой required_facts → ratio=0 (без деления на 0)."""
    from workspace.skills.legal_summarizer.scripts import summarizer

    text = "Любой документ без маркеров FACT_NNN."
    result = summarizer.run(
        text, length="brief", confirmed=True, workspace_root=tmp_path,
    )
    assert result["status"] == "completed"

    # Empty list → no facts to check → не должно падать.
    presence, ratio = _fact_presence([], result["result"]["summary"])
    assert presence == []
    assert ratio == 0.0
