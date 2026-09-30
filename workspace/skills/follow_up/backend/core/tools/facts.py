"""Follow Up 2.0 — сущности и структурные запросы.

Два вопроса, на которые поиск по фрагментам не отвечает в принципе:

**«В каких актах встречался Иванов И.И.»** — это вопрос про ОХВАТ по сущности,
а не про релевантные абзацы. Нужен обратный индекс «упоминание → документы», и
нужна честная оговорка: полнота такого ответа ограничена не размером корпуса, а
тем, сколько упоминаний вообще сумел распознать экстрактор. Поэтому у каждого
экстрактора есть `measured_recall` — и он печатается вместе с ответом. Сказать
«найдено в 7 актах», умолчав, что распознаётся три четверти написаний, значит
соврать про полноту.

**«Сколько нарушений дороже 10 млн и по каким системам»** — это запрос к полям,
а не к тексту. Поля есть: `financial_impact_rub`, `affected_systems`,
`responsible_unit`, `regulation_refs`, `severity`. Доступа к ним из диалога не
было: единственный путь — `ILIKE '%тема%'` по описанию с лимитом 50, который на
многословной теме почти всегда пуст.

Отдельно важное: у структурного ответа обязателен **`field_fill`** — доля строк,
где запрошенное поле вообще заполнено. «Нарушений дороже 10 млн: три» при
заполненности поля в 9 % — это не ответ, а ловушка.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Dict, List, Optional

from backend.core.evidence import (Coverage, DOC, Evidence, Provenance, ROW,
                                   ToolResult)
from backend.core.pools import PRIO_INTERACTIVE, CancelToken, run_cpu
from backend.core.tools.registry import ToolSpec, register

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────
# Экстракторы сущностей
# ──────────────────────────────────────────────────────────────────

# Каждый экстрактор ОБЯЗАН нести measured_recall: доля написаний, которые он
# распознаёт на размеченной выборке. Без этого числа строка полноты по
# сущностям будет врать, а проверить её будет нечем.
EXTRACTORS: Dict[str, Dict] = {
    "person": {
        # «Иванов И.И.», «Иванов И. И.», «И.И. Иванов»
        "re": re.compile(
            r"(?:(?<=\s)|^)("
            r"[А-ЯЁ][а-яё]{2,}\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.|"
            r"[А-ЯЁ]\.\s?[А-ЯЁ]\.\s?[А-ЯЁ][а-яё]{2,}"
            r")"),
        "label": "ФИО",
        # Не распознаёт: полное «Иванов Иван Иванович», склонения («Иванову»),
        # инициалы без точек. Замерено на выборке 100 упоминаний из актов.
        "measured_recall": 0.72,
    },
    "system": {
        # «АС Пример», «АБС БИСквит», «ЕПС», латиница в кавычках
        "re": re.compile(
            r"(?:АС|АБС|ИС|ПО|СУБД)\s+[«\"]?([А-ЯЁA-Z][\w\-\s]{2,30}?)[»\"]?(?=[\s,.;)]|$)"),
        "label": "система",
        "measured_recall": 0.65,
    },
    "regulation": {
        # «716-П», «152-ФЗ», «п. 4.2 Положения»
        "re": re.compile(r"\b(\d{2,4}-[ПФ]З?|\d{2,4}-П)\b"),
        "label": "норматив",
        "measured_recall": 0.88,
    },
    "process": {
        "re": re.compile(r"\b(П\d{3,4})\b"),
        "label": "процесс",
        "measured_recall": 0.95,
    },
}


def extract_entities(text: str) -> List[Dict]:
    """Все распознанные сущности фрагмента с позициями."""
    out: List[Dict] = []
    for kind, ex in EXTRACTORS.items():
        for m in ex["re"].finditer(text or ""):
            value = (m.group(1) or "").strip()
            if len(value) < 2:
                continue
            out.append({"kind": kind, "value": value,
                        "norm": normalize_entity(value),
                        "start": m.start(1), "end": m.end(1)})
    return out


_SPACES = re.compile(r"\s+")
# Экстрактор систем забирает НАЗВАНИЕ без класса: «АС Пример» → «Пример».
# Аудитор же пишет с классом. Без снятия префикса запрос не находил бы ничего,
# и это выглядело бы как «в корпусе нет», а не как расхождение форм.
_SYS_PREFIX = re.compile(r"^(?:ас|абс|ис|по|субд)\s+", re.IGNORECASE)


def normalize_entity(value: str) -> str:
    """«Иванов И. И.» и «Иванов И.И.» — одно и то же упоминание."""
    v = _SPACES.sub(" ", (value or "").strip())
    v = v.replace(". ", ".").replace("«", "").replace("»", "").replace('"', "")
    return v.lower()


def entity_variants(value: str) -> List[str]:
    """Написания, под которыми одна и та же сущность может лежать в индексе."""
    norm = normalize_entity(value)
    out = [norm]
    stripped = _SYS_PREFIX.sub("", norm).strip()
    if stripped and stripped != norm:
        out.append(stripped)
    return out


def recall_note(kinds: List[str]) -> str:
    """Оговорка о полноте: чем ограничен ответ по сущностям."""
    if not kinds:
        return ""
    worst = min(EXTRACTORS[k]["measured_recall"] for k in kinds
                if k in EXTRACTORS)
    return (f"распознавание упоминаний неполное: на размеченной выборке "
            f"находится {worst:.0%} написаний, поэтому список может быть "
            f"короче фактического")


# ──────────────────────────────────────────────────────────────────
# Поиск по сущности
# ──────────────────────────────────────────────────────────────────

async def search_entity(text: str, kind: Optional[str] = None,
                        cancel: Optional[CancelToken] = None) -> ToolResult:
    """В каких актах встречается эта сущность — с цитатами и оговоркой о полноте.

    Использует обратный индекс `entity_mentions`, если он построен, иначе
    сканирует чанки. Сканирование — не «запасной путь на всякий случай»: на
    214 актах прод-корпуса это секунды единственного процессора на КАЖДЫЙ
    вопрос, поэтому в ответе прямо сказано, что индекс не построен.
    """
    targets = entity_variants(text)
    if not targets or not targets[0]:
        return ToolResult(tool="search_entity", status="empty",
                          error="пустой запрос")

    def _from_index() -> Optional[Dict]:
        """Быстрый путь: обратный индекс упоминаний."""
        from sqlalchemy import func
        from backend.storage.database import Document, EntityMention, get_db
        with get_db() as db:
            if db.query(EntityMention).limit(1).count() == 0:
                return None
            # Аудитор пишет «Иванов», а в индексе лежит «иванов а.п.»:
            # точное сравнение не находило НИЧЕГО, критик считал ход пустым и
            # добирал охватным поиском — тот на одной фамилии даёт десятки
            # случайных актов с пустыми цитатами. Поэтому: сначала точное
            # совпадение, а если его нет — по началу строки.
            q = db.query(EntityMention).filter(EntityMention.norm.in_(targets))
            if kind:
                q = q.filter(EntityMention.kind == kind)
            rows = q.limit(2000).all()
            if not rows:
                from sqlalchemy import or_
                like = [EntityMention.norm.like(f"{t}%") for t in targets
                        if len(t) >= 3]
                if like:
                    q2 = db.query(EntityMention).filter(or_(*like))
                    if kind:
                        q2 = q2.filter(EntityMention.kind == kind)
                    rows = q2.limit(2000).all()
            docs = db.query(func.count(func.distinct(Document.check_id))).scalar()
        hits: Dict[str, List[Dict]] = {}
        for m in rows:
            hits.setdefault(m.check_id, []).append({
                "check_id": m.check_id, "chunk_index": m.chunk_index,
                "header_path": m.header_path or "", "quote": m.quote or "",
                "where": m.where or "case_text"})
        return {"hits": hits, "docs": int(docs or 0), "source": "index"}

    def _work() -> Dict:
        indexed = _from_index()
        if indexed is not None:
            return indexed
        from backend.storage.database import Chunk, Document, get_db
        hits: Dict[str, List[Dict]] = {}
        scanned = 0
        with get_db() as db:
            rows = (db.query(Document.check_id, Chunk.chunk_index,
                             Chunk.text, Chunk.header_path)
                    .join(Chunk, Chunk.document_id == Document.id).all())
        docs = set()
        for check_id, idx, body, header in rows:
            scanned += 1
            docs.add(check_id)
            low = (body or "").lower()
            pos = next((low.index(t) for t in targets if t in low), None)
            if pos is None:
                # Дословного вхождения нет — пробуем распознанные сущности:
                # «Иванов И. И.» найдётся по нормализованной форме
                found = [e for e in extract_entities(body or "")
                         if e["norm"] in targets
                         and (kind is None or e["kind"] == kind)]
                if not found:
                    continue
                pos = found[0]["start"]
            window = (body or "")[max(0, pos - 200): pos + 250].strip()
            hits.setdefault(check_id, []).append({
                "check_id": check_id, "chunk_index": idx,
                "header_path": header or "", "quote": window,
                # Реквизиты акта (подписи, состав группы) — не то же самое,
                # что упоминание в описанном кейсе. Аудитору важно различать.
                "where": "requisites" if _looks_like_requisites(header, window)
                         else "case_text"})
        return {"hits": hits, "scanned_chunks": scanned, "docs": len(docs),
                "source": "scan"}

    got = await run_cpu(_work, prio=PRIO_INTERACTIVE, cancel=cancel,
                        stage="search_entity")
    hits: Dict[str, List[Dict]] = got["hits"]

    ev: List[Evidence] = []
    for check_id, items in sorted(hits.items()):
        # Одна сущность встречается во фрагменте по нескольку раз, и окно
        # вокруг них совпадает: показывать одну и ту же цитату дважды — шум
        seen_chunks: set = set()
        uniq = [it for it in items
                if not (it["chunk_index"] in seen_chunks
                        or seen_chunks.add(it["chunk_index"]))]
        for it in uniq[:3]:
            ev.append(Evidence(
                kind="entity_mention", check_id=check_id,
                quote=it["quote"], header_path=it["header_path"],
                chunk_uid=f"{check_id}:{it['chunk_index']}:ent",
                where=it["where"], score=1.0,
                provenance=Provenance("exact", True),
                fields={"entity": text}))

    kinds = [kind] if kind else list(EXTRACTORS)
    # Отсутствие индекса — не деталь реализации: без него каждый такой вопрос
    # стоит полного скана корпуса, и знать об этом надо до пилота
    degraded = [] if got.get("source") == "index" else ["entity_index"]
    return ToolResult(
        evidence=ev, tool="search_entity",
        status="ok" if ev else "empty",
        degraded_sources=degraded,
        coverage=Coverage(unit="docs", scanned=got["docs"],
                          matched=len(hits), returned=len(hits),
                          field_fill={"entity_recall":
                                      min(EXTRACTORS[k]["measured_recall"]
                                          for k in kinds if k in EXTRACTORS)}),
    )


_REQ_MARKERS = ("подпис", "состав групп", "руководител", "исполнител",
                "утвержда", "согласова", "реквизит")


def _looks_like_requisites(header: Optional[str], text: str) -> bool:
    blob = f"{header or ''} {text[:200]}".lower()
    return any(m in blob for m in _REQ_MARKERS)


# ──────────────────────────────────────────────────────────────────
# Структурный запрос к отклонениям
# ──────────────────────────────────────────────────────────────────

_ALLOWED_FACETS = {
    "category": "category", "severity": "severity",
    "responsible_unit": "responsible_unit", "check_id": "check_id",
}


async def query_deviations(check_id: Optional[str] = None,
                           min_rub: Optional[float] = None,
                           category: Optional[str] = None,
                           severity: Optional[str] = None,
                           system: Optional[str] = None,
                           unit: Optional[str] = None,
                           group_by: Optional[str] = None,
                           limit: int = 50,
                           cancel: Optional[CancelToken] = None) -> ToolResult:
    """Отклонения по ПОЛЯМ, а не по тексту.

    Обязательный `field_fill`: «нарушений дороже 10 млн — три» при
    заполненности суммы в 9 % это не ответ, а ловушка. Число рядом с ответом
    делает его проверяемым.
    """
    def _work() -> Dict:
        from backend.core import identity
        from backend.storage.database import (Deviation, _check_id_filter,
                                              get_db)
        with get_db() as db:
            q = db.query(Deviation)
            if check_id:
                # Вопрос «какие кейсы/отклонения в акте КМ-…» отвечается ИМЕННО
                # отсюда: отклонения уже извлечены при индексации, и это готовый
                # перечень случаев. Раньше фильтра по проверке не было вовсе, и
                # такой вопрос уходил в чтение текста акта, где перечня нет —
                # аудитор получал титульный лист и таблицу тарифов.
                canon = identity.format(check_id) or check_id
                q = q.filter(_check_id_filter(Deviation.check_id, canon))
            total = q.count()
            filled = q.filter(Deviation.financial_impact_rub.isnot(None)).count()
            if min_rub is not None:
                q = q.filter(Deviation.financial_impact_rub >= float(min_rub))
            if category:
                q = q.filter(Deviation.category.ilike(f"%{category}%"))
            if severity:
                q = q.filter(Deviation.severity.ilike(f"%{severity}%"))
            if unit:
                q = q.filter(Deviation.responsible_unit.ilike(f"%{unit}%"))
            rows = q.limit(max(1, min(limit, 500))).all()
            items = [{
                "check_id": d.check_id, "category": d.category,
                "severity": d.severity, "description": d.description or "",
                "financial_impact_rub": d.financial_impact_rub,
                "affected_systems": _as_list(d.affected_systems),
                "responsible_unit": d.responsible_unit,
                "regulation_refs": _as_list(d.regulation_refs),
            } for d in rows]
        return {"items": items, "total": total, "money_filled": filled}

    got = await run_cpu(_work, prio=PRIO_INTERACTIVE, cancel=cancel,
                        stage="query_deviations")
    items = got["items"]

    if system:
        s = system.lower()
        items = [i for i in items
                 if any(s in str(x).lower() for x in i["affected_systems"])]

    ev = [Evidence(
        kind="deviation", check_id=i["check_id"],
        quote=i["description"][:600],
        chunk_uid=f"{i['check_id']}:dev:{abs(hash(i['description'])) % 99999}",
        fields={k: i[k] for k in ("category", "severity",
                                 "financial_impact_rub", "affected_systems",
                                 "responsible_unit", "regulation_refs")},
        score=float(i["financial_impact_rub"] or 0),
        provenance=Provenance("exact", True)) for i in items]

    total = got["total"] or 1
    return ToolResult(
        evidence=ev, tool="query_deviations",
        status="ok" if ev else "empty",
        coverage=Coverage(
            unit="rows", scanned=got["total"], matched=len(items),
            returned=len(ev), truncated=len(items) >= limit,
            # Заполненность — обязательная часть ответа, а не украшение
            field_fill={"financial_impact_rub":
                        round(got["money_filled"] / total, 2)}),
    )


def _as_list(raw) -> List:
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else [v]
    except (ValueError, TypeError):
        return [raw]


# ──────────────────────────────────────────────────────────────────
# Профиль корпуса
# ──────────────────────────────────────────────────────────────────

async def corpus_profile(cancel: Optional[CancelToken] = None) -> ToolResult:
    """Что вообще есть в корпусе — чтобы «не нашёл» отличалось от «нет такого»."""
    def _work() -> Dict:
        from backend.storage.database import (ChunkRepo, DeviationRepo,
                                              DocumentRepo, get_db)
        with get_db() as db:
            return {
                "checks": len(DocumentRepo.list_check_ids(db)),
                "documents": DocumentRepo.count(db),
                "chunks": ChunkRepo.count(db),
                "deviations": DeviationRepo.count(db),
                "categories": [s["category"] for s in
                               DeviationRepo.get_categories_stats(db)[:15]],
            }

    got = await run_cpu(_work, prio=PRIO_INTERACTIVE, cancel=cancel,
                        stage="corpus_profile")
    ev = [Evidence(kind="fact_row", check_id="", quote=json.dumps(
        got, ensure_ascii=False), chunk_uid="corpus:profile", fields=got)]
    return ToolResult(evidence=ev, tool="corpus_profile", status="ok",
                      coverage=Coverage(unit="docs", scanned=got["checks"],
                                        matched=got["checks"], returned=1))


# ──────────────────────────────────────────────────────────────────
# Регистрация
# ──────────────────────────────────────────────────────────────────

register(ToolSpec(
    name="search_entity",
    human_label="Поиск по упоминанию",
    description="в каких актах встречается конкретный человек, система или "
                "норматив; отмечает, было упоминание в описанном кейсе или в "
                "реквизитах акта",
    args_schema={"text": {"type": "str", "required": True,
                          "desc": "ФИО, название системы, номер норматива"},
                 "kind": {"type": "str",
                          "desc": "person | system | regulation | process"}},
    produces=DOC, corpus_wide=True, role="body",
    cost_class="cpu", latency_hint_ms=1500,
    when_not="ищется тема, а не конкретное упоминание — там search_coverage",
    examples=("в каких актах встречался Иванов И.И.",
              "где упоминается АС Пример"),
), search_entity)

register(ToolSpec(
    name="query_deviations",
    human_label="Отклонения по полям",
    description="перечень ОТКЛОНЕНИЙ (кейсов, нарушений) — по конкретной "
                "проверке через check_id либо по структурным признакам: сумма "
                "ущерба, критичность, категория, система, подразделение. "
                "Всегда возвращает заполненность поля, по которому фильтровали",
    args_schema={"check_id": {"type": "str",
                              "desc": "номер проверки — для вопросов «какие "
                                      "кейсы / отклонения / нарушения в акте»"},
                 "min_rub": {"type": "float", "desc": "ущерб не меньше"},
                 "category": {"type": "str"}, "severity": {"type": "str"},
                 "system": {"type": "str"}, "unit": {"type": "str"},
                 "limit": {"type": "int"}},
    produces=ROW, corpus_wide=True, role="body",
    cost_class="cpu", latency_hint_ms=400,
    when_not="вопрос про методику, объём выборки или период проверки — это "
             "текст акта, там read_document",
    examples=("какие кейсы были в акте КМ-99-12348",
              "расскажи про суть отклонений в этом акте",
              "сколько нарушений дороже 10 млн",
              "какие критичные отклонения по АБС"),
), query_deviations)

register(ToolSpec(
    name="corpus_profile",
    human_label="Профиль корпуса",
    description="что вообще есть в базе: сколько проверок, актов, отклонений, "
                "какие категории — чтобы отличить «не нашёл» от «такого нет»",
    args_schema={},
    produces=ROW, corpus_wide=True, role="enrich",
    cost_class="cpu", latency_hint_ms=200,
    when_not="вопрос по конкретной теме или проверке",
    examples=("что у нас вообще есть в базе",),
), corpus_profile)


# ──────────────────────────────────────────────────────────────────
# Семантика по отклонениям
# ──────────────────────────────────────────────────────────────────

async def semantic_deviations(query: str, top_n: int = 25,
                              min_score: Optional[float] = None,
                              cancel: Optional[CancelToken] = None) -> ToolResult:
    """Отклонения по СМЫСЛУ темы, а не по дословному совпадению.

    `ILIKE '%кредитные карты%'` требует буквального вхождения, а в акте
    написано «операции по картам для детей» — и фасетный ответ по многословной
    теме оказывается пустым. Здесь сравниваются эмбеддинги описаний.

    Если эмбеддинги не построены, инструмент НЕ делает вид, что всё в порядке:
    он честно деградирует к лексическому поиску и говорит об этом.
    """
    import numpy as np

    thr = min_score if min_score is not None else _cfg_dev_min_score()

    def _work() -> Dict:
        from backend.indexing.derived import load_deviation_embeddings
        from backend.indexing.embedder import embed_texts
        from backend.storage.database import Deviation, get_db

        store = load_deviation_embeddings()
        if store is None:
            # Деградация: лексика по стеммированным термам — лучше, чем ILIKE,
            # но полноты семантики не даёт
            from backend.indexing.lexicon import content_terms
            terms = content_terms(query)
            with get_db() as db:
                rows = db.query(Deviation).limit(3000).all()
                out = []
                for d in rows:
                    body = content_terms(d.description or "")
                    hit = len(set(terms) & set(body))
                    if hit:
                        out.append((hit / max(1, len(terms)), _dev_dict(d)))
            out.sort(key=lambda x: -x[0])
            return {"items": out[:top_n], "total": len(rows),
                    "degraded": ["deviation_embeddings"]}

        q = embed_texts([query], normalize=True)[0]
        sims = store["emb"] @ q
        order = np.argsort(sims)[::-1][:top_n * 3]
        keep = [(float(sims[i]), int(store["ids"][i])) for i in order
                if sims[i] >= thr][:top_n]
        if not keep:
            return {"items": [], "total": int(len(store["ids"])), "degraded": []}
        ids = [i for _, i in keep]
        with get_db() as db:
            found = {d.id: d for d in
                     db.query(Deviation).filter(Deviation.id.in_(ids)).all()}
        items = [(sc, _dev_dict(found[i])) for sc, i in keep if i in found]
        return {"items": items, "total": int(len(store["ids"])), "degraded": []}

    got = await run_cpu(_work, prio=PRIO_INTERACTIVE, cancel=cancel,
                        stage="semantic_deviations")
    items = got["items"]
    ev = [Evidence(
        kind="deviation", check_id=d["check_id"], quote=d["description"][:600],
        chunk_uid=f"{d['check_id']}:dev:{d['id']}", score=float(sc),
        fields={k: d[k] for k in ("category", "severity",
                                  "financial_impact_rub", "affected_systems",
                                  "responsible_unit")},
        provenance=Provenance("semantic", True)) for sc, d in items]

    return ToolResult(
        evidence=ev, tool="semantic_deviations",
        status="ok" if ev else "empty",
        degraded_sources=got["degraded"],
        coverage=Coverage(unit="rows", scanned=got["total"],
                          matched=len(items), returned=len(ev),
                          truncated=len(items) >= top_n))


def _cfg_dev_min_score() -> float:
    from backend.config import get_settings
    return float(get_settings().deviation_sim_min_score)


def _dev_dict(d) -> Dict:
    return {"id": d.id, "check_id": d.check_id, "category": d.category,
            "severity": d.severity, "description": d.description or "",
            "financial_impact_rub": d.financial_impact_rub,
            "affected_systems": _as_list(d.affected_systems),
            "responsible_unit": d.responsible_unit}


# ──────────────────────────────────────────────────────────────────
# Сравнение проверок
# ──────────────────────────────────────────────────────────────────

async def compare_checks(check_ids: List[str],
                         cancel: Optional[CancelToken] = None) -> ToolResult:
    """Сопоставление НЕСКОЛЬКИХ проверок — единственный случай, где это законно.

    Каждая КМ отдельная, и смешивать их артефакты нельзя. Но аудитор иногда
    прямо просит сравнить: «что общего у КМ-99-12345 и КМ-99-12346». Здесь
    строки не сливаются — у каждой свой check_id, и общее показано как
    пересечение, а не как единый список.
    """
    ids = [i for i in (check_ids or []) if i]
    if len(ids) < 2:
        return ToolResult(tool="compare_checks", status="empty",
                          error="нужно минимум две проверки")

    def _work() -> Dict:
        from backend.core import identity
        from backend.storage.database import DeviationRepo, get_db
        per: Dict[str, Dict] = {}
        with get_db() as db:
            for raw in ids:
                km = identity.normalize(raw) or raw
                devs = DeviationRepo.get_by_check_id(db, km)
                per[km] = {
                    "count": len(devs),
                    "categories": sorted({d.category for d in devs if d.category}),
                    "severities": sorted({d.severity for d in devs if d.severity}),
                    "systems": sorted({s for d in devs
                                       for s in _as_list(d.affected_systems)}),
                    "money": sum(float(d.financial_impact_rub or 0) for d in devs),
                }
        return per

    per = await run_cpu(_work, prio=PRIO_INTERACTIVE, cancel=cancel,
                        stage="compare_checks")
    cat_sets = [set(v["categories"]) for v in per.values()]
    sys_sets = [set(v["systems"]) for v in per.values()]
    common = {
        "categories": sorted(set.intersection(*cat_sets)) if cat_sets else [],
        "systems": sorted(set.intersection(*sys_sets)) if sys_sets else [],
    }

    ev = [Evidence(
        kind="fact_row", check_id=km,
        quote=(f"отклонений {v['count']}, категории: "
               f"{', '.join(v['categories']) or '—'}"),
        chunk_uid=f"{km}:compare", fields=v,
        provenance=Provenance("exact", True)) for km, v in per.items()]
    ev.append(Evidence(kind="fact_row", check_id="",
                       quote=f"общее: {common}", chunk_uid="compare:common",
                       fields=common, provenance=Provenance("exact", True)))

    return ToolResult(evidence=ev, tool="compare_checks", status="ok",
                      coverage=Coverage(unit="docs", scanned=len(per),
                                        matched=len(per), returned=len(per)))


register(ToolSpec(
    name="semantic_deviations",
    human_label="Отклонения по смыслу",
    description="отклонения, близкие по СМЫСЛУ к теме, а не по дословному "
                "совпадению слов",
    args_schema={"query": {"type": "str", "required": True},
                 "top_n": {"type": "int"}},
    produces=ROW, corpus_wide=True, role="scope",
    cost_class="cpu", latency_hint_ms=600,
    when_not="нужен фильтр по точному полю — там query_deviations",
    examples=("что было по теме кредитных карт",),
), semantic_deviations)

register(ToolSpec(
    name="compare_checks",
    human_label="Сравнение проверок",
    description="сопоставление нескольких проверок: что общего и чем "
                "различаются. Строки НЕ сливаются — у каждой своя проверка",
    args_schema={"check_ids": {"type": "list[str]", "required": True,
                               "desc": "номера проверок через запятую"}},
    produces=ROW, corpus_wide=False, role="body",
    cost_class="cpu", latency_hint_ms=400,
    when_not="проверка одна",
    examples=("что общего у КМ-99-12345 и КМ-99-12346",),
), compare_checks)
