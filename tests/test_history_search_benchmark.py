"""Baseline benchmark для ``history_search`` (Этап 2 плана).

Запускается отдельно от основного прогона через метку ``benchmark``:

    pytest -m benchmark tests/test_history_search_benchmark.py -v

Метрики:
  * Recall@5 / Recall@10 — доля ожидаемых id в первых 5/10 результатах;
  * MRR — среднее обратное ранга первого ожидаемого события;
  * false_positive_rate — доля неверных матчей для негативных сценариев;
  * avg_latency_ms — среднее время одного ``execute()`` (на синтетике).

Эмулятор ``_simulate_baseline_sql`` воспроизводит поведение
``history_search_tool.py::execute`` (Этап 2) — текущее ILIKE + ORDER BY
timestamp DESC. Метрики фиксируются в
``docs/architecture/HISTORY_SEARCH_ANALYSIS.md`` §10.

Проверка оптимизированных вариантов (PROPOSAL-A в
``docs/architecture/HISTORY_SEARCH_SQL_PROPOSAL.md``) делается на реальной
БД через ``EXPLAIN ANALYZE`` — здесь только baseline.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import pytest

from workspace.tools.history_search_tool import (
    HistorySearchTool,
    HistorySearchToolConfig,
)


FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "history_search"
EVENTS_PATH = FIXTURE_DIR / "gateway_logs.jsonl"
SCENARIOS_PATH = FIXTURE_DIR / "scenarios.json"


pytestmark = pytest.mark.benchmark


# -------------------------------------------------------------------- helpers


def _load_events() -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with EVENTS_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            events.append(json.loads(line))
    events.sort(key=lambda e: e["timestamp"], reverse=True)
    return events


def _load_scenarios() -> list[dict[str, Any]]:
    with SCENARIOS_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def _stable_key(ev: dict[str, Any]) -> str:
    """Стабильный ключ события — ``event_id`` из JSON-ответа tool'а.

    Gap №3 закрыт: ``history_search_tool.py`` теперь возвращает ``event_id``
    в каждом событии (UUID из ``public.agent_gateway_logs.id``). Это
    прямое и однозначное сопоставление ожидаемого и реально вернувшегося
    события.
    """
    return str(ev.get("event_id") or "")


def _make_event_id_set(events: list[dict]) -> set[str]:
    return {e["id"] for e in events if e.get("id")}


def _simulate_baseline_sql(sql: str, params: tuple, events: list[dict]) -> list[dict]:
    """Эмулятор текущего SQL: фильтры + ORDER BY timestamp DESC + LIMIT.

    Воспроизводит поведение ``history_search_tool.py::execute`` (Этап 2).
    Парсит SQL для определения применённых фильтров — это устойчивее, чем
    жёсткая привязка к индексу параметра, потому что опциональные фильтры
    могут отсутствовать и индексы сдвигаются.
    """
    if not params:
        return []

    allow_all = bool(params[0])
    session_id = params[1] or None

    # Разбираем SQL по меткам, чтобы понять, какие фильтры применены
    sql_lc = sql.lower()
    has_query = "ilike %s" in sql_lc
    has_tool_name = "name = %s" in sql_lc
    has_since = '"timestamp" >= %s' in sql_lc
    has_until = '"timestamp" <= %s' in sql_lc

    # Идём по params в порядке, который соответствует порядку clauses в tool
    idx = 2
    event_type = params[idx] if idx < len(params) else None
    idx += 2  # event_type повторяется дважды в clause

    tool_name = None
    if has_tool_name:
        if idx < len(params):
            tool_name = params[idx]
        idx += 1

    like = None
    if has_query:
        if idx + 1 < len(params):
            like = params[idx]
            # params[idx+1] — дубль для payload::text ILIKE
        idx += 2

    since = None
    if has_since:
        if idx < len(params):
            since = params[idx]
        idx += 1

    until = None
    if has_until:
        if idx < len(params):
            until = params[idx]
        idx += 1

    limit = params[-1]

    out = []
    for e in events:
        if not allow_all:
            if e.get("session_id") != session_id:
                continue
        if event_type and e.get("event_type") != event_type:
            continue
        if tool_name and e.get("name") != tool_name:
            continue
        ts = e.get("timestamp") or ""
        if since and ts < since:
            continue
        if until and ts > until:
            continue
        if like:
            stripped = like.strip("%")
            blob = (e.get("summary") or "") + " " + json.dumps(
                e.get("payload") or {}, ensure_ascii=False
            )
            if stripped.lower() not in blob.lower():
                continue
        out.append(e)
        if len(out) >= limit:
            break
    return out


def _make_fake_fetch(events: list[dict]):
    """Вернуть функцию ``fetch(sql, *params)``, повторяющую baseline-SQL."""

    def _fetch(sql: str, *params):
        return _simulate_baseline_sql(sql, params, events)

    return _fetch


# -------------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def events() -> list[dict[str, Any]]:
    return _load_events()


@pytest.fixture(scope="module")
def scenarios() -> list[dict[str, Any]]:
    return _load_scenarios()


@pytest.fixture
def tool() -> HistorySearchTool:
    return HistorySearchTool(config=HistorySearchToolConfig(
        max_rows=50, max_result_chars=80_000,
    ))


# -------------------------------------------------------------------- metrics


def _recall_at_k(returned_ids: list[str], expected: list[str], k: int) -> float:
    if not expected:
        return 1.0 if not returned_ids[:k] else 0.0
    top = returned_ids[:k]
    hit = sum(1 for x in expected if x in top)
    return hit / len(expected)


def _reciprocal_rank(returned_ids: list[str], expected: list[str]) -> float:
    if not expected:
        return 0.0 if returned_ids else 1.0
    for i, rid in enumerate(returned_ids, start=1):
        if rid in expected:
            return 1.0 / i
    return 0.0


def _is_false_positive(returned_ids: list[str], expected: list[str],
                       query: str) -> bool:
    """Сценарий 'negative'/'ambiguous': если что-то нашлось и expected пуст —
    это false positive. Если запрос пустой в payload — тоже шум."""
    if expected:
        return False
    return bool(returned_ids)


# -------------------------------------------------------------------- benchmark


@pytest.mark.asyncio
async def test_baseline_metrics(events, scenarios, tool, monkeypatch):
    """BASELINE: текущая логика tool'а (Этап 2).

    Прогон всех сценариев на текущей реализации. Возвращает агрегированные
    метрики в ``pytest -s`` выводе для записи в
    ``HISTORY_SEARCH_ANALYSIS.md`` §10. Тест всегда проходит
    (``benchmark`` не должен ломать CI).

    Проверка оптимизированных вариантов (PROPOSAL-A в
    ``HISTORY_SEARCH_SQL_PROPOSAL.md``) делается на реальной БД через
    ``EXPLAIN ANALYZE`` — здесь только baseline.
    """
    monkeypatch.setattr(
        "workspace.tools.history_search_tool._current_session_key",
        lambda: "postgres:1001",
    )
    monkeypatch.setattr("utils.db.fetch", _make_fake_fetch(events))

    recalls_5: list[float] = []
    recalls_10: list[float] = []
    rrs: list[float] = []
    fps = 0
    fps_total = 0
    latencies_ms: list[float] = []
    per_category: dict[str, dict[str, float]] = {}
    stable_ids = _make_event_id_set(events)

    for s in scenarios:
        kwargs: dict[str, Any] = {
            "query": s.get("query"),
            "event_type": s.get("expected_event_type"),
            "tool_name": None,
            "since": None,
            "until": None,
            "session_scope": s.get("session_scope", "all"),
            "limit": s.get("limit") or 50,
        }
        t0 = time.perf_counter()
        result_json = await tool.execute(**kwargs)
        dt = (time.perf_counter() - t0) * 1000.0
        latencies_ms.append(dt)

        data = json.loads(result_json)
        returned = data.get("events") or []

        expected_ids = [eid for eid in (s.get("expected_event_ids") or [])
                        if eid in stable_ids]
        returned_ids = [_stable_key(ev) for ev in returned]

        r5 = _recall_at_k(returned_ids, expected_ids, 5)
        r10 = _recall_at_k(returned_ids, expected_ids, 10)
        rr = _reciprocal_rank(returned_ids, expected_ids)

        recalls_5.append(r5)
        recalls_10.append(r10)
        rrs.append(rr)

        if not expected_ids:
            fps_total += 1
            if _is_false_positive(returned_ids, expected_ids, s.get("query") or ""):
                fps += 1

        cat = s["category"]
        agg = per_category.setdefault(
            cat, {"r5": 0.0, "r10": 0.0, "rr": 0.0, "n": 0})
        agg["r5"] += r5
        agg["r10"] += r10
        agg["rr"] += rr
        agg["n"] += 1

    def _avg(xs):
        return sum(xs) / len(xs) if xs else 0.0

    overall = {
        "n": len(scenarios),
        "Recall@5": _avg(recalls_5),
        "Recall@10": _avg(recalls_10),
        "MRR": _avg(rrs),
        "false_positive_rate": (fps / fps_total) if fps_total else 0.0,
        "avg_latency_ms": _avg(latencies_ms),
    }
    per_cat = {
        cat: {
            "n": v["n"],
            "Recall@5": v["r5"] / v["n"],
            "Recall@10": v["r10"] / v["n"],
            "MRR": v["rr"] / v["n"],
        }
        for cat, v in per_category.items()
    }

    print("\n=== BASELINE METRICS (history_search, Этап 2) ===")
    print(json.dumps({
        "overall": {k: round(v, 4) if isinstance(v, float) else v
                    for k, v in overall.items()},
        "by_category": {
            cat: {k: round(val, 4) if isinstance(val, float) else val
                  for k, val in vals.items()}
            for cat, vals in per_cat.items()
        },
    }, ensure_ascii=False, indent=2))

    assert overall["n"] == len(scenarios)
    assert overall["MRR"] >= 0.0
