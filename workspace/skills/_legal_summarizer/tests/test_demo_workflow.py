"""Демо end-to-end: --length detailed → --question."""
import sys
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def test_demo_workflow_step_by_step(tmp_path, monkeypatch):
    """4-step демо полного workflow."""
    import application.service as summarizer
    from cache.document_cache import DocumentCache
    from document.identity import DocumentIdentity

    text = """1. Раздел 1. Предмет

1.1. Заказчик поручает, а Исполнитель принимает на себя обязательства по оказанию услуг.
1.2. Срок оказания услуг — 30 дней с момента подписания.
1.3. При нарушении срока начисляется неустойка в размере 0.1% за каждый день просрочки.
1.4. Стоимость услуг составляет 1 000 000 рублей.

2. Раздел 2. Обязанности сторон

2.1. Исполнитель обязуется:
а) оказать услуги качественно и в срок;
б) предоставить акт выполненных работ.

2.2. Заказчик обязуется:
а) оплатить услуги в течение 5 банковских дней.

3. Раздел 3. Ответственность

3.1. Стороны несут ответственность в соответствии с законодательством РФ.
3.2. Споры разрешаются в Арбитражном суде.
"""

    recorded = {"batch": 0, "section": 0, "doc": 0}

    def _fake_batch(chunks, *, chunks_total, structure, length, question=None):
        recorded["batch"] += 1
        return {c.chunk_id: f"summary_{c.chunk_id}_q={question}" for c in chunks}

    def _fake_section(path, heading, text, *, length, question=None):
        recorded["section"] += 1
        return f"sec_summary[{heading}]_q={question}"

    def _fake_doc(text, *, length, focus, structure, question=None):
        recorded["doc"] += 1
        return f"final_answer_q={question}_iter{recorded['doc']}"

    from llm import calls as llm_calls
    monkeypatch.setattr(llm_calls, "llm_batch", _fake_batch)
    monkeypatch.setattr(llm_calls, "llm_section_reduce", _fake_section)
    monkeypatch.setattr(llm_calls, "llm_document_reduce", _fake_doc)

    p = tmp_path / "doc.txt"
    p.write_text(text, encoding="utf-8")
    document_id = DocumentIdentity.from_path(p).document_id

    # STEP 1: --length detailed (полный анализ)
    print("\n=== STEP 1: --length detailed ===")
    r1 = summarizer.run(
        text, length="detailed",
        document_path=str(p), workspace_root=tmp_path, confirmed=True,
    )
    assert r1["status"] in ("completed", "partial")
    print(f"  batch={recorded['batch']}, section={recorded['section']}, doc={recorded['doc']}")
    cache = DocumentCache(tmp_path)
    assert cache.is_complete(document_id)
    initial_batch, initial_section, initial_doc = (
        recorded["batch"], recorded["section"], recorded["doc"],
    )

    # STEP 2: --question "штраф за просрочку?"
    print("\n=== STEP 2: --question 'штраф за просрочку?' ===")
    recorded["batch"] = recorded["section"] = recorded["doc"] = 0
    r2 = summarizer.run(
        text, question="Какой штраф за просрочку?",
        document_path=str(p), workspace_root=tmp_path,
    )
    assert r2["status"] == "completed"
    assert r2["result"]["strategy"] == "document_cache_question"
    assert recorded["batch"] == 0
    assert recorded["doc"] == 1
    print(f"  strategy={r2['result']['strategy']}, batch={recorded['batch']}, doc={recorded['doc']}")
    print(f"  summary={r2['result']['summary']}")

    # STEP 3: --question "кто отвечает за качество?"
    print("\n=== STEP 3: --question 'кто отвечает за качество?' ===")
    recorded["batch"] = recorded["section"] = recorded["doc"] = 0
    r3 = summarizer.run(
        text, question="Кто отвечает за качество услуг?",
        document_path=str(p), workspace_root=tmp_path,
    )
    assert r3["status"] == "completed"
    assert recorded["batch"] == 0
    assert recorded["doc"] == 1

    # STEP 4: idempotency (повтор того же вопроса)
    print("\n=== STEP 4: повторный question (idempotency) ===")
    recorded["batch"] = recorded["section"] = recorded["doc"] = 0
    r4 = summarizer.run(
        text, question="Какой штраф за просрочку?",
        document_path=str(p), workspace_root=tmp_path,
    )
    assert r4["status"] == "completed"
    assert r4["stats"].get("cached") is True
    assert recorded["batch"] == 0
    assert recorded["doc"] == 0
    print(f"  cached={r4['stats'].get('cached')}, batch={recorded['batch']}, doc={recorded['doc']}")

    print("\n=== Все 4 шага прошли ===")
    print(f"  Step 1: batch={initial_batch}, section={initial_section}, doc={initial_doc}")
    print(f"  Step 2 (q='штраф?'): batch=0, doc=1, strategy=document_cache_question")
    print(f"  Step 3 (q='кто отвечает?'): batch=0, doc=1")
    print(f"  Step 4 (повтор q): cached=True, 0 LLM")
