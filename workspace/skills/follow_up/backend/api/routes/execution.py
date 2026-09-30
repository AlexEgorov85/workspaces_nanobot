"""Follow Up 2.0 — API скилла «Контроль исполнения поручений».

Вердикты аудитора + drill-down данные для блоков карточки.
Без GP вердикты пишутся в локальный файл (демо-режим).
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/execution", tags=["execution"])

_local_lock = threading.Lock()

VERDICTS = ("solved", "not_solved", "rework")


class VerdictRequest(BaseModel):
    poruch_key: str
    km_id: Optional[str] = None
    verdict: str            # solved | not_solved | rework
    comment: Optional[str] = None


def _local_verdicts_path():
    return get_settings().index_dir / "verdicts_local.json"


@router.post("/verdicts")
async def add_verdict(req: VerdictRequest):
    """Фиксация вердикта аудитора (замыкание контура)."""
    if req.verdict not in VERDICTS:
        raise HTTPException(status_code=400,
                            detail=f"verdict должен быть одним из {VERDICTS}")
    from backend.storage import gp
    if gp.gp_enabled():
        gp.VerdictRepo.add(req.poruch_key, req.km_id, req.verdict, req.comment)
        return {"ok": True, "stored": "greenplum"}

    # Демо-режим: локальный файл
    path = _local_verdicts_path()
    with _local_lock:
        data = []
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, TypeError):
                data = []
        data.append({
            "poruch_key": req.poruch_key, "km_id": req.km_id,
            "verdict": req.verdict, "comment": req.comment,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        })
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    return {"ok": True, "stored": "local"}


@router.get("/poruch/{poruch_key}")
async def poruch_detail(poruch_key: str):
    """Полная строка поручения (drill-down блока «Поручение»)."""
    from backend.agents.execution_control import fetch_rows, _row_public
    for r in fetch_rows():
        if r["poruch_key"] == poruch_key:
            return {"poruch": _row_public(r)}
    raise HTTPException(status_code=404, detail="Поручение не найдено")


@router.get("/stats")
async def unit_stats(block_unit: Optional[str] = None):
    """Разбивка по подразделению (drill-down блока «Статистика»)."""
    from backend.agents.execution_control import fetch_rows, _row_public
    from backend.storage import gp
    rows = fetch_rows()
    if block_unit:
        target = set(gp.normalize_block_unit(block_unit))
        rows = [r for r in rows
                if set(gp.normalize_block_unit(r.get("block_unit"))) & target]
    return {
        "count": len(rows),
        "items": [_row_public(r) for r in rows[:100]],
    }


# ──────────────────────────────────────────────────────────────────
# Журнал резолвера: пользователь выбрал проверку из предложенных
# ──────────────────────────────────────────────────────────────────

class ResolveChoice(BaseModel):
    resolve_id: str
    km_id: str
    mode: Optional[str] = None      # confirm | choice | undo


@router.post("/resolve-choice")
async def resolve_choice(req: ResolveChoice):
    from backend.agents.poruch_resolver import log_resolve
    log_resolve("chosen", req.resolve_id, tier=req.mode,
                chosen_km=req.km_id, how="user")
    return {"ok": True}


# ──────────────────────────────────────────────────────────────────
# Полнотекст акта («Читать акт» в модалке исходной проверки)
# ──────────────────────────────────────────────────────────────────

@router.get("/act-content")
async def act_content(act: str, matched_by: str = "exact"):
    """Все чанки акта по порядку — для чтения в модалке."""
    import re as _re
    from backend.agents.execution_control import (
        _fetch_act_chunks, _fetch_act_chunks_by_filename)
    if matched_by in ("filename", "semantic_unknown"):
        m = _re.search(r"«(.+?)»", act or "")
        chunks = _fetch_act_chunks_by_filename(m.group(1)) if m else []
    else:
        chunks = _fetch_act_chunks(act)
    if not chunks:
        raise HTTPException(status_code=404, detail="Акт не найден в базе")
    chunks.sort(key=lambda c: c.get("chunk_id") or 0)
    return {"act": act, "chunks": chunks[:400]}


# ──────────────────────────────────────────────────────────────────
# Черновик письма-запроса профильному подразделению (LLM по клику)
# ──────────────────────────────────────────────────────────────────

class DraftRequest(BaseModel):
    check_id: str
    rows: list = []          # строки блока «Поручение» (payload.rows)
    items: list = []         # анализ (payload.analysis.items)
    model: Optional[str] = None


@router.post("/draft-request")
async def draft_request(req: DraftRequest):
    """Готовое письмо-запрос недостающих доказательств."""
    from backend.llm.client import generate_async
    from backend.llm.prompts.execution_control import (
        REQUEST_DRAFT_SYSTEM, REQUEST_DRAFT_USER_TEMPLATE)
    by_key = {i.get("poruch_key"): i for i in req.items}
    lines, missing = [], []
    for n, r in enumerate(req.rows, 1):
        lines.append(f"Поручение {n} (рег.№ {r.get('doc_reg_num') or '—'}): "
                     f"{(r.get('assignment_') or '')[:400]}")
        a = by_key.get(r.get("poruch_key"))
        if a:
            lines.append(f"  Заявление профильника: {(a.get('claim') or '')[:300]}")
            lines.append(f"  Качество доказательств: {a.get('evidence_quality') or '—'}"
                         f", характер ответа: {a.get('formality') or '—'}")
            for x in (a.get("missing_evidence") or []):
                missing.append(f"- (поручение {n}) {x}")
            for x in (a.get("what_to_request") or []):
                missing.append(f"- (поручение {n}) {x}")
    if not missing:
        missing = ["- подтверждающие документы по каждому заявлению ответа"]
    try:
        letter = await generate_async(
            [{"role": "system", "content": REQUEST_DRAFT_SYSTEM},
             {"role": "user", "content": REQUEST_DRAFT_USER_TEMPLATE.format(
                 check_id=req.check_id,
                 analysis_block="\n".join(lines)[:6000],
                 missing_block="\n".join(dict.fromkeys(missing))[:2500])}],
            model=req.model, max_tokens=2500, temperature=0.2)
    except Exception as e:
        logger.exception(f"[Exec] Черновик запроса: {e}")
        raise HTTPException(status_code=502, detail=f"LLM недоступен: {e}")
    return {"letter": letter.strip()}


# ──────────────────────────────────────────────────────────────────
# Просмотр и LLM-аннотация файла репозитория
# ──────────────────────────────────────────────────────────────────

@router.get("/repo-file")
async def repo_file(slug: str, path: str):
    """Содержимое файла из BitBucket (просмотр в модалке)."""
    from backend.connectors import bitbucket
    if not bitbucket.available():
        raise HTTPException(status_code=503,
                            detail="BitBucket недоступен: укажите BITBUCKET_TOKEN")
    content = bitbucket.get_file_content(slug, path)
    if content is None:
        raise HTTPException(status_code=404, detail="Файл не найден")
    return {"slug": slug, "path": path, "content": content,
            "file_url": bitbucket.file_url(slug, path)}


class AnnotateRequest(BaseModel):
    slug: str
    path: str
    check_id: Optional[str] = None
    model: Optional[str] = None


@router.post("/annotate-script")
async def annotate_script(req: AnnotateRequest):
    """LLM-аннотация скрипта: что делает, откуда читает, воспроизводим ли.
    Кэшируется на файл — один вызов для всех пользователей."""
    from backend.storage import gp
    from backend.agents.execution_control import (
        _local_cache_get, _local_cache_put, _parse_llm_json)
    cache_key = f"{req.slug}::{req.path}"
    if gp.gp_enabled():
        cached = gp.ScriptAnnotRepo.get(req.slug, req.path)
        if cached:
            return {"annot": cached, "cached": True}
    else:
        cached = _local_cache_get("script_annot", cache_key)
        if cached:
            return {"annot": cached, "cached": True}

    from backend.connectors import bitbucket
    if not bitbucket.available():
        raise HTTPException(status_code=503,
                            detail="BitBucket недоступен: укажите BITBUCKET_TOKEN")
    code = bitbucket.get_file_content(req.slug, req.path, max_chars=24_000)
    if code is None:
        raise HTTPException(status_code=404, detail="Файл не найден")

    from backend.llm.client import generate_async
    from backend.llm.prompts.execution_control import (
        SCRIPT_ANNOT_SYSTEM, SCRIPT_ANNOT_USER_TEMPLATE)
    try:
        raw = await generate_async(
            [{"role": "system", "content": SCRIPT_ANNOT_SYSTEM},
             {"role": "user", "content": SCRIPT_ANNOT_USER_TEMPLATE.format(
                 file_path=req.path, repo_slug=req.slug,
                 check_id=req.check_id or "—", code=code)}],
            model=req.model, max_tokens=2500, temperature=0.0)
    except Exception as e:
        logger.exception(f"[Exec] Аннотация скрипта: {e}")
        raise HTTPException(status_code=502, detail=f"LLM недоступен: {e}")
    annot = _parse_llm_json(raw)
    if not annot:
        raise HTTPException(status_code=502, detail="LLM вернул невалидную структуру")
    try:
        if gp.gp_enabled():
            gp.ScriptAnnotRepo.put(req.slug, req.path, annot, req.model or "auto")
        else:
            _local_cache_put("script_annot", cache_key, annot)
    except Exception as e:
        logger.warning(f"[Exec] Кэш аннотации: {e}")
    return {"annot": annot, "cached": False}


# ──────────────────────────────────────────────────────────────────
# Персистентный чек-лист плана проверки
# ──────────────────────────────────────────────────────────────────

class ChecklistItem(BaseModel):
    card_id: str
    step_idx: int
    done: bool = False
    note: Optional[str] = None


def _local_checklist_path():
    return get_settings().index_dir / "checklist_local.json"


@router.post("/checklist")
async def checklist_set(item: ChecklistItem):
    from backend.storage import gp
    if gp.gp_enabled():
        gp.ChecklistRepo.set_item(item.card_id, item.step_idx,
                                  item.done, item.note)
        return {"ok": True, "stored": "greenplum"}
    path = _local_checklist_path()
    with _local_lock:
        data = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, TypeError):
                data = {}
        data.setdefault(item.card_id, {})[str(item.step_idx)] = {
            "done": item.done, "note": item.note}
        path.write_text(json.dumps(data, ensure_ascii=False),
                        encoding="utf-8")
    return {"ok": True, "stored": "local"}


@router.get("/checklist/{card_id}")
async def checklist_get(card_id: str):
    from backend.storage import gp
    if gp.gp_enabled():
        return {"items": {str(k): v for k, v in
                          gp.ChecklistRepo.get(card_id).items()}}
    path = _local_checklist_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return {"items": data.get(card_id, {})}
        except (ValueError, TypeError):
            pass
    return {"items": {}}


# ──────────────────────────────────────────────────────────────────
# Экспорт карточки в DOCX (рабочая программа проверки)
# ──────────────────────────────────────────────────────────────────

class ExportRequest(BaseModel):
    card: dict


@router.post("/export/docx")
async def export_docx(req: ExportRequest):
    from fastapi.responses import Response
    from backend.reports.card_docx import build_card_docx
    card = req.card or {}
    km = card.get("km_id") or "unknown"
    try:
        checklist = (await checklist_get(card.get("card_id") or ""))["items"]
    except Exception:
        checklist = {}
    try:
        data = build_card_docx(card, checklist)
    except Exception as e:
        logger.exception(f"[Exec] DOCX экспорт: {e}")
        raise HTTPException(status_code=500, detail=f"Не удалось собрать DOCX: {e}")
    fname = f"kontrol_ispolneniya_KM-{km}.docx"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument"
                   ".wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'})


# ──────────────────────────────────────────────────────────────────
# Дэшборд руководителя: дисциплина исполнения по подразделениям
# ──────────────────────────────────────────────────────────────────

@router.get("/dashboard")
async def dashboard():
    from backend.agents.execution_control import fetch_rows
    from backend.storage import gp
    rows = fetch_rows()
    units: dict = {}
    g_total = g_done = 0
    for r in rows:
        st = (r.get("poruch_status") or "").lower()
        is_done = "исполнено" in st and "не " not in st
        g_total += 1
        if is_done:
            g_done += 1
        for u in (gp.normalize_block_unit(r.get("block_unit")) or ["(не указано)"]):
            s = units.setdefault(u, {"unit": u, "total": 0, "done": 0,
                                     "in_progress": 0, "no_status": 0,
                                     "kms": set()})
            s["total"] += 1
            s["kms"].add(r["km_id"])
            if is_done:
                s["done"] += 1
            elif st:
                s["in_progress"] += 1
            else:
                s["no_status"] += 1

    # Паттерн отписок из накопленных анализов (если GP доступен)
    patterns = {}
    if gp.gp_enabled():
        for u in units:
            try:
                p = gp.CardAnalysisRepo.unit_response_pattern([u])
                if p and p.get("formal_share") is not None:
                    patterns[u] = p
            except Exception:
                break

    items = []
    for u, s in units.items():
        undone_share = round(1 - s["done"] / s["total"], 2) if s["total"] else 0
        item = {**s, "kms": len(s["kms"]), "undone_share": undone_share}
        if u in patterns:
            item["formal_share"] = patterns[u]["formal_share"]
            item["analyzed"] = patterns[u]["analyzed"]
        items.append(item)
    items.sort(key=lambda x: (-x["undone_share"], -x["total"]))
    return {
        "totals": {"poruch": g_total, "done": g_done,
                   "done_share": round(g_done / g_total, 2) if g_total else None,
                   "units": len(units)},
        "units": items,
    }
