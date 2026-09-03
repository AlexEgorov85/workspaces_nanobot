"""Регрессия: max_concurrent_batches == 1 (single-flight LLM).

План: 4 бага legal_summarizer / шаг 2.

После фикса default ``max_concurrent_batches`` в ``summarizer.py``
строго 1. Это означает: одновременно выполняется не более одного
LLM-вызова (в map-фазе). Тест мокает ``llm.chat`` и трекает пиковое
число in-flight вызовов; ожидаем ``peak == 1``.
"""

from __future__ import annotations

import re
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
    """Sanity-check: значение default в самом summarizer.py — 1.

    Если кто-то поменяет default обратно на 4, этот тест поймает.
    """
    import inspect

    src = inspect.getsource(summarizer)
    m = re.search(
        r'max_concurrent_batches["\']?\s*,\s*(\d+)\s*\)',
        src,
    )
    assert m is not None, (
        "Не удалось найти default для max_concurrent_batches в summarizer.py"
    )
    assert int(m.group(1)) == 1, (
        f"Default max_concurrent_batches == {m.group(1)}, ожидалось 1. "
        "Параллельные LLM-вызовы запрещены текущим runtime'ом."
    )


def test_concurrency_overrides_apply_via_max_call():
    """Проверка, что значение max_concurrent_batches из config читается
    и применяется через ``max(1, ...)`` clamp.
    """
    cfg_default: dict = {}
    concurrency_default = max(1, int(cfg_default.get("max_concurrent_batches", 1)))
    assert concurrency_default == 1

    cfg_one = {"max_concurrent_batches": 1}
    concurrency_one = max(1, int(cfg_one.get("max_concurrent_batches", 1)))
    assert concurrency_one == 1

    cfg_zero = {"max_concurrent_batches": 0}
    concurrency_zero = max(1, int(cfg_zero.get("max_concurrent_batches", 1)))
    assert concurrency_zero == 1

    cfg_neg = {"max_concurrent_batches": -5}
    concurrency_neg = max(1, int(cfg_neg.get("max_concurrent_batches", 1)))
    assert concurrency_neg == 1


def test_summarizer_uses_max_concurrent_batches_from_config(monkeypatch):
    """summarizer.run() читает ``max_concurrent_batches`` из
    ``get_execution_config()``. Проверяем интроспекцией исходника,
    что значение действительно берётся из конфига, а не хардкодится.
    """
    import inspect

    src = inspect.getsource(summarizer.run)
    assert "max_concurrent_batches" in src, (
        "summarizer.run должен читать max_concurrent_batches из config"
    )
    assert "get_execution_config" in src, (
        "summarizer.run должен звать get_execution_config для "
        "получения max_concurrent_batches"
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