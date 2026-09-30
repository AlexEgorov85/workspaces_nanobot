"""Follow Up 2.0 — режимы поиска под форму вопроса.

Сегодня режим один: `retrieve(top_k=15) → rerank(top_n=5)`. Это форма «дай пять
самых релевантных абзацев». Она хорошо отвечает на «что было по теме X» и
СТРУКТУРНО не способна ответить на вопросы, ради которых писался этот модуль:

| Вопрос аудитора | Нужно | Даёт top-5 чанков |
|---|---|---|
| «в каких актах встречался Иванов» | охват по документам | 5 чанков из 2-3 актов |
| «все акты, где поднимался вопрос лимитов» | группировка + полнота | то же |
| «выведи кейс из акта КМ-…» | весь акт + место внутри | первые 25 000 символов |

Три инструмента, три формы ответа:

- `search_passages` — точечный, как сегодня. Полноту по корпусу НЕ обещает;
- `search_coverage` — охватный: полный проход, группировка по документам,
  один лучший фрагмент на акт, честная строка полноты;
- `read_document` — весь акт, но с поиском МЕСТА внутри него: окно вокруг
  совпадения, а не начало файла.

Бюджет реранкера — не украшение. Замерено 13 пар/с: реранк 150 чанков акта это
11.6 с на единственном процессоре. Поэтому кросс-энкодер видит не всё, а
финальный отбор: кандидаты сначала отсеиваются дешёвым скорингом.
"""
from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional

from backend.core.evidence import (Coverage, DOC, Evidence, PASSAGE,
                                   Provenance, ToolResult)
from backend.core.pools import PRIO_INTERACTIVE, CancelToken, run_cpu
from backend.core.tools.registry import ToolSpec, register

logger = logging.getLogger(__name__)


def _cfg():
    from backend.config import get_settings
    return get_settings()


def _to_evidence(ch: Dict, how: str = "semantic",
                 score: float = 0.0) -> Evidence:
    return Evidence(
        kind="passage",
        check_id=ch.get("check_id") or "",
        quote=(ch.get("text") or "").strip(),
        header_path=ch.get("header_path") or "",
        chunk_uid=f"{ch.get('check_id')}:{ch.get('chunk_index')}",
        score=float(score or ch.get("score") or 0.0),
        provenance=Provenance(how, True),
    )


# ──────────────────────────────────────────────────────────────────
# 1. Точечный поиск — сегодняшнее поведение, без обещаний полноты
# ──────────────────────────────────────────────────────────────────

async def search_passages(query: str, km: Optional[List[str]] = None,
                          top_k: Optional[int] = None,
                          cancel: Optional[CancelToken] = None) -> ToolResult:
    from backend.rag.reranker import rerank
    from backend.rag.retrieval import retrieve

    k = top_k or _cfg().top_k

    def _work() -> List[Dict]:
        found = retrieve(query=query, km_filter=km or None, mode="hybrid",
                         top_k=k)
        return rerank(query, found) if found else []

    chunks = await run_cpu(_work, prio=PRIO_INTERACTIVE, cancel=cancel,
                           stage="search_passages")
    ev = [_to_evidence(c) for c in chunks]
    return ToolResult(
        evidence=ev, tool="search_passages",
        status="ok" if ev else "empty",
        # scanned здесь — это НЕ размер корпуса: точечный режим смотрит top-K и
        # полноту не обещает. Врать про «просмотрен корпус» тут нельзя.
        coverage=Coverage(unit="chunks", scanned=len(chunks),
                          matched=len(chunks), returned=len(ev)),
    )


# ──────────────────────────────────────────────────────────────────
# 2. Охватный поиск — «во всех актах, где…»
# ──────────────────────────────────────────────────────────────────

