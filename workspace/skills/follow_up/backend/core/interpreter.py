"""Follow Up 2.0 — понимание запроса относительно всей нити диалога.

Один вызов модели на ход. Возвращает не «интент» из закрытого списка, а три
вещи сразу: какая проверка в фокусе, каким ОБЯЗАН быть ответ и что для этого
вызвать. Список возможностей приходит из реестра — добавление инструмента не
требует правок здесь.

Tools API у GigaChat нет, поэтому протокол — JSON в тексте, разбираемый
`core/structured.py`. Ретрай ровно один и с текстом ошибки в диалоге: приём
отработан в арбитре резолвера.

При любом сбое — не исключение, а деградация: план из приоров. Ход, который
не понят, всё равно должен что-то ответить.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

SYSTEM = """Ты — планировщик поискового конвейера для ИТ-аудитора банка.
Твоя задача — понять вопрос ОТНОСИТЕЛЬНО диалога и выбрать инструменты.

ЖЁСТКИЕ ПРАВИЛА ПРЕДМЕТНОЙ ОБЛАСТИ:
- Каждая КМ (проверка) — отдельная, со своим актом и своими отклонениями.
  Никогда не подставляй номер проверки, которого нет в вопросе или в фокусе.
- Вопрос про ОХВАТ («в каких актах», «все акты», «сколько всего») — это вопрос
  про корпус. Сужать его до одной проверки нельзя.

ОТВЕТЬ СТРОГО ОДНИМ JSON-ОБЪЕКТОМ:
{
  "focus_check_id": "КМ-99-XXXXX" | null,
  "contract": {
    "kind": "passages|within_document|coverage|entity_rollup|facets|compare|corpus_profile",
    "needs_quote": true|false
  },
  "plan": [{"tool": "имя", "args": {...}}],
  "why": "одна фраза"
}

ВИДЫ ОТВЕТА:
- passages        — несколько релевантных фрагментов по теме
- within_document — вопрос про ОДИН конкретный акт
- coverage        — «в каких актах / все акты, где…»
- entity_rollup   — «в каких актах встречался человек / система»
- facets          — числа и фильтры по полям отклонений
- compare         — сравнение нескольких проверок
- corpus_profile  — «что вообще есть в базе»

План — от одного до трёх шагов. Ровно ОДИН шаг должен быть основным.
"""

USER = """{memory}
ВОПРОС АУДИТОРА: {question}

ДОСТУПНЫЕ ИНСТРУМЕНТЫ:
{manifest}

Верни только JSON."""


@dataclass
class Understanding:
    focus: Optional[str] = None
    contract: Dict = field(default_factory=dict)
    plan: List[Dict] = field(default_factory=list)
    why: str = ""
    degraded: bool = False
    raw: str = ""


async def understand(question: str, memory_digest: str, budget,
                     cancel=None, model: Optional[str] = None) -> Understanding:
    from backend.core import structured
    from backend.core.tools import registry
    from backend.llm.client import LLMUnavailable, generate_async

    manifest = registry.manifest()
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": USER.format(
            memory=memory_digest or "", question=question[:1500],
            manifest=manifest)},
    ]

    for attempt in (1, 2):
        if not budget.can_afford(1):
            logger.info("[interpreter] Бюджет исчерпан — деградирую")
            return Understanding(degraded=True)
        try:
            raw = await generate_async(messages, model=model, profile="interpret")
        except LLMUnavailable as e:
            logger.warning(f"[interpreter] Модель недоступна: {e.outcome.kind}")
            return Understanding(degraded=True)
        budget.charge(1, "interpreter")

        res = structured.parse(raw, want="object")
        if res.ok and isinstance(res.value.get("plan"), list):
            d = res.value
            return Understanding(
                focus=d.get("focus_check_id") or None,
                contract=d.get("contract") or {},
                plan=[s for s in d["plan"] if isinstance(s, dict) and s.get("tool")],
                why=str(d.get("why") or "")[:200], raw=raw)
        if attempt == 1:
            # Ошибку разбора подмешиваем в диалог — приём отработан в арбитре
            messages.append({"role": "assistant", "content": raw[:1500]})
            messages.append({"role": "user", "content":
                             f"Ответ невалиден ({res.error}). Верни ТОЛЬКО "
                             f"валидный JSON по описанной схеме."})
    logger.warning("[interpreter] JSON не получен и после ретрая")
    return Understanding(degraded=True)


def fallback_plan(question: str, focus: Optional[str]) -> Understanding:
    """План без модели: дешёвые лексические признаки.

    Ход, который не понят, всё равно должен что-то ответить — «Ошибка» это не
    ответ, а отказ от работы.
    """
    from backend.core.memory.bridge import is_corpus_wide

    low = (question or "").lower()
    if focus and not is_corpus_wide(question):
        return Understanding(
            focus=focus, contract={"kind": "within_document", "needs_quote": True},
            plan=[{"tool": "read_document",
                   "args": {"check_id": focus, "query": question}}],
            why="фокус диалога", degraded=True)
    if is_corpus_wide(question):
        return Understanding(
            contract={"kind": "coverage", "needs_quote": True},
            plan=[{"tool": "search_coverage", "args": {"query": question}}],
            why="охватная формулировка", degraded=True)
    if any(w in low for w in ("сколько", "дороже", "млн", "критичн")):
        return Understanding(
            contract={"kind": "facets", "needs_quote": False},
            plan=[{"tool": "query_deviations", "args": {}}],
            why="числовой вопрос", degraded=True)
    return Understanding(
        contract={"kind": "passages", "needs_quote": True},
        plan=[{"tool": "search_passages", "args": {"query": question}}],
        why="общий поиск", degraded=True)
