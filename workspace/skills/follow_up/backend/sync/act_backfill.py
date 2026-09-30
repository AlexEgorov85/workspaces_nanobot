"""
Follow Up 2.0 — автодогрузка недостающих актов из витрины пунктов актов.

Часть актов не загружена в корпус знаний как .docx, но их тексты (пункты
нарушений) лежат в GP-витрине пунктов актов (cfg.gp_act_vitrina_view). Модуль
собирает из пунктов «витринный акт»: документ + чанки с эмбеддингами +
базовые отклонения (по одному на пункт) и публикует в общий корпус
t_fu_act_docs/chunks/deviations — дальше штатная гидратация раздаёт
всем инстансам.

Работает в трёх режимах:
  - по требованию из карточки: нет акта → backfill_for_card() собирает
    его прямо сейчас (эмбеддинг на CPU, секунды);
  - фоновый цикл: под lease-локом доливает недостающие акты батчами;
  - массово из админ-тетрадки scripts/fu_act_backfill.ipynb.

Витринный акт — ВРЕМЕННАЯ замена: когда появляется полный .docx,
чтение предпочитает его, а публикация полного акта удаляет витринный
(file_id с префиксом vitrina://).
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional

from backend.core import identity

logger = logging.getLogger(__name__)

VITRINA_PREFIX = "vitrina://"
_MAX_CHUNK = 1400
_LEASE_KEY = "act_backfill"


# ──────────────────────────────────────────────────────────────────
# Сборка документов из пунктов витрины (чистые функции)
# ──────────────────────────────────────────────────────────────────

def split_point_text(text: str, max_c: int = _MAX_CHUNK) -> List[str]:
    """Длинный пункт акта → куски ≤ max_c по границам предложений."""
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if not text:
        return []
    if len(text) <= max_c:
        return [text]
    out, buf = [], ""
    for sent in re.split(r"(?<=[.!?;])\s+", text):
        if buf and len(buf) + len(sent) + 1 > max_c:
            out.append(buf)
            buf = sent
        else:
            buf = (buf + " " + sent).strip()
        while len(buf) > max_c:            # предложение длиннее лимита
            out.append(buf[:max_c])
            buf = buf[max_c:]
    if buf:
        out.append(buf)
    return out


def build_docs_for_km(km: str, rows: List[Dict]) -> List[Dict]:
    """Пункты витрины одного КМ → документы корпуса.

    Один документ на act_realized_doc (СЗ/акт). Возвращает
    [{doc, chunks: [{chunk_index, header_path, text}], deviations}].
    """
    by_doc: Dict[str, List[Dict]] = {}
    for r in rows:
        key = str(r.get("act_realized_doc") or "—").strip()
        by_doc.setdefault(key, []).append(r)

    docs: List[Dict] = []
    for doc_num, points in by_doc.items():
        file_id = VITRINA_PREFIX + hashlib.md5(
            f"{km}|{doc_num}".encode("utf-8")).hexdigest()
        chunks: List[Dict] = []
        deviations: List[Dict] = []
        for p in points:
            sub = str(p.get("act_sub_number") or "").strip().rstrip(".")
            header = f"Пункт {sub}" if sub else "Пункт"
            text = (p.get("content") or p.get("description") or "").strip()
            if not text:
                continue
            for piece in split_point_text(text):
                chunks.append({"chunk_index": len(chunks),
                               "header_path": header,
                               "text": piece})
            desc = (p.get("description") or text)[:800].strip()
            if desc:
                deviations.append({
                    "check_id": f"КМ-{km}",
                    "description": desc,
                    "category": None, "severity": None,
                    "financial_impact_rub": None,
                    "affected_systems": None,
                    "regulation_refs": None, "affected_count": None,
                    "responsible_unit": None,
                    "recommendation": None,
                })
        if not chunks:
            continue
        docs.append({
            "doc": {
                "file_id": file_id,
                "filename": f"КМ-{km}_акт-{doc_num}_из_витрины.md",
                "check_id": f"КМ-{km}",
                "topic": (points[0].get("process_codes_list") or None),
            },
            "chunks": chunks,
            "deviations": deviations,
        })
    return docs


# ──────────────────────────────────────────────────────────────────
# Заливка в общий корпус
# ──────────────────────────────────────────────────────────────────

def backfill_km_sync(km: str) -> List[Dict]:
    """Собирает витринный акт КМ, эмбеддит и публикует в GP.
    Возвращает чанки для немедленного использования карточкой
    ([{chunk_id, header_path, text}]; [] — в витрине по КМ пусто).
    """
    from backend.storage import gp
    from backend.indexing.embedder import embed_texts

    rows = gp.ActVitrinaRepo.fetch_km_rows(km)
    if not rows:
        return []
    docs = build_docs_for_km(km, rows)
    if not docs:
        return []

    card_chunks: List[Dict] = []
    for d in docs:
        texts = [c["text"] for c in d["chunks"]]
        embs = embed_texts(texts, normalize=True)
        chunk_dicts = [{**c, "emb": [float(x) for x in e]}
                       for c, e in zip(d["chunks"], embs)]
        gp.ActGPRepo.push_document(d["doc"], chunk_dicts, d["deviations"])
        for c in d["chunks"]:
            card_chunks.append({"chunk_id": c["chunk_index"],
                                "header_path": c["header_path"],
                                "text": c["text"]})
    n_chunks = len(card_chunks)
    logger.info(f"[Backfill] КМ {km}: собран акт из витрины — "
                f"{len(docs)} докум., {n_chunks} чанков")
    # Локальные кэши (свой и чужие) наполнит ФОНОВАЯ гидратация:
    # инлайн-гидратация здесь тянула всю дельту и пересобирала BM25
    # внутри запроса карточки (прод: карточка строилась ~5 минут)
    return card_chunks


def backfill_for_card(kms: List[str]) -> Optional[Dict]:
    """Догрузка по требованию из карточки: первый КМ, по которому в
    витрине есть тексты. Возвращает {chunks, act_check_id} или None."""
    from backend.storage import gp
    if not gp.gp_enabled():
        return None
    for km in kms:
        try:
            chunks = backfill_km_sync(km)
        except Exception as e:
            logger.warning(f"[Backfill] КМ {km}: {e}")
            continue
        if chunks:
            return {"chunks": chunks, "act_check_id": f"КМ-{km}"}
    return None


# ──────────────────────────────────────────────────────────────────
# Фоновый цикл: доливаем недостающие акты батчами под lease-локом
# ──────────────────────────────────────────────────────────────────

_state: Dict = {"status": "idle", "last_run_at": None, "last_error": None,
                "backfilled_total": 0, "missing_left": None}
_state_lock = threading.Lock()
_stop_event = threading.Event()
_thread: Optional[threading.Thread] = None


def backfill_status() -> Dict:
    with _state_lock:
        return dict(_state)


def missing_kms() -> List[str]:
    """КМ, чьи тексты есть в витрине, а акта в корпусе нет."""
    from backend.storage import gp
    have = set(gp.ActGPRepo.corpus_check_ids())
    return sorted(km for km in gp.ActVitrinaRepo.distinct_kms()
                  if f"КМ-{km}" not in have)


def backfill_once(batch: Optional[int] = None) -> Dict:
    """Один проход: до batch недостающих актов. Идемпотентен."""
    from backend.config import get_settings
    from backend.storage import gp
    cfg = get_settings()
    n = batch or cfg.fu_backfill_batch
    summary = {"backfilled": 0, "missing_left": 0}

    if not gp.is_owner():
        logger.info("[Backfill] Режим читателя — догрузка не наша работа")
        return summary

    missing = missing_kms()
    summary["missing_left"] = len(missing)
    with _state_lock:
        _state["missing_left"] = len(missing)
    if not missing:
        return summary

    if not gp.SyncLockRepo.try_acquire(_LEASE_KEY):
        logger.info("[Backfill] Lease занята другим инстансом — пропуск")
        return summary
    try:
        for km in missing[:n]:
            if _stop_event.is_set():
                break
            try:
                if backfill_km_sync(km):
                    summary["backfilled"] += 1
            except Exception as e:
                logger.warning(f"[Backfill] КМ {km}: {e}")
            gp.SyncLockRepo.renew(_LEASE_KEY)
    finally:
        gp.SyncLockRepo.release(_LEASE_KEY)
    summary["missing_left"] = max(
        0, summary["missing_left"] - summary["backfilled"])
    with _state_lock:
        _state["backfilled_total"] += summary["backfilled"]
        _state["missing_left"] = summary["missing_left"]
    if summary["backfilled"]:
        identity.invalidate()      # долитые акты стали частью корпуса
        logger.info(f"[Backfill] Долито актов из витрины: "
                    f"{summary['backfilled']}, осталось: "
                    f"{summary['missing_left']}")
    return summary


def _loop() -> None:
    from backend.config import get_settings
    cfg = get_settings()
    _stop_event.wait(90)     # даём стартовать синкам и гидратации
    while not _stop_event.is_set():
        try:
            # Вежливость: пользователь строит карточку → эмбеддинг актов
            # фоном отбирал бы у него CPU — пропускаем цикл
            try:
                from backend.core import activity
                if activity.user_is_busy(cfg.background_idle_sec):
                    logger.info("[Backfill] Аудитор активен — "
                                "пропускаю цикл")
                    _stop_event.wait(cfg.fu_sync_interval_min * 60)
                    continue
            except Exception:
                pass
            from backend.storage import gp
            if gp.gp_enabled() and cfg.fu_backfill_enabled \
                    and gp.ActVitrinaRepo.available():
                with _state_lock:
                    _state["status"] = "running"
                    _state["last_run_at"] = datetime.now().isoformat(
                        timespec="seconds")
                backfill_once()
                with _state_lock:
                    _state["status"] = "idle"
                    _state["last_error"] = None
        except Exception as e:
            logger.error(f"[Backfill] Цикл упал: {e}")
            with _state_lock:
                _state["status"] = "error"
                _state["last_error"] = str(e)
        _stop_event.wait(cfg.fu_sync_interval_min * 60)


def start_background_backfill() -> None:
    """Догрузка актов из витрины в общую схему — работа владельца.

    Читатель эти акты уже видит: они лежат в общей схеме, и представление их
    отдаёт. Запускать у него догрузку значит гарантированно упереться в отказ
    записи на каждом акте.
    """
    global _thread
    from backend.storage import gp
    if not gp.is_owner():
        logger.info("[Backfill] Режим читателя — автодогрузка не запускается")
        return
    if _thread is not None and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_loop, name="act-backfill", daemon=True)
    _thread.start()
    logger.info("[Backfill] Фоновая автодогрузка актов из витрины запущена")


def stop_background_backfill() -> None:
    _stop_event.set()