async def search_coverage(query: str, min_score: Optional[float] = None,
                          max_docs: Optional[int] = None,
                          cancel: Optional[CancelToken] = None) -> ToolResult:
    """Полный проход по корпусу, группировка по документам, честная полнота.

    Не «пять лучших абзацев», а «вот все акты, где это встречается, и вот
    сколько их из скольких». Реранк применяется к ОДНОМУ лучшему фрагменту на
    акт и не более чем к `coverage_max_docs` парам — иначе на 200 актах
    кросс-энкодер съел бы полминуты единственного процессора.
    """
    import numpy as np

    cfg = _cfg()
    limit = max_docs or cfg.coverage_max_docs
    threshold = min_score if min_score is not None else cfg.coverage_min_score

    def _work() -> Dict:
        import faiss
        from backend.indexing.embedder import embed_texts
        from backend.indexing.index_builder import load_bm25, load_faiss
        from backend.indexing.lexicon import tokenize
        from backend.rag.retrieval import _enrich_from_db

        degraded: List[str] = []
        sem_ids: List[int] = []
        total_vectors = 0
        try:
            index = load_faiss()
            total_vectors = index.ntotal
            q = embed_texts([query], normalize=True)
            faiss.normalize_L2(q)
            # Полный проход дешёв: 24 596 векторов по 1024 — 50-200 мс
            scores, ids = index.search(q, min(total_vectors, limit * 20))
            sem_ids = [(int(i), float(s)) for i, s in zip(ids[0], scores[0])
                       if i >= 0 and s >= threshold]
        except Exception as e:
            logger.warning(f"[coverage] Семантика недоступна: {e}")
            degraded.append("faiss")

        kw_ids: List[tuple] = []
        try:
            data = load_bm25()
            bm = data["bm25"]
            all_ids = data.get("ids") or list(range(len(data["corpus"])))
            sc = bm.get_scores(tokenize(query))
            order = np.argsort(sc)[::-1][:limit * 20]
            mx = float(sc[order[0]]) if len(order) and sc[order[0]] > 0 else 0.0
            kw_ids = [(int(all_ids[i]), float(sc[i]) / mx) for i in order
                      if sc[i] > 0 and mx > 0]
        except Exception as e:
            logger.warning(f"[coverage] BM25 недоступен: {e}")
            degraded.append("bm25")

        best: Dict[int, float] = {}
        for fid, sc_ in list(sem_ids) + list(kw_ids):
            best[fid] = max(best.get(fid, 0.0), sc_)
        if not best:
            return {"docs": {}, "scanned": total_vectors, "degraded": degraded}

        rows = _enrich_from_db(sorted(best, key=lambda i: -best[i])[:limit * 6])
        by_doc: Dict[str, Dict] = {}
        for r in rows:
            cid = r.get("check_id") or ""
            if not cid:
                continue
            sc_ = best.get(r.get("faiss_id", -1), 0.0)
            cur = by_doc.get(cid)
            if cur is None or sc_ > cur["score"]:
                by_doc[cid] = {"chunk": r, "score": sc_}
        return {"docs": by_doc, "scanned": total_vectors, "degraded": degraded}

    got = await run_cpu(_work, prio=PRIO_INTERACTIVE, cancel=cancel,
                        stage="search_coverage")
    by_doc: Dict[str, Dict] = got["docs"]
    matched = len(by_doc)

    top = sorted(by_doc.items(), key=lambda kv: -kv[1]["score"])[:limit]

    # Реранк ТОЛЬКО финального среза: один фрагмент на акт, не более limit пар
    if top:
        def _rr() -> List:
            from backend.rag.reranker import rerank
            pairs = [v["chunk"] for _, v in top]
            return rerank(query, pairs, top_n=len(pairs))
        try:
            reranked = await run_cpu(_rr, prio=PRIO_INTERACTIVE, cancel=cancel,
                                     stage="coverage_rerank")
            order = {f"{c.get('check_id')}:{c.get('chunk_index')}": i
                     for i, c in enumerate(reranked)}
            top.sort(key=lambda kv: order.get(
                f"{kv[0]}:{kv[1]['chunk'].get('chunk_index')}", 999))
        except Exception as e:
            logger.warning(f"[coverage] Реранк среза не удался: {e}")

    ev = [_to_evidence(v["chunk"], "semantic", v["score"]) for _, v in top]
    total_docs = _corpus_doc_count()
    return ToolResult(
        evidence=ev, tool="search_coverage",
        status="ok" if ev else "empty",
        degraded_sources=got["degraded"],
        coverage=Coverage(unit="docs", scanned=total_docs or matched,
                          matched=matched, returned=len(ev),
                          truncated=matched > len(ev)),
    )


