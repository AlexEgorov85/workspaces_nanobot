"""
Follow Up 2.0 — Резолвер 2.0: определение проверки (КМ) по письму.

Каскад сигналов:
  S1  идентификаторы — КМ-номера и рег.номера поручений («Поручение № 123»)
      → детерминированный авторезолв;
  S2  семантика ПО АБЗАЦАМ письма против чанков витрины (bge-m3);
  S3  лексические маркеры — П-коды процессов, CR, АС «…», суммы;
  S4  кросс-энкодер (bge-reranker) по лучшим парам абзац×поручение;
  fusion → группы-семейства → LLM-арбитр (вердикт/почему/цитата)
  → четыре уровня уверенности (auto / confirm / choice / none).

Цитаты арбитра валидируются на дословное вхождение в письмо —
галлюцинированные основания отбрасываются.
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
import uuid
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# Веса fusion (S1 — не вес, а авторезолв)
_W_RERANK, _W_SEM, _W_LEX = 0.45, 0.30, 0.25
_MIN_GROUP_SCORE = 0.20     # ниже — кандидата даже не показываем арбитру
_MAX_CANDIDATES = 5
_MAX_PARAGRAPHS = 20


def _ec():
    """Ленивый импорт execution_control (общие хелперы, без цикла)."""
    from backend.agents import execution_control as ec
    return ec


# ──────────────────────────────────────────────────────────────────
# S1: идентификаторы — рег.номера поручений
# ──────────────────────────────────────────────────────────────────

# «Поручение № 123», «поручения №№ 100, 101», «исполнение поручения N 4567»
_REG_NUM_RE = re.compile(
    r"поручени\w*[^.\n]{0,60}?№+\s*(\d{2,5}(?:\s*,\s*\d{2,5})*)",
    re.IGNORECASE)


def extract_reg_nums(text: str) -> List[str]:
    out: List[str] = []
    for m in _REG_NUM_RE.finditer(text or ""):
        for num in re.split(r"\s*,\s*", m.group(1)):
            num = num.strip()
            if num and num not in out:
                out.append(num)
    return out


def _match_by_reg_nums(text: str, all_rows: List[Dict]) -> Optional[Dict]:
    """Рег.номер поручения → строки витрины.

    Одно поручение (один документ) может закрывать НЕСКОЛЬКО разных
    проверок — если номер зарегистрирован под несколькими КМ, выбор
    своей проверки остаётся за аудитором (ambiguous), автоматически
    не выбираем никогда."""
    nums = extract_reg_nums(text[:8000])
    if not nums:
        return None
    matched = [r for r in all_rows
               if str(r.get("doc_reg_num") or "").strip() in nums]
    if not matched:
        return None
    kms = sorted({r["km_id"] for r in matched})
    if len(kms) == 1:
        # Карточку строим по КМ (все поручения КМ), не только по совпавшим
        rows = [r for r in all_rows if r["km_id"] == kms[0]]
        return {"rows": rows, "reg_nums": nums, "matched_kms": kms}
    logger.info(f"[Resolver] Рег.№ {nums} зарегистрированы под {len(kms)} КМ "
                f"— выбор за аудитором")
    return {"ambiguous": True, "reg_nums": nums, "matched_kms": kms,
            "matched": matched}


# ──────────────────────────────────────────────────────────────────
# S2: семантика по абзацам
# ──────────────────────────────────────────────────────────────────

def _split_paragraphs(text: str) -> List[str]:
    parts = [p.strip() for p in re.split(r"\n+", text or "")]
    parts = [p for p in parts if len(p) >= 60]
    if not parts and (text or "").strip():
        parts = [text.strip()[:1500]]
    return parts[:_MAX_PARAGRAPHS]


def _semantic_by_paragraphs(text: str, corpus: List[Dict]) -> Tuple[Dict, Dict]:
    """→ (score_by_key, best_paragraph_by_key). Score: 0.7·max + 0.3·топ-2."""
    from backend.indexing.embedder import embed_texts
    paras = _split_paragraphs(text)
    if not paras or not corpus:
        return {}, {}
    p_emb = np.asarray(embed_texts([p[:1200] for p in paras], normalize=True),
                       dtype=np.float32)
    c_emb = np.asarray([c["emb"] for c in corpus], dtype=np.float32)
    sims = p_emb @ c_emb.T                      # [P, C]
    score_by_key: Dict[str, float] = {}
    best_para: Dict[str, str] = {}
    per_key_para: Dict[str, np.ndarray] = {}
    for j, c in enumerate(corpus):
        key = c["poruch_key"]
        col = sims[:, j]
        cur = per_key_para.get(key)
        per_key_para[key] = col if cur is None else np.maximum(cur, col)
    for key, col in per_key_para.items():
        top = np.sort(col)[::-1]
        base = float(top[0])
        top2 = float(top[:2].mean()) if len(top) >= 2 else base
        score_by_key[key] = 0.7 * base + 0.3 * top2
        best_para[key] = paras[int(np.argmax(col))]
    return score_by_key, best_para


# ──────────────────────────────────────────────────────────────────
# S3: лексические маркеры
# ──────────────────────────────────────────────────────────────────

_MARKER_PATTERNS = [
    (re.compile(r"\bП\d{3,4}\b"), 1.0),                     # процессы П0000
    (re.compile(r"\bCR[-–]?\s?\d{3,}\b", re.I), 1.0),        # CR-12345
    (re.compile(r"АС\s*«([^»]{2,40})»"), 0.8),               # АС «Система»
    (re.compile(r"«([A-Za-z][\w .-]{3,30})»"), 0.8),         # «Product»
    (re.compile(r"\b\d{1,3}(?:[.,]\d{1,2})?\s*(?:тыс|млн|млрд)\b"), 0.6),
]


def extract_markers(text: str) -> List[Tuple[str, float]]:
    out, seen = [], set()
    for rx, w in _MARKER_PATTERNS:
        for m in rx.finditer(text or ""):
            token = (m.group(1) if m.groups() else m.group(0)).strip()
            k = token.lower()
            if k and k not in seen:
                seen.add(k)
                out.append((token, w))
    return out[:25]


def _lexical_scores(text: str, all_rows: List[Dict]) -> Dict[str, float]:
    markers = extract_markers(text[:8000])
    if not markers:
        return {}
    total_w = sum(w for _, w in markers)
    scores: Dict[str, float] = {}
    for r in all_rows:
        hay = " ".join(str(r.get(f) or "") for f in
                       ("problem", "assignment_", "actions")).lower()
        got = sum(w for tok, w in markers if tok.lower() in hay)
        if got:
            scores[r["poruch_key"]] = min(1.0, got / max(total_w, 1e-6))
    return scores


# ──────────────────────────────────────────────────────────────────
# S4: кросс-энкодер
# ──────────────────────────────────────────────────────────────────

def _rerank_scores(pairs: List[Tuple[str, str, str]]) -> Dict[str, float]:
    """pairs: [(poruch_key, абзац письма, текст поручения)] → key→[0..1]."""
    if not pairs:
        return {}
    try:
        from backend.rag.reranker import _get_reranker
        model = _get_reranker()
        if model is None:
            return {}
        raw = model.predict([(p[1][:512], p[2][:512]) for p in pairs],
                            show_progress_bar=False)
        out: Dict[str, float] = {}
        for (key, _, _), s in zip(pairs, raw):
            val = 1.0 / (1.0 + float(np.exp(-float(s))))    # сигмоида логита
            out[key] = max(out.get(key, 0.0), val)
        return out
    except Exception as e:
        logger.warning(f"[Resolver] Реранкер недоступен: {e}")
        return {}


# ──────────────────────────────────────────────────────────────────
# Fusion → группы-кандидаты
# ──────────────────────────────────────────────────────────────────

def _fuse_candidates(text: str) -> List[Dict]:
    ec = _ec()
    all_rows = ec.fetch_rows()
    corpus = ec.load_corpus()
    rows_by_key = {r["poruch_key"]: r for r in all_rows}

    sem, best_para = _semantic_by_paragraphs(text, corpus)
    lex = _lexical_scores(text, all_rows)

    # Реранк — только по топ-8 ключей предварительного скора
    pre = {k: 0.55 * sem.get(k, 0) + 0.45 * lex.get(k, 0)
           for k in set(sem) | set(lex)}
    top_keys = sorted(pre, key=pre.get, reverse=True)[:8]
    rr_pairs = []
    for k in top_keys:
        row = rows_by_key.get(k)
        if not row:
            continue
        poruch_text = ((row.get("problem") or "") + " " +
                       (row.get("assignment_") or ""))[:800]
        rr_pairs.append((k, best_para.get(k, text[:800]), poruch_text))
    rr = _rerank_scores(rr_pairs)

    fused: Dict[str, float] = {}
    for k in set(sem) | set(lex) | set(rr):
        fused[k] = (_W_RERANK * rr.get(k, 0.0) + _W_SEM * sem.get(k, 0.0)
                    + _W_LEX * lex.get(k, 0.0))

    # Группировка по семейству (текст проблемы), как в карточке
    groups: Dict[str, Dict] = {}
    for k, score in fused.items():
        row = rows_by_key.get(k)
        if not row or score < _MIN_GROUP_SCORE / 2:
            continue
        gkey = ec._problem_key(row.get("problem") or row.get("assignment_"))
        g = groups.setdefault(gkey, {
            "problem_short": (row.get("problem") or
                              row.get("assignment_") or "")[:220],
            "score": 0.0, "kms": {}, "reg_nums": [], "block_unit": None,
            "best_para": best_para.get(k, "")[:400],
            "signals": {"sem": 0.0, "lex": 0.0, "rr": 0.0},
        })
        g["score"] = max(g["score"], score)
        g["signals"]["sem"] = max(g["signals"]["sem"], sem.get(k, 0.0))
        g["signals"]["lex"] = max(g["signals"]["lex"], lex.get(k, 0.0))
        g["signals"]["rr"] = max(g["signals"]["rr"], rr.get(k, 0.0))
        g["kms"].setdefault(row["km_id"], {
            "status": row.get("poruch_status"),
            "close": str(row.get("close_fact") or "") or None})
        rn = str(row.get("doc_reg_num") or "").strip()
        if rn and rn not in g["reg_nums"]:
            g["reg_nums"].append(rn)
        g["block_unit"] = g["block_unit"] or row.get("block_unit")

    cands = sorted(groups.values(), key=lambda g: -g["score"])
    cands = [g for g in cands if g["score"] >= _MIN_GROUP_SCORE]

    # Один набор КМ = один кандидат: три поручения одной проверки не
    # должны выглядеть тремя отдельными вариантами выбора (прод-факт)
    merged: Dict[frozenset, Dict] = {}
    for g in cands:
        key = frozenset(g["kms"])
        cur = merged.get(key)
        if cur is None:
            g["other_problems"] = []
            merged[key] = g
        else:
            cur["score"] = max(cur["score"], g["score"])
            for s, v in g["signals"].items():
                cur["signals"][s] = max(cur["signals"][s], v)
            if len(cur["other_problems"]) < 3:
                cur["other_problems"].append(g["problem_short"][:140])
            for rn in g["reg_nums"]:
                if rn not in cur["reg_nums"]:
                    cur["reg_nums"].append(rn)
    cands = sorted(merged.values(), key=lambda g: -g["score"])

    # Число поручений проверки (по первому КМ группы)
    for g in cands:
        km0 = next(iter(g["kms"]))
        g["n_poruchs"] = sum(1 for r in all_rows if r["km_id"] == km0)
    return cands[:_MAX_CANDIDATES]


def _enrich_candidates_km_info(cands: List[Dict]) -> None:
    """Per-KM контекст для карточки выбора: аудитор не помнит номера.
    Тема — первый пункт витрины актов (у головной и дочерних КМ темы
    РАЗНЫЕ), бейджи наличия акта в корпусе и репозитория. Головная КМ
    (с артефактами) — первой в ряду кнопок."""
    from backend.storage import gp
    if not cands or not gp.gp_enabled():
        return
    try:
        all_kms = sorted({km for g in cands for km in g.get("kms", {})})
        if not all_kms:
            return
        hints: Dict[str, str] = {}
        acts, repos = set(), set()
        try:
            hints = gp.ActVitrinaRepo.first_points(all_kms)
        except Exception as e:
            logger.warning(f"[Resolver] Темы из витрины: {e}")
        check_ids = [f"КМ-{km}" for km in all_kms]
        try:
            acts = {r["check_id"] for r in gp.gp_query(
                f"SELECT DISTINCT check_id FROM {gp._schema()}.t_fu_act_docs "
                f"WHERE check_id = ANY(%s)", (check_ids,))}
            repos = {r["check_id"] for r in gp.gp_query(
                f"SELECT DISTINCT check_id FROM {gp._schema()}.t_fu_repo_index "
                f"WHERE check_id = ANY(%s)", (check_ids,))}
        except Exception as e:
            logger.warning(f"[Resolver] Наличие артефактов: {e}")
        for g in cands:
            info = {}
            for km, v in g.get("kms", {}).items():
                info[km] = {"status": (v or {}).get("status"),
                            "hint": hints.get(km),
                            "has_act": f"КМ-{km}" in acts,
                            "has_repo": f"КМ-{km}" in repos}
            order = sorted(info, key=lambda k: (not info[k]["has_act"],
                                                not info[k]["has_repo"], k))
            g["kms"] = {k: g["kms"][k] for k in order}
            g["kms_info"] = {k: info[k] for k in order}
    except Exception as e:
        logger.warning(f"[Resolver] Обогащение КМ-контекстом: {e}")


# ──────────────────────────────────────────────────────────────────
# LLM-арбитр
# ──────────────────────────────────────────────────────────────────

def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


async def _arbiter(text: str, cands: List[Dict],
                   model: Optional[str]) -> Optional[Dict]:
    from backend.llm.client import generate_async
    from backend.llm.prompts.execution_control import (
        RESOLVE_VERIFY_SYSTEM, RESOLVE_VERIFY_USER_TEMPLATE)
    ec = _ec()
    blocks = []
    for i, g in enumerate(cands, 1):
        kms = ", ".join(f"КМ-{k} ({v.get('status') or 'нет статуса'})"
                        for k, v in g["kms"].items())
        others = "".join(f"\nДругая проблема той же проверки: {p}"
                         for p in (g.get("other_problems") or []))
        blocks.append(
            f"Кандидат {i}: {kms}\n"
            f"Проблема проверки: {g['problem_short']}{others}\n"
            f"Рег.№ поручений: {', '.join(g['reg_nums']) or '—'}; "
            f"исполнитель: {g.get('block_unit') or '—'}; "
            f"поручений: {g.get('n_poruchs')}")
    messages = [
        {"role": "system", "content": RESOLVE_VERIFY_SYSTEM},
        {"role": "user", "content": RESOLVE_VERIFY_USER_TEMPLATE.format(
            letter=text[:4000],
            candidates_block="\n\n".join(blocks))}]
    parsed = None
    for attempt in (1, 2):     # один ретрай при невалидном JSON
        try:
            raw = await generate_async(messages, model=model,
                                       max_tokens=2500, temperature=0.0)
        except Exception as e:
            logger.warning(f"[Resolver] Арбитр недоступен: {e}")
            return None
        parsed = ec._parse_llm_json(raw)
        if parsed and isinstance(parsed.get("candidates"), list):
            break
        logger.warning(f"[Resolver] Арбитр вернул невалидную структуру "
                       f"(попытка {attempt})")
        parsed = None
        messages = messages + [
            {"role": "assistant", "content": (raw or "")[:1500]},
            {"role": "user", "content": "Ответ невалиден. Верни ТОЛЬКО "
                                        "валидный JSON по схеме из "
                                        "инструкции, без пояснений."}]
    if not parsed:
        return None
    # Валидация цитат: только дословные (по нормализованным пробелам)
    letter_norm = _norm_ws(text)
    for c in parsed["candidates"]:
        q = c.get("quote")
        if q and _norm_ws(q) not in letter_norm:
            c["quote"] = None
    return parsed


# ──────────────────────────────────────────────────────────────────
# Главный вход
# ──────────────────────────────────────────────────────────────────

async def resolve_smart(query_ctx, model: Optional[str] = None) -> Dict:
    """
    → {"status": "resolved"|"confirm"|"choice"|"none"|"empty",
       "rows", "provenance", "candidates", "resolve_id", "duration_ms"}

    resolved: строить карточку (provenance в шапку);
    confirm:  один сильный кандидат — попросить подтверждение;
    choice:   2-4 кандидата — карточка выбора;
    none:     совпадений нет — уточнение (слабые темы в candidates).
    """
    ec = _ec()
    t0 = time.time()
    resolve_id = uuid.uuid4().hex[:12]
    full_text = (query_ctx.raw_query + "\n" +
                 (query_ctx.attachment_text or ""))
    all_rows = ec.fetch_rows()
    if not all_rows:
        return {"status": "empty", "rows": [], "candidates": [],
                "resolve_id": resolve_id}

    def _done(res: Dict) -> Dict:
        res["resolve_id"] = resolve_id
        res["duration_ms"] = int((time.time() - t0) * 1000)
        res["letter_hash"] = hashlib.md5(
            full_text.encode("utf-8")).hexdigest()[:32]
        return res

    # ── S1a: явный КМ-номер ──
    km_ids = [k.replace("КМ-", "") for k in query_ctx.km_numbers]
    for k in ec._extract_km_ids(full_text[:6000]):
        if k not in km_ids:
            km_ids.append(k)
    known = {r["km_id"] for r in all_rows}
    matched_kms = [k for k in km_ids if k in known]
    if matched_kms:
        rows = [r for r in all_rows if r["km_id"] in matched_kms]
        if len(matched_kms) > 1:
            seen_p, dedup = set(), []
            for r in rows:
                pk = (r.get("doc_reg_num"),
                      ec._problem_key(r.get("assignment_")))
                if pk not in seen_p:
                    seen_p.add(pk)
                    dedup.append(r)
            rows = dedup
        return _done({"status": "resolved", "rows": rows, "candidates": [],
                      "provenance": {"how": "km",
                                     "detail": ", ".join(
                                         f"КМ-{k}" for k in matched_kms)}})

    # ── S1b: рег.номер поручения ──
    reg = _match_by_reg_nums(full_text, all_rows)
    if reg and not reg.get("ambiguous"):
        return _done({"status": "resolved", "rows": reg["rows"],
                      "candidates": [],
                      "provenance": {"how": "reg_num",
                                     "detail": "рег.№ поручения "
                                               + ", ".join(reg["reg_nums"])}})
    if reg:
        # Номер под несколькими КМ: точное попадание, но проверку
        # выбирает аудитор — по одному КМ в кнопке
        by_km: Dict[str, Dict] = {}
        for r in reg["matched"]:
            by_km.setdefault(r["km_id"], r)
        first = reg["matched"][0]
        cand = {
            "problem_short": (first.get("problem") or
                              first.get("assignment_") or "")[:220],
            "kms": {km: {"status": r.get("poruch_status"),
                         "close": str(r.get("close_fact") or "") or None}
                    for km, r in by_km.items()},
            "reg_nums": reg["reg_nums"],
            "block_unit": first.get("block_unit"),
            "n_poruchs": sum(1 for r in all_rows
                             if r["km_id"] == reg["matched_kms"][0]),
            "other_problems": [],
            "verdict": "yes", "quote": None, "confidence": 1.0,
            "why": ("В письме найден рег.№ поручения "
                    + ", ".join(reg["reg_nums"])
                    + " — он зарегистрирован под несколькими проверками"),
        }
        _enrich_candidates_km_info([cand])
        return _done({"status": "choice", "rows": [], "candidates": [cand],
                      "hint": "Поручение зарегистрировано под несколькими "
                              "КМ — выберите вашу проверку."})

    # ── S2-S4 + fusion ──
    try:
        cands = _fuse_candidates(full_text)
    except Exception as e:
        logger.exception(f"[Resolver] Fusion упал: {e}")
        cands = []
    if not cands:
        return _done({"status": "none", "rows": [], "candidates": [],
                      "hint": None})

    # ── LLM-арбитр ──
    verdicts = await _arbiter(full_text, cands, model)
    if verdicts:
        by_ref = {int(c.get("ref", 0)): c for c in verdicts["candidates"]
                  if isinstance(c.get("ref"), (int, float, str))
                  and str(c.get("ref")).isdigit()}
        for i, g in enumerate(cands, 1):
            v = by_ref.get(i, {})
            g["verdict"] = v.get("verdict") if v.get("verdict") in (
                "yes", "likely", "no") else "no"
            g["why"] = (v.get("why") or "")[:300] or None
            g["quote"] = (v.get("quote") or "")[:300] or None
            try:
                g["confidence"] = max(0.0, min(1.0, float(
                    v.get("confidence") or 0)))
            except (TypeError, ValueError):
                g["confidence"] = 0.0
        hint = (verdicts.get("clarify_hint") or "")[:300] or None
    else:
        # Арбитр недоступен — деградация до эвристики по скору
        for g in cands:
            g["verdict"] = ("yes" if g["score"] >= 0.55
                            else "likely" if g["score"] >= 0.35 else "no")
            g["why"], g["quote"], g["confidence"] = None, None, g["score"]
        hint = None

    yes = [g for g in cands if g.get("verdict") == "yes"]
    likely = [g for g in cands if g.get("verdict") == "likely"]
    order = sorted(yes, key=lambda g: -g.get("confidence", 0)) + \
        sorted(likely, key=lambda g: -g.get("confidence", 0))
    logger.info(f"[Resolver] Арбитр: yes={len(yes)}, likely={len(likely)}, "
                f"кандидатов={len(cands)}")
    # Per-KM контекст для карточки выбора (темы, бейджи, порядок кнопок)
    _enrich_candidates_km_info(cands)

    def _rows_for(g: Dict) -> List[Dict]:
        km = next(iter(g["kms"]))
        return [r for r in all_rows if r["km_id"] == km]

    # T1: единственный уверенный yes, ОБЯЗАТЕЛЬНО с дословной цитатой —
    # авторезолв без текстового доказательства запрещён (прод-факт:
    # тематически близкая, но чужая проверка прошла с confidence 0.7).
    # Без цитаты даже единственный yes идёт на подтверждение (T2).
    # И только для кандидата с ОДНИМ КМ: если поручения зарегистрированы
    # под несколькими КМ, свою проверку выбирает аудитор
    if (len(yes) == 1 and yes[0].get("confidence", 0) >= 0.75
            and yes[0].get("quote") and len(yes[0].get("kms", {})) == 1):
        g = yes[0]
        # Альтернативы для «Не та проверка?» — ВСЕ остальные кандидаты
        # fusion (включая отвергнутые арбитром: если авторезолв ошибся,
        # правильная проверка скорее всего среди них)
        alts = sorted((c for c in cands if c is not g),
                      key=lambda c: -c["score"])[:3]
        return _done({"status": "resolved", "rows": _rows_for(g),
                      "candidates": alts,
                      "provenance": {"how": "semantic",
                                     "detail": g.get("why"),
                                     "quote": g.get("quote")}})
    # T2: один сильный кандидат (yes без отрыва или единственный likely)
    if len(yes) == 1 or (not yes and len(likely) == 1):
        return _done({"status": "confirm", "rows": [],
                      "candidates": order[:4], "hint": hint})
    # T3: несколько кандидатов
    if order:
        return _done({"status": "choice", "rows": [],
                      "candidates": order[:4], "hint": hint})
    # T4: всё отвергнуто — слабые темы для подсказки
    weak = sorted(cands, key=lambda g: -g["score"])[:3]
    return _done({"status": "none", "rows": [], "candidates": weak,
                  "hint": hint})


def log_resolve(event: str, resolve_id: str, tier: Optional[str] = None,
                letter_hash: Optional[str] = None,
                shown_kms: Optional[List[str]] = None,
                chosen_km: Optional[str] = None,
                how: Optional[str] = None,
                duration_ms: Optional[int] = None) -> None:
    """Журнал резолвера (метрики качества пилота). Не роняет поток."""
    try:
        from backend.storage import gp
        if not gp.gp_enabled():
            return
        gp.ResolveLogRepo.add(event, resolve_id, tier, letter_hash,
                              ",".join(shown_kms or [])[:500] or None,
                              chosen_km, how, duration_ms)
    except Exception as e:
        logger.warning(f"[Resolver] Лог не записан: {e}")
