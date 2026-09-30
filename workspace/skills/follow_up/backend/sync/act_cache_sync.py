"""
Follow Up 2.0 — гидратация локального кэша актов из Greenplum.

Источник истины — общий корпус в GP (t_fu_act_docs / t_fu_act_chunks /
t_fu_deviations, эмбеддинги уже посчитаны). Локальные SQLite + FAISS +
BM25 — только кэш: пользователь НЕ должен ничего индексировать вручную —
свежая инсталляция наполняется сама при старте.

Механика:
  - watermark (максимальный виденный id чанка GP) хранится в
    data/index/gp_watermark.json;
  - при старте и раз в fu_sync_interval_min тянутся чанки id > watermark
    батчами: документы, которых нет локально, вставляются в SQLite
    (вместе с отклонениями) и дозаписываются в FAISS/BM25 БЕЗ
    переэмбеддинга — векторы приезжают из GP;
  - faiss_id гидратированных чанков = GP_ID_OFFSET + gp_chunk_id, чтобы
    не пересекаться с позиционными id локальной индексации;
  - документы, которые уже есть локально (например, сам их и
    проиндексировал), пропускаются — watermark всё равно двигается.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

import numpy as np

from backend.config import get_settings
from backend.core import identity

logger = logging.getLogger(__name__)

# Смещение для faiss_id гидратированных чанков: локальная индексация даёт
# позиционные id (0..N), GP-чанки получают 10M+gp_id — пространства не
# пересекаются в одном IndexIDMap2
GP_ID_OFFSET = 10_000_000

_BATCH = 2000              # меньше round-trip к GP при том же объёме
_POLITE_PAUSE_SEC = 300    # пауза гидратации, пока пользователь строит карточку


@dataclass
class ActCacheState:
    status: str = "idle"
    last_run_at: Optional[str] = None
    last_error: Optional[str] = None
    watermark: int = 0
    hydrated_docs_total: int = 0
    hydrated_chunks_total: int = 0

    def as_dict(self) -> Dict:
        return self.__dict__.copy()


_state = ActCacheState()
_state_lock = threading.Lock()
_stop_event = threading.Event()
_thread: Optional[threading.Thread] = None


def act_cache_status() -> Dict:
    with _state_lock:
        return _state.as_dict()


def _wm_path():
    return get_settings().index_dir / "gp_watermark.json"


def _read_watermark() -> int:
    p = _wm_path()
    if p.exists():
        try:
            return int(json.loads(p.read_text()).get("act_chunk_id", 0))
        except (ValueError, TypeError):
            pass
    return 0


def _write_watermark(wm: int) -> None:
    p = _wm_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"act_chunk_id": wm}))


def reset_watermark() -> None:
    """Сброс watermark: следующая гидратация перечитает весь корпус GP
    (уже имеющиеся локально документы отсеются по file_id)."""
    p = _wm_path()
    if p.exists():
        p.unlink()
    with _state_lock:
        _state.watermark = 0


def hydrate_once() -> Dict:
    """Один проход гидратации. Идемпотентен, безопасен при обрыве
    (watermark двигается после записи батча)."""
    from backend.storage import gp
    from backend.storage.database import (
        get_db, Document, Chunk, ChunkRepo, DeviationRepo)
    from backend.indexing.index_builder import append_to_indexes, flush_bm25

    def _user_is_busy() -> bool:
        """Аудитор работает — уступаем ему CPU.

        Раньше здесь смотрели только на активность КАРТОЧКИ: обычный вопрос
        шёл по тому же единственному процессору и гидратацию не тормозил.
        Теперь общий счётчик (`core/activity.py`), и «занят» — это либо
        открытый ход, либо недавнее действие аудитора.
        """
        try:
            from backend.config import get_settings
            from backend.core import activity
            return activity.user_is_busy(get_settings().background_idle_sec)
        except Exception:
            return False

    summary = {"docs": 0, "chunks": 0}
    wm = _read_watermark()
    with _state_lock:
        _state.status = "hydrating"
        _state.last_run_at = datetime.now().isoformat(timespec="seconds")
        _state.watermark = wm

    docs_meta: Optional[Dict[str, Dict]] = None   # лениво, один раз

    while not _stop_event.is_set():
        # Вежливость: пока пользователь строит карточку, ждём — эмбеддингов
        # тут нет, но запись индексов и SQLite конкурирует за CPU и диск
        while _user_is_busy() and not _stop_event.is_set():
            logger.info("[ActCache] Пользователь активен — пауза гидратации")
            _stop_event.wait(30)

        rows = gp.ActGPRepo.fetch_chunks_after(wm, limit=_BATCH)
        if not rows:
            break

        # Документ, чьи чанки пересекают границу батча, нельзя вставлять
        # с частью чанков: следующий батч его пропустит (file_id уже
        # локально) и хвост потеряется навсегда. Граничный документ
        # откладываем; если он один занимает весь батч — дотягиваем его
        # чанки целиком отдельным запросом.
        deferred: List[Dict] = []
        window_wm: Optional[int] = None
        if len(rows) == _BATCH:
            boundary_fid = rows[-1]["doc_file_id"]
            deferred = [r for r in rows if r["doc_file_id"] == boundary_fid]
            if len(deferred) == len(rows):
                # Документ занимает весь батч — дотягиваем его целиком.
                # Watermark двигаем только до конца исходного окна: чанки
                # других документов с бóльшими id не должны быть перескочены,
                # а хвост этого документа при перечитывании отсеет local_ids
                window_wm = max(int(r["id"]) for r in rows)
                rows = gp.ActGPRepo.fetch_chunks_by_doc(
                    boundary_fid, with_emb=True)
                deferred = []
            else:
                rows = [r for r in rows
                        if r["doc_file_id"] != boundary_fid]

        if docs_meta is None:
            docs_meta = {d["file_id"]: d for d in gp.ActGPRepo.list_docs()}

        with get_db() as db:
            local_ids = {fid for (fid,) in db.query(Document.file_id).all()}

        # Группируем чанки батча по документам
        by_doc: Dict[str, List[Dict]] = {}
        for r in rows:
            by_doc.setdefault(r["doc_file_id"], []).append(r)

        new_items: List[Dict] = []
        new_embs: List[List[float]] = []
        batch_docs = 0

        # Транзакция = ОДИН документ, а не батч на 2000 строк. Запись
        # сериализована общим локом (storage/writer.py: vfs=unix-none отключает
        # блокировки SQLite), и батч под этим локом стал бы стоп-краном для
        # чата: документ — это 60-150 строк и ~150 мс на NFS, батч — секунды.
        for fid, chs in by_doc.items():
            if fid in local_ids:
                continue  # уже есть локально (сам индексировал)
            with get_db() as db:
                meta = docs_meta.get(fid) or {}
                doc = Document(
                    file_id=fid,
                    filename=meta.get("filename") or (chs[0].get("filename") or fid),
                    check_id=meta.get("check_id") or chs[0].get("check_id") or "UNKNOWN",
                    topic=meta.get("topic"),
                    original_path=f"greenplum://{fid}",
                    md_path="",
                )
                db.add(doc)
                db.flush()
                chunk_rows = []
                for c in sorted(chs, key=lambda x: x["chunk_index"]):
                    fid_faiss = GP_ID_OFFSET + int(c["id"])
                    chunk_rows.append({
                        "document_id": doc.id,
                        "chunk_index": c["chunk_index"],
                        "faiss_id": fid_faiss,
                        "text": c["chunk_text"],
                        "header_path": c.get("header_path"),
                        "char_count": len(c["chunk_text"]),
                    })
                    new_items.append({"faiss_id": fid_faiss,
                                      "text": c["chunk_text"]})
                    new_embs.append(c["emb"])
                ChunkRepo.insert_bulk(db, chunk_rows)

                # Отклонения документа — чтобы реестр/аналитика работали сразу
                try:
                    devs = gp.ActGPRepo.fetch_deviations_by_doc(fid)
                    if devs:
                        DeviationRepo.insert_bulk(db, [{
                            "document_id": doc.id,
                            "check_id": d.get("check_id") or doc.check_id,
                            **{k: d.get(k) for k in (
                                "category", "description", "severity",
                                "financial_impact_rub", "affected_systems",
                                "regulation_refs", "affected_count",
                                "responsible_unit", "recommendation",
                                "source_chunk_index")},
                        } for d in devs])
                except Exception as e:
                    logger.warning(f"[ActCache] Отклонения {fid}: {e}")

                summary["docs"] += 1
                batch_docs += 1
                local_ids.add(fid)

        # Индексы: дозапись готовых векторов (без переэмбеддинга).
        # BM25 откладываем — пересоберём один раз после всех батчей
        if new_items:
            append_to_indexes(new_items,
                              np.array(new_embs, dtype=np.float32),
                              defer_bm25=True)
            summary["chunks"] += len(new_items)

        # Watermark: перед отложенным документом (его чанки перечитаются
        # в следующем батче и обработаются целиком), иначе — конец батча
        if deferred:
            wm = min(int(r["id"]) for r in deferred) - 1
        elif window_wm is not None:
            wm = window_wm
        else:
            wm = max(int(r["id"]) for r in rows)
        _write_watermark(wm)
        with _state_lock:
            _state.watermark = wm
            _state.hydrated_docs_total += batch_docs
            _state.hydrated_chunks_total += len(new_items)

    # Одна пересборка BM25 на весь прогон вместо одной на батч
    try:
        flush_bm25()
    except Exception as e:
        logger.error(f"[ActCache] Пересборка BM25: {e}")

    with _state_lock:
        _state.status = "idle"
        _state.last_error = None
    if summary["docs"]:
        # Справочник проверок кэширован на identity_registry_ttl_sec: без
        # сброса свежегидратированный акт до минуты считался бы «в витрине
        # есть, в корпусе нет»
        identity.invalidate()
        logger.info(f"[ActCache] Гидратировано из GP: {summary['docs']} докум., "
                    f"{summary['chunks']} чанков (watermark={wm})")
    return summary


def _loop() -> None:
    cfg = get_settings()
    _stop_event.wait(25)   # даём приложению стартовать
    while not _stop_event.is_set():
        try:
            from backend.storage import gp
            if gp.gp_enabled():
                hydrate_once()
        except Exception as e:
            logger.error(f"[ActCache] Цикл гидратации упал: {e}")
            with _state_lock:
                _state.status = "error"
                _state.last_error = str(e)
        _stop_event.wait(cfg.fu_sync_interval_min * 60)


def start_background_hydration() -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_loop, name="act-cache-sync", daemon=True)
    _thread.start()
    logger.info("[ActCache] Фоновая гидратация корпуса актов из GP запущена")


def stop_background_hydration() -> None:
    _stop_event.set()
