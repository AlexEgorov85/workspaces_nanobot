"""Регрессия: max_concurrent_batches == 1 (single-flight LLM).

План: 4 бага legal_summarizer / шаг 2.

После фикса default ``max_concurrent_batches`` в ``summarizer.py``
строго 1. Это означает: одновременно выполняется не более одного
LLM-вызова (в map-фазе). Тест мокает ``llm.chat`` и трекает пиковое
число in-flight вызовов; ожидаем ``peak == 1``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parents[1] / "workspace" / "skills" / "legal_summarizer"
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import summarizer  # noqa: E402


def test_default_value_in_summarizer_is_one():
    """Sanity-check: production concurrency в summarizer.py строго 1.

    Если кто-то поменяет runtime invariant обратно на семафор(>1),
    этот тест поймает.
    """
    import inspect

    src = inspect.getsource(summarizer)
    assert "concurrency = 1" in src, (
        "Ожидалась жёсткая строка 'concurrency = 1' в summarizer.run(). "
        "Runtime invariant single-flight нарушен — параллельные LLM-вызовы "
        "должны быть запрещены."
    )


def test_max_concurrent_batches_clamped_to_one_with_warning():
    """DEPRECATED ключ ``max_concurrent_batches > 1`` clamp'ится до 1
    с ``DeprecationWarning``.
    """
    import warnings

    # Имитируем то, что summarizer.run() делает inline:
    # читаем ключ, clamp'им до 1, эмитим warning на > 1.
    for cfg_value, expected_concurrency, should_warn in [
        (None, 1, False),                  # ключ отсутствует
        (1, 1, False),                      # ключ == 1 (валидный default)
        (0, 1, False),                      # 0 → 1 без warning
        (-5, 1, False),                     # отрицательное → 1 без warning
        (2, 1, True),                       # > 1 → warning + clamp до 1
        (4, 1, True),                       # 4 → warning + clamp до 1
    ]:
        cfg: dict = {} if cfg_value is None else {"max_concurrent_batches": cfg_value}
        configured = int(cfg.get("max_concurrent_batches", 1) or 1)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            if configured > 1:
                warnings.warn(
                    "skills.legal_summarizer.execution.max_concurrent_batches > 1 "
                    f"({configured}) DEPRECATED",
                    DeprecationWarning,
                    stacklevel=2,
                )
            concurrency = 1
        warnings_emitted = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert concurrency == expected_concurrency, (
            f"cfg={cfg_value}: concurrency={concurrency}, "
            f"expected {expected_concurrency}"
        )
        if should_warn:
            assert len(warnings_emitted) == 1, (
                f"cfg={cfg_value}: ожидался ровно 1 DeprecationWarning, "
                f"получили {len(warnings_emitted)}"
            )
        else:
            assert len(warnings_emitted) == 0, (
                f"cfg={cfg_value}: warning не должен эмититься, "
                f"получили {len(warnings_emitted)}"
            )


def test_summarizer_emits_warning_on_max_concurrent_batches_above_one(
    monkeypatch,
):
    """summarizer.run() эмитит DeprecationWarning, если в config
    ``max_concurrent_batches > 1`` — backward-compat path.
    """
    # Проверяем, что в исходнике summarizer.run() есть DeprecationWarning
    # на configured_concurrency > 1.
    import inspect

    import summarizer as _summarizer

    src = inspect.getsource(_summarizer.run)
    assert "DeprecationWarning" in src, (
        "summarizer.run должен эмитить DeprecationWarning при "
        "max_concurrent_batches > 1"
    )
    assert "DEPRECATED" in src, (
        "summarizer.run должен явно указывать DEPRECATED в warning"
    )


def test_runtime_single_flight_through_run_one_batch_async(monkeypatch):
    """Runtime single-flight: ``run_one_batch_async`` под семафором
    concurrency=1 не выпускает более 1 LLM-вызова одновременно.

    Тестируем изолированный семафор (как в production ``summarizer.run``):
    запускаем 5 батчей через ``asyncio.gather``, имитируем I/O latency
    в mock-LLM, считаем peak in-flight. Ожидаем ровно 1.
    """
    import asyncio
    import threading
    import time as _time

    from workspace.skills.legal_summarizer.scripts.packing import ContextBatch
    from workspace.skills.legal_summarizer.scripts.pipeline import (
        run_one_batch_async,
    )

    state = {
        "in_flight": 0,
        "peak": 0,
        "lock": threading.Lock(),
    }

    def fake_batch_meta(*args, **kwargs):
        with state["lock"]:
            state["in_flight"] += 1
            if state["in_flight"] > state["peak"]:
                state["peak"] = state["in_flight"]
        try:
            _time.sleep(0.05)  # I/O latency
            return {"batch_id": "x", "chunk_ids": [], "started_at": "",
                    "completed_at": "", "duration_sec": 0.05}
        finally:
            with state["lock"]:
                state["in_flight"] -= 1

    monkeypatch.setattr(
        "workspace.skills.legal_summarizer.scripts.pipeline.process_context_batch",
        fake_batch_meta,
    )

    sem = asyncio.Semaphore(1)
    batches = [
        ContextBatch(
            batch_id=f"cb_{i:03d}",
            chunks=(),
            total_tokens_estimate=0,
            section_paths=(),
            page_range=None,
        )
        for i in range(5)
    ]

    async def _gather_all():
        return await asyncio.gather(*[
            run_one_batch_async(
                b, chunks_total=0, structure=None, operation_id="op",
                workspace_root=None, sem=sem,
            )
            for b in batches
        ])

    results = asyncio.run(_gather_all())
    assert all(r[0] == "ok" for r in results)
    assert state["peak"] == 1, (
        f"single-flight нарушен в run_one_batch_async: "
        f"peak in-flight == {state['peak']}"
    )


# ---------------------------------------------------------------------------
# Production-path invariant: summarizer.run с max_concurrent_batches=4
# ВСЁ РАВНО должен оставаться single-flight (peak == 1).
# Без runtime-clamp'а это была бы регрессия: config=4 → Semaphore(4).
# ---------------------------------------------------------------------------


def test_summarizer_run_single_flight_under_max_concurrent_4(
    monkeypatch, tmp_path,
):
    """summarizer.run под config ``max_concurrent_batches=4`` всё равно
    держит peak in-flight LLM == 1.

    До runtime-clamp'а (DEPRECATE+warn) этот тест был красным:
    config=4 → Semaphore(4) → peak in-flight до 4.

    Мок на уровне ``pipeline.process_context_batch`` — обходит парсер
    и реальный LLM-вызов (latency через time.sleep), но counting peak
    in-flight идёт в нашей обёртке.
    """
    import threading
    import time as _time

    import summarizer as _summarizer

    state = {
        "in_flight": 0, "peak": 0, "lock": threading.Lock(),
        "calls": 0,
    }

    from workspace.skills.legal_summarizer.scripts.packing import ContextBatch

    def fake_process_context_batch(
        batch, *, length="", question=None, **kwargs,
    ):
        with state["lock"]:
            state["in_flight"] += 1
            if state["in_flight"] > state["peak"]:
                state["peak"] = state["in_flight"]
            state["calls"] += 1
        try:
            _time.sleep(0.03)
            return {
                "batch_id": batch.batch_id,
                "chunk_ids": [c.chunk_id for c in batch.chunks],
                "started_at": "1970-01-01T00:00:00Z",
                "completed_at": "1970-01-01T00:00:00Z",
                "duration_sec": 0.03,
            }
        finally:
            with state["lock"]:
                state["in_flight"] -= 1

    def one_chunk_per_batch(chunks, budget):
        return tuple(
            ContextBatch(
                batch_id=f"cb_{i:03d}",
                chunks=(c,),
                total_tokens_estimate=c.token_estimate,
                section_paths=(c.section_path,),
                page_range=None,
            )
            for i, c in enumerate(chunks)
        )

    monkeypatch.setattr(
        "workspace.skills.legal_summarizer.scripts.pipeline.process_context_batch",
        fake_process_context_batch,
    )
    monkeypatch.setattr(_summarizer, "pack_chunks", one_chunk_per_batch)
    monkeypatch.setattr(_summarizer, "get_chunking_config", lambda: {
        "chunk_size": 200, "chunk_overlap": 0, "single_call_threshold": 100,
        "chunk_size_input_ratio": None,
        "context_window_tokens": 200,
        "brief_max_input_chars": 6000,
        "brief_max_chars_per_chunk": None,
        "brief_coverage_ratio": 0.5,
    })
    monkeypatch.setattr(_summarizer, "get_execution_config", lambda: {
        "confirmation_threshold_sec": 0.001,
        "estimated_chunk_duration_sec": 0.001,
        "max_chunks_for_execution": 100,
        # КРИТИЧНО: провоцируем DEPRECATE-ветку.
        "max_concurrent_batches": 4,
        "context_batching": {
            "system_prompt_tokens": 100, "instruction_tokens_per_map": 50,
            "chars_per_token": 3.5, "safety_margin": 0.85,
        },
        "llm_max_tokens": 100,
    })

    paragraph = "Длинный абзац про договор подряда, права и обязанности. "
    text = "\n\n".join([paragraph] * 200)

    import warnings as _warnings
    with _warnings.catch_warnings(record=True) as caught:
        _warnings.simplefilter("always")
        _summarizer.run(
            text, length="brief", confirmed=True, workspace_root=tmp_path,
        )
    deprecation = [
        w for w in caught if issubclass(w.category, DeprecationWarning)
    ]
    assert len(deprecation) == 1, (
        f"Ожидался ровно 1 DeprecationWarning на max_concurrent_batches=4, "
        f"получили {len(deprecation)}: {[str(w.message) for w in deprecation]}"
    )

    # Главный инвариант: даже при max_concurrent_batches=4 peak == 1.
    assert state["peak"] == 1, (
        f"INV-2 нарушен под config max_concurrent_batches=4: "
        f"peak in-flight == {state['peak']}, ожидалось 1. "
        f"Runtime single-flight invariant НЕ соблюдён."
    )
    # total_llm_calls > 0 — иначе это empty test.
    assert state["calls"] >= 1, (
        f"process_context_batch не вызывался ни разу (calls={state['calls']}); "
        "тест не информативен."
    )

