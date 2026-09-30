"""Follow Up 2.0 — Hypothesis Agent (движок гипотез 2.0).

Стартовый агент аудитора: «начинаю проверку по X» → проверяемые гипотезы-схемы.

Конвейер (материал собирается детерминированно, думает LLM на каждом этапе):
  ШАГ 0  сбор: отклонения по теме + смежные, статистика ущерба, карта покрытия
  ШАГ 1  MECHANICS — механики нарушений («почему стало возможно»)
  ШАГ 2  SCHEMES   — новые схемы злоупотреблений в периметре розницы
  ШАГ 3  SCORING   — ущерб/вероятность/обнаружимость → сортировка по ущербу

Гипотеза = схема (кто, через какую лазейку, какая выгода, признак в данных),
а не пересказ найденного отклонения. Факты только из базы; схемы — гипотезы
с прослеживаемым основанием.
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter, OrderedDict
from typing import Dict, List, Optional

from backend.agents.base import BaseAgent
from backend.core import identity, structured
from backend.llm.client import generate_async
from backend.llm.prompts._shared import FOLLOWUPS_INSTRUCTION, HISTORY_BLOCK_TEMPLATE
from backend.llm.prompts.clarification import (
    CLARIFICATION_SYSTEM,
    CLARIFICATION_USER_TEMPLATE,
)
from backend.llm.prompts.hypothesis import (
    HYPOTHESIS_SYSTEM,
    HYPOTHESIS_USER_TEMPLATE,
    MECHANICS_SYSTEM,
    MECHANICS_USER_TEMPLATE,
    RETAIL_SCOPE,
    SCHEMES_SYSTEM,
    SCHEMES_USER_TEMPLATE,
    SCORING_SYSTEM,
    SCORING_USER_TEMPLATE,
)
from backend.rag.context_builder import build_context, build_deviations_context
from backend.rag.query_understanding import QueryContext
from backend.storage.database import DeviationRepo, DocumentRepo, get_db

logger = logging.getLogger(__name__)

N_SCHEMES = 7                # сколько гипотез просим у модели
_MAX_DEVS = 40               # отклонений в контекст механик


# ──────────────────────────────────────────────────────────────────
# Хелперы сбора материала (без LLM)
# ──────────────────────────────────────────────────────────────────

def _parse_json_block(raw: str) -> Optional[Dict]:
    """JSON из ответа LLM (модель иногда оборачивает в текст/```json)."""
    return structured.parse_object(raw)


def _deviation_to_dict(d) -> Dict:
    def _parse_list(raw):
        if not raw:
            return []
        try:
            return json.loads(raw) if isinstance(raw, str) else list(raw)
        except (ValueError, TypeError):
            return []

    return {
        "check_id": d.check_id,
        "category": d.category,
        "description": d.description,
        "severity": d.severity,
        "financial_impact_rub": d.financial_impact_rub,
        "affected_systems": _parse_list(d.affected_systems),
        "regulation_refs": _parse_list(d.regulation_refs),
        "affected_count": d.affected_count,
        "responsible_unit": d.responsible_unit,
        "recommendation": d.recommendation,
    }


def _fmt_money(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    if v >= 1e9:
        return f"{v / 1e9:.1f} млрд ₽"
    if v >= 1e6:
        return f"{v / 1e6:.1f} млн ₽"
    if v >= 1e3:
        return f"{v / 1e3:.0f} тыс ₽"
    return f"{v:.0f} ₽"


def _build_similar_checks(chunks: List[Dict], available_km: List[str]) -> str:
    if not chunks:
        if not available_km:
            return "В базе нет проверок."
        return ("Прямых совпадений по теме не найдено. В базе доступны проверки: "
                + ", ".join(available_km[:20]))
    seen: "OrderedDict[str, str]" = OrderedDict()
    for c in chunks:
        km = c.get("check_id", "")
        if not km or km == "UNKNOWN" or km in seen:
            continue
        text = (c.get("text") or "").strip().replace("\n", " ")
        seen[km] = text[:140] + ("…" if len(text) > 140 else "")
    if not seen:
        return ("Прямых совпадений по теме не найдено. В базе доступны: "
                + ", ".join(available_km[:20]))
    return "\n".join(f"- {km}: {desc}" for km, desc in seen.items())


def _damage_stats(devs: List[Dict]) -> str:
    """Реальные суммы ущерба для калибровки оценок LLM."""
    with_money = [d for d in devs if d.get("financial_impact_rub")]
    if not with_money:
        return ("Сумм ущерба в базе по этой теме нет — оценивай порядок "
                "величины по объёму затронутых клиентов/операций.")
    amounts = sorted((float(d["financial_impact_rub"]) for d in with_money),
                     reverse=True)
    lines = [f"Кейсов с оценённым ущербом: {len(amounts)}",
             f"Максимальный: {_fmt_money(amounts[0])}",
             f"Медианный: {_fmt_money(amounts[len(amounts) // 2])}",
             f"Суммарно по теме: {_fmt_money(sum(amounts))}", "",
             "Крупнейшие кейсы (для сопоставления):"]
    for d in sorted(with_money,
                    key=lambda x: -float(x["financial_impact_rub"]))[:6]:
        lines.append(f"- {_fmt_money(d['financial_impact_rub'])} — "
                     f"{(d.get('description') or '')[:130]} [{d.get('check_id')}]")
    return "\n".join(lines)


def _systems_and_processes(devs: List[Dict], context: str) -> str:
    """Системы из отклонений + процессы (П-коды) из текстов актов."""
    systems = Counter()
    for d in devs:
        for s in (d.get("affected_systems") or []):
            if s:
                systems[str(s).strip()] += 1
    procs = Counter(re.findall(r"\bП\d{3,4}\b", context or ""))
    units = Counter(d.get("responsible_unit") for d in devs
                    if d.get("responsible_unit"))
    parts = []
    if systems:
        parts.append("Системы: " + ", ".join(
            f"{s} ({n})" for s, n in systems.most_common(12)))
    if procs:
        parts.append("Процессы: " + ", ".join(
            f"{p} ({n})" for p, n in procs.most_common(12)))
    if units:
        parts.append("Подразделения: " + ", ".join(
            f"{u}" for u, _ in units.most_common(6)))
    return "\n".join(parts) or "(в данных не указаны)"


def _covered_map(devs: List[Dict]) -> str:
    """Карта покрытия: что уже проверялось — чтобы не повторять дословно."""
    by_km: Dict[str, List[str]] = OrderedDict()
    for d in devs:
        km = d.get("check_id") or "—"
        by_km.setdefault(km, [])
        if len(by_km[km]) < 3:
            by_km[km].append((d.get("description") or "")[:110])
    if not by_km:
        return "(по теме в базе проверок нет)"
    return "\n".join(f"- {km}: " + "; ".join(items)
                     for km, items in list(by_km.items())[:12])


def _verify_km_refs(schemes: List[Dict], known_km: List[str]) -> int:
    """Вычищает выдуманные номера КМ из оснований гипотез.

    Прод-риск: LLM «дописывает» правдоподобные номера (КМ-99-12405), которых
    в базе нет — в банковском аудите это недопустимо. Оставляем только те
    номера, что реально есть в корпусе; остальные помечаем как аналогию.
    Возвращает число отброшенных ссылок.
    """
    known = {identity.normalize(k) or "" for k in known_km}
    known.discard("")
    dropped = 0
    for s in schemes:
        based = s.get("based_on")
        if not isinstance(based, dict):
            based = {}
            s["based_on"] = based
        refs = based.get("km") or []
        if isinstance(refs, str):
            refs = [refs]
        kept = []
        for r in refs:
            n = identity.normalize(str(r))
            if n and n in known:
                kept.append(n)
            else:
                dropped += 1
        based["km"] = kept
        if not kept:
            based["unverified"] = True
    if dropped:
        logger.warning(f"[Hypothesis] Отброшено выдуманных ссылок на КМ: {dropped}")
    return dropped


def _build_topic_catalog() -> str:
    with get_db() as db:
        docs = DocumentRepo.list_all(db)
        if not docs:
            return "(база пуста)"
        lines = []
        for doc in docs:
            cats = sorted({d.category for d in doc.deviations if d.category})
            topic = (doc.topic or "").strip() or doc.filename
            cats_str = (" — " + ", ".join(cats)) if cats else ""
            lines.append(f"- **{doc.check_id}** — {topic}{cats_str}")
        return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────
# Рендер результата
# ──────────────────────────────────────────────────────────────────

_PROB_RU = {"high": "высокая", "medium": "средняя", "low": "низкая"}
_DET_RU = {"easy": "простая выгрузка", "medium": "сверка источников",
           "hard": "Process Mining / ручная выборка"}
_AXIS_RU = {"перенос": "перенос механики", "комбинация": "комбинация механик",
            "инсайдер": "инсайдер", "контрагент": "внешний контрагент",
            "слепая зона": "слепая зона"}


def _render(topic: str, similar: str, schemes: List[Dict],
            mechanics: List[Dict], devs: List[Dict]) -> str:
    """Markdown-ответ: гипотезы, отсортированные по потенциальному ущербу."""
    out: List[str] = []

    n_km = len({d.get("check_id") for d in devs if d.get("check_id")})
    out.append(f"## Что уже находили по теме «{topic}»\n")
    if devs:
        out.append(f"Проверок в базе: **{n_km}**, отклонений: **{len(devs)}**.\n")
        out.append(similar + "\n")
    else:
        out.append("Прямых кейсов по теме в базе нет — гипотезы ниже выведены "
                   "из механик смежных проверок и помечены соответственно.\n")

    if mechanics:
        out.append("\n**Механики нарушений, извлечённые из актов:**\n")
        for m in mechanics[:6]:
            km = ", ".join(m.get("seen_in_km") or [])
            rep = m.get("repeat_count")
            rep_s = f" · встречалась в {rep} КМ" if rep and rep > 1 else ""
            out.append(f"- **{m.get('name', '')}** — {m.get('essence', '')} "
                       f"[{km}]{rep_s}")

    out.append("\n## Гипотезы для проверки\n")
    out.append("_Отсортированы по оценке потенциального ущерба. Схемы — "
               "гипотезы аудитора, не установленные факты; фактура под ними "
               "(КМ, суммы, системы) — из базы._\n")

    for i, s in enumerate(schemes, 1):
        dmg = s.get("damage_rub")
        dmg_s = _fmt_money(dmg) if dmg else "оценка не задана"
        prob = _PROB_RU.get(s.get("probability", ""), s.get("probability", "—"))
        det = _DET_RU.get(s.get("detectability", ""), s.get("detectability", "—"))
        axis = _AXIS_RU.get(s.get("axis", ""), s.get("axis", ""))
        eff = s.get("effort") or "—"

        out.append(f"\n### {i}. {s.get('title', 'Без названия')}")
        out.append(f"`~{dmg_s}` · вероятность {prob} · обнаружение: {det} "
                   f"· трудозатраты {eff}\n")
        if s.get("actor"):
            out.append(f"**Кто:** {s['actor']}  ")
        out.append(f"**Схема:** {s.get('scheme', '')}\n")
        if s.get("why_possible"):
            out.append(f"**Почему возможно:** {s['why_possible']}\n")

        flags = s.get("red_flags") or []
        if flags:
            out.append("**Красные флаги:**")
            for f in flags:
                out.append(f"- {f}")
            out.append("")
        if s.get("detection_query"):
            where = s.get("where_to_look") or "—"
            out.append(f"**Как проверить:** {s['detection_query']}  ")
            out.append(f"_Где смотреть: {where}_\n")
        if s.get("damage_basis"):
            out.append(f"_Оценка ущерба: {s['damage_basis']}_\n")

        based = s.get("based_on") or {}
        km_refs = (", ".join(based.get("km") or [])
                   or "аналогия по механике, прямого кейса в базе нет")
        out.append(f"<sub>Тип: {axis} · Основание: {km_refs}</sub>")
        if s.get("priority_note"):
            out.append(f"<sub>{s['priority_note']}</sub>")

    return "\n".join(out)


# ──────────────────────────────────────────────────────────────────
# Агент
# ──────────────────────────────────────────────────────────────────

class HypothesisAgent(BaseAgent):
    agent_type = "hypothesis"

    def _supports_clarification(self, query_ctx=None) -> bool:
        if query_ctx and query_ctx.km_numbers:
            return False
        return True

    async def _generate_clarification(
        self,
        query_ctx: QueryContext,
        model: Optional[str] = None,
        history: Optional[List[Dict]] = None,
    ) -> str:
        catalog = _build_topic_catalog()
        history_block = HISTORY_BLOCK_TEMPLATE.format(
            history=self._format_history(history or []),
        )
        user_content = history_block + CLARIFICATION_USER_TEMPLATE.format(
            query=query_ctx.raw_query,
            topic=query_ctx.topic or "(не извлечена)",
            available_topics=catalog,
        )
        messages = [
            {"role": "system", "content": CLARIFICATION_SYSTEM + FOLLOWUPS_INSTRUCTION},
            {"role": "user", "content": user_content},
        ]
        return await generate_async(messages, model=model)

    # ── этапы конвейера ──

    async def _step_mechanics(self, topic: str, devs_text: str, context: str,
                              model: Optional[str]) -> List[Dict]:
        # Контекст режем: на 40 отклонениях модель не укладывалась в лимит,
        # ответ обрывался и JSON не парсился (прод-прогон дал 0 механик)
        raw = await generate_async(
            [{"role": "system", "content": MECHANICS_SYSTEM},
             {"role": "user", "content": MECHANICS_USER_TEMPLATE.format(
                 topic=topic, deviations=devs_text[:9000],
                 context=context[:6000])}],
            model=model, max_tokens=4000, temperature=0.2)
        parsed = _parse_json_block(raw) or {}
        mechanics = parsed.get("mechanics") or []
        logger.info(f"[Hypothesis] Механик извлечено: {len(mechanics)}")
        return mechanics

    async def _step_schemes(self, topic: str, mechanics: List[Dict],
                            covered: str, systems: str,
                            model: Optional[str]) -> List[Dict]:
        raw = await generate_async(
            [{"role": "system", "content": SCHEMES_SYSTEM},
             {"role": "user", "content": SCHEMES_USER_TEMPLATE.format(
                 retail_scope=RETAIL_SCOPE, topic=topic,
                 mechanics=json.dumps(mechanics, ensure_ascii=False, indent=1)[:8000],
                 covered=covered, systems=systems, n_schemes=N_SCHEMES)}],
            model=model, max_tokens=4000, temperature=0.75)
        parsed = _parse_json_block(raw) or {}
        schemes = parsed.get("schemes") or []
        logger.info(f"[Hypothesis] Схем сгенерировано: {len(schemes)}")
        return schemes

    async def _step_scoring(self, schemes: List[Dict], damage_stats: str,
                            model: Optional[str]) -> List[Dict]:
        brief = [{"index": i, "title": s.get("title"), "axis": s.get("axis"),
                  "scheme": (s.get("scheme") or "")[:400],
                  "actor": s.get("actor")}
                 for i, s in enumerate(schemes, 1)]
        raw = await generate_async(
            [{"role": "system", "content": SCORING_SYSTEM},
             {"role": "user", "content": SCORING_USER_TEMPLATE.format(
                 schemes=json.dumps(brief, ensure_ascii=False, indent=1)[:8000],
                 damage_stats=damage_stats)}],
            model=model, max_tokens=2500, temperature=0.1)
        parsed = _parse_json_block(raw) or {}
        scored = parsed.get("scored") or []
        by_idx = {}
        for s in scored:
            try:
                by_idx[int(s.get("index"))] = s
            except (TypeError, ValueError):
                continue
        merged = []
        for i, sch in enumerate(schemes, 1):
            merged.append({**sch, **{k: v for k, v in (by_idx.get(i) or {}).items()
                                     if k != "index"}})
        logger.info(f"[Hypothesis] Оценено схем: {len(by_idx)} из {len(schemes)}")
        return merged

    # ── основной вход ──

    async def _generate(
        self,
        query_ctx: QueryContext,
        chunks: List[Dict],
        model: Optional[str] = None,
        history: Optional[List[Dict]] = None,
    ) -> str:
        context = build_context(chunks)
        topic = query_ctx.topic or query_ctx.raw_query

        # ── ШАГ 0: сбор материала (без LLM) ──
        with get_db() as db:
            km_filter = query_ctx.km_numbers[0] if query_ctx.km_numbers else None
            devs = DeviationRepo.search(db, query=topic[:100], check_id=km_filter)
            devs_data = [_deviation_to_dict(d) for d in devs[:_MAX_DEVS]]
            available_km = DocumentRepo.list_check_ids(db)

        similar_checks = _build_similar_checks(chunks, available_km)
        devs_text = build_deviations_context(devs_data)
        damage_stats = _damage_stats(devs_data)
        systems = _systems_and_processes(devs_data, context)
        covered = _covered_map(devs_data)

        n_km = len({d.get("check_id") for d in devs_data if d.get("check_id")})
        await self._say("collected",
                        f"Поднял базу по теме: {n_km} "
                        f"{'проверка' if n_km == 1 else 'проверок'}, "
                        f"{len(devs_data)} отклонений")

        # ── ШАГИ 1-3: механики → схемы → оценка ──
        try:
            await self._say("mechanics",
                            "Выделяю механики нарушений — почему это стало "
                            "возможно…")
            mechanics = await self._step_mechanics(topic, devs_text, context, model)
            if mechanics:
                names = ", ".join((m.get("name") or "")[:34]
                                  for m in mechanics[:3])
                await self._say("mechanics_done",
                                f"Выделено механик: {len(mechanics)} — {names}…")
            if not mechanics:
                # Этап 1 мог не уложиться в лимит токенов — это не повод терять
                # весь конвейер: схемы генерируются и напрямую из отклонений
                logger.warning("[Hypothesis] Механики не извлечены — "
                               "генерирую схемы из отклонений напрямую")
                mechanics = [{"id": 0, "name": "(механики не выделены)",
                              "essence": devs_text[:1500], "seen_in_km": []}]

            await self._say("schemes",
                            "Генерирую схемы злоупотреблений: перенос, "
                            "комбинация, инсайдер, контрагент, слепая зона…")
            schemes = await self._step_schemes(topic, mechanics, covered,
                                               systems, model)
            if not schemes:
                raise ValueError("схемы не сгенерированы")
            await self._say("schemes_done",
                            f"Сгенерировано гипотез: {len(schemes)}")

            await self._say("scoring",
                            "Оцениваю потенциальный ущерб и приоритеты…")
            schemes = await self._step_scoring(schemes, damage_stats, model)
            # Ссылки на КМ — только реально существующие в корпусе
            dropped = _verify_km_refs(schemes, available_km)
            if dropped:
                await self._say("verified",
                                f"Проверил ссылки на КМ: отброшено "
                                f"неподтверждённых — {dropped}")

            # Сортировка по потенциальному ущербу (главное требование)
            def _dmg(s) -> float:
                try:
                    return float(s.get("damage_rub") or 0)
                except (TypeError, ValueError):
                    return 0.0
            schemes.sort(key=_dmg, reverse=True)

            return _render(topic, similar_checks, schemes, mechanics, devs_data)

        except Exception as e:
            # Конвейер не должен оставлять аудитора без ответа
            logger.warning(f"[Hypothesis] Конвейер упал ({e}) — одношаговый режим")
            history_block = HISTORY_BLOCK_TEMPLATE.format(
                history=self._format_history(history or []),
            )
            user_content = history_block + HYPOTHESIS_USER_TEMPLATE.format(
                query=query_ctx.raw_query,
                topic=topic,
                similar_checks=similar_checks,
                context=context,
                deviations=devs_text,
            )
            return await generate_async(
                [{"role": "system", "content": HYPOTHESIS_SYSTEM + FOLLOWUPS_INSTRUCTION},
                 {"role": "user", "content": user_content}],
                model=model)