def _corpus_doc_count() -> int:
    try:
        from backend.storage.database import DocumentRepo, get_db
        with get_db() as db:
            return len(DocumentRepo.list_check_ids(db))
    except Exception:
        return 0


# ──────────────────────────────────────────────────────────────────
# 3. Чтение акта — место внутри, а не начало файла
# ──────────────────────────────────────────────────────────────────

async def read_document(check_id: str, query: Optional[str] = None,
                        section: Optional[str] = None,
                        top_n: Optional[int] = None,
                        cancel: Optional[CancelToken] = None) -> ToolResult:
    """Фрагменты ОДНОГО акта, отобранные под вопрос.

    Прежний drill-down брал первые 25 000 символов: для акта на 40-80 страниц
    вопрос про конкретный кейс упирался в обрезку и получал «в акте это не
    указано», хотя указано было на тридцатой странице.

    Отбор двухступенчатый и это не оптимизация, а необходимость: реранк 150
    чанков при замеренных 13 парах/с — 11.6 с. Сначала дешёвый скоринг по уже
    лежащим в индексе векторам, потом кросс-энкодер на `rerank_batch_pairs`
    финалистах.
    """
    cfg = _cfg()
    n = top_n or cfg.read_document_top_n

    def _work() -> Dict:
        from backend.storage.database import ChunkRepo, DocumentRepo, get_db
        with get_db() as db:
            doc = DocumentRepo.get_canonical(db, check_id)
            if doc is None:
                return {"doc": None, "chunks": []}
            chunks = ChunkRepo.get_by_check_id(db, check_id, canonical_only=True)
            rows = [{"check_id": doc.check_id, "filename": doc.filename,
                     "chunk_index": c.chunk_index, "text": c.text or "",
                     "header_path": c.header_path or "",
                     "faiss_id": c.faiss_id} for c in chunks]
        return {"doc": {"check_id": doc.check_id, "filename": doc.filename},
                "chunks": rows}

    got = await run_cpu(_work, prio=PRIO_INTERACTIVE, cancel=cancel,
                        stage="read_document")
    doc, rows = got["doc"], got["chunks"]
    if doc is None:
        return ToolResult(tool="read_document", status="empty",
                          error=f"{check_id} нет в корпусе")

    if section:
        rows = [r for r in rows
                if section.lower() in (r["header_path"] or "").lower()] or rows

    if not query:
        picked = rows[:n]
        return ToolResult(
            evidence=[_to_evidence(r, "exact") for r in picked],
            tool="read_document", status="ok" if picked else "empty",
            coverage=Coverage(unit="chunks", scanned=len(got["chunks"]),
                              matched=len(rows), returned=len(picked),
                              truncated=len(rows) > len(picked)))

    def _shortlist() -> Dict:
        """Кандидаты внутри акта — ЧЕРЕЗ ОБЩИЙ ПОИСК, а не собственным скорингом.

        Прод-факт: на вопрос «какие кейсы были в акте» дважды подряд приходили
        одни и те же шесть фрагментов — титульный лист, сроки, таблица тарифов.
        То есть отбор шёл по совпадению СЛОВ, а слова «кейс» в акте нет вовсе:
        случаи описаны как «нарушения», «инциденты», «выявлено».

        Первая попытка чинить это чтением векторов из FAISS (`reconstruct`)
        оказалась хуже проблемы: отказ ловился поштучно и МОЛЧА, и при любом
        сбое всё откатывалось на ту же лексику без единой строки в логе. Такой
        «фолбэк» неотличим от работающего кода — ровно то, чего быть не должно.

        Теперь используется `retrieve()` с фильтром по проверке: семантика плюс
        BM25 плюс RRF, тот же путь, что и у остального поиска, со своими
        фолбэками и своими логами. Что не нашлось поиском — добирается по
        лексике, чтобы короткий акт не остался без кандидатов.
        """
        from backend.indexing.lexicon import tokenize
        from backend.rag.retrieval import retrieve

        want = _cfg().rerank_batch_pairs
        by_idx = {r["chunk_index"]: r for r in rows}
        picked: List[Dict] = []
        degraded: List[str] = []

        try:
            found = retrieve(query=query, km_filter=[check_id], mode="hybrid",
                             top_k=want * 2)
            for f in found:
                r = by_idx.get(f.get("chunk_index"))
                if r is not None and r not in picked:
                    picked.append(r)
        except Exception as e:
            logger.warning(f"[read_document] Поиск внутри акта {check_id} не "
                           f"сработал ({e}) — остаётся лексика")
            degraded.append("retrieval")

        if len(picked) < want:
            # Добор по словам: ловит точные номера и названия, которые
            # семантика размывает, и спасает совсем короткие акты
            terms = set(tokenize(query))
            rest = [r for r in rows if r not in picked]
            rest.sort(key=lambda r: -(
                len(terms & set(tokenize(r["text"][:1500]))) / (len(terms) or 1)
                + (0.15 if terms & set(tokenize(r["header_path"])) else 0.0)))
            picked.extend(rest[: want - len(picked)])

        if not picked:
            degraded.append("shortlist_empty")
        return {"picked": picked[:want], "degraded": degraded}

    got_sl = await run_cpu(_shortlist, prio=PRIO_INTERACTIVE, cancel=cancel,
                           stage="read_document_shortlist")
    shortlist = got_sl["picked"]

    def _rr() -> List[Dict]:
        from backend.rag.reranker import rerank
        return rerank(query, shortlist, top_n=n)

    picked = await run_cpu(_rr, prio=PRIO_INTERACTIVE, cancel=cancel,
                           stage="read_document_rerank")
    return ToolResult(
        evidence=[_to_evidence(r, "exact") for r in picked],
        tool="read_document", status="ok" if picked else "empty",
        degraded_sources=got_sl["degraded"],
        coverage=Coverage(unit="chunks", scanned=len(rows),
                          matched=len(shortlist), returned=len(picked),
                          truncated=len(rows) > len(picked)))


# ──────────────────────────────────────────────────────────────────
# Регистрация
# ──────────────────────────────────────────────────────────────────

register(ToolSpec(
    name="search_passages",
    human_label="Поиск фрагментов",
    description="точечный поиск по корпусу: несколько самых релевантных "
                "фрагментов актов",
    args_schema={"query": {"type": "str", "required": True,
                           "desc": "что искать"},
                 "km": {"type": "list[str]", "desc": "ограничить проверками"}},
    produces=PASSAGE, corpus_wide=False, role="body",
    cost_class="cpu", latency_hint_ms=800,
    when_not="вопрос про ОХВАТ («во всех актах», «сколько актов») — там "
             "search_coverage, иначе полнота будет обещана ложно",
    examples=("что находили по теме кредитных карт",),
), search_passages)

register(ToolSpec(
    name="search_coverage",
    human_label="Охват по актам",
    description="все акты, где встречается тема, с группировкой по проверкам "
                "и честной строкой полноты",
    args_schema={"query": {"type": "str", "required": True,
                           "desc": "тема или формулировка вопроса"},
                 "max_docs": {"type": "int", "desc": "потолок числа актов"}},
    produces=DOC, corpus_wide=True, role="body",
    cost_class="cpu", latency_hint_ms=3000,
    when_not="нужен один конкретный акт — там read_document",
    examples=("покажи все акты, где поднимался вопрос лимитов",),
), search_coverage)

register(ToolSpec(
    name="read_document",
    human_label="Чтение акта",
    description="фрагменты одного акта, отобранные под вопрос: ищет МЕСТО "
                "внутри акта, а не начало файла",
    args_schema={"check_id": {"type": "str", "required": True,
                              "desc": "номер проверки"},
                 "query": {"type": "str", "desc": "что искать внутри акта"},
                 "section": {"type": "str", "desc": "ограничить разделом"}},
    produces=PASSAGE, corpus_wide=False, role="body",
    cost_class="cpu", latency_hint_ms=2000,
    when_not="номер проверки неизвестен",
    examples=("выведи кейс из акта КМ-99-12345 про лимиты",
              "какие данные использовались в КМ-99-12345"),
), read_document)
