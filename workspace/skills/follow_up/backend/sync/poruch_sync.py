"""
Follow Up 2.0 — ленивый синк витрины поручений (серверлесс).

Сервера нет: любой клиент, зашедший в инструмент, отрабатывает дельту —
незаметно для пользователя, под lease-локом в GP (advisory locks в GP
не поддерживаются).

Цикл:
  1. Дельта-детекция: строки витрины хэшируются на клиенте и сверяются
     с теневиком t_fu_poruch_shadow (витрина обновляется in-place без
     modify_dt — только хэш ловит апдейты).
  2. pending > 0 → попытка захвата lease 'poruch_emb'.
  3. Батчи: чанкование текстовых полей → bge-m3 (CPU) → замена чанков
     в t_fu_poruch_chunks → shadow_mark_done. Lease продлевается после
     каждого батча.
  4. Release. Прерванный клиент не проблема: lease истечёт, следующий
     продолжит с pending-строк. Идемпотентно (chunks_replace).

Объёмы (разведка): 311 строк, тексты ≤4401 симв. Полный первичный
прогон — минуты на CPU.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from backend.config import get_settings

logger = logging.getLogger(__name__)

LOCK_KEY = "poruch_emb"

# Текстовые поля витрины, которые чанкуем и эмбеддим
_TEXT_FIELDS = ("problem", "assignment_", "actions")


# ──────────────────────────────────────────────────────────────────
# Состояние (для /api/admin/sync/status)
# ──────────────────────────────────────────────────────────────────

@dataclass
class SyncState:
    status: str = "idle"            # idle | checking | embedding | done | error
    last_run_at: Optional[str] = None
    last_error: Optional[str] = None
    pending: int = 0
    processed_total: int = 0
    lease_held: bool = False

    def as_dict(self) -> Dict:
        return {
            "status": self.status,
            "last_run_at": self.last_run_at,
            "last_error": self.last_error,
            "pending": self.pending,
            "processed_total": self.processed_total,
            "lease_held": self.lease_held,
        }


_state = SyncState()
_state_lock = threading.Lock()
_stop_event = threading.Event()
_thread: Optional[threading.Thread] = None


def sync_status() -> Dict:
    with _state_lock:
        return _state.as_dict()


def _set(**kwargs) -> None:
    with _state_lock:
        for k, v in kwargs.items():
            setattr(_state, k, v)


def _inc(processed: int) -> None:
    """Инкремент счётчиков атомарно (read-modify-write под локом)."""
    with _state_lock:
        _state.processed_total += processed
        _state.pending = max(0, _state.pending - processed)


# ──────────────────────────────────────────────────────────────────
# Чанкование коротких текстов поручений
# ──────────────────────────────────────────────────────────────────

def chunk_poruch_text(text: str, max_chars: Optional[int] = None,
                      overlap: Optional[int] = None) -> List[str]:
    """
    Поля короткие (max 4401 симв.): целиком, если влезает; иначе режем
    по абзацам/предложениям с перекрытием.
    """
    cfg = get_settings()
    max_c = max(200, max_chars or cfg.poruch_chunk_chars)
    ov = overlap if overlap is not None else cfg.poruch_chunk_overlap
    # overlap >= max_chars зацикливает жёсткую нарезку — клампим
    ov = max(0, min(ov, max_c // 4))
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_c:
        return [text]

    # Режем по абзацам, добираем до max_c
    parts: List[str] = []
    current = ""
    for para in text.split("\n"):
        candidate = (current + "\n" + para).strip() if current else para.strip()
        if len(candidate) <= max_c:
            current = candidate
            continue
        if current:
            parts.append(current)
            # перекрытие: хвост предыдущего чанка
            tail = current[-ov:] if ov and len(current) > ov else ""
            current = (tail + " " + para).strip()
        else:
            current = para.strip()
        # абзац сам длиннее max_c — жёсткая нарезка
        while len(current) > max_c:
            parts.append(current[:max_c])
            current = current[max_c - ov:] if ov else current[max_c:]
    if current:
        parts.append(current)
    return parts


def build_poruch_chunks(row: Dict) -> List[Dict]:
    """Строка витрины → список чанков (без эмбеддингов)."""
    out: List[Dict] = []
    for fld in _TEXT_FIELDS:
        for i, piece in enumerate(chunk_poruch_text(row.get(fld) or "")):
            out.append({
                "poruch_key": row["poruch_key"],
                "km_id": row["km_id"],
                "field_src": fld,
                "chunk_idx": i,
                "chunk_text": piece,
            })
    return out


# ──────────────────────────────────────────────────────────────────
# Один цикл синка
# ──────────────────────────────────────────────────────────────────

def run_sync_once() -> Dict:
    """
    Полный цикл: дельта → lease → эмбеддинг батчами → release.
    Возвращает сводку. Безопасно вызывать конкурентно с других клиентов.
    """
    from backend.storage import gp
    cfg = get_settings()
    summary = {"new_or_changed": 0, "embedded": 0, "lease_acquired": False}

    # Проверка режима — ДО дельта-детекции: `shadow_upsert_pending` ниже уже
    # пишет, и у читателя проход упал бы там, успев сходить за всей витриной.
    if not gp.is_owner():
        logger.info("[Sync] Режим читателя — запись в общую схему не наша")
        _set(status="idle", lease_held=False)
        return summary

    _set(status="checking", last_run_at=datetime.now().isoformat(timespec="seconds"))

    # 1. Дельта-детекция (дёшево, без лока).
    # Дубли poruch_key во вью (одинаковые km/doc/assignment) схлопываем
    # детерминированно — иначе строка вечно мигала бы pending.
    raw_rows = gp.PoruchRepo.fetch_view_rows()
    rows_by_key: Dict[str, Dict] = {}
    for r in sorted(raw_rows, key=lambda x: x["row_hash"]):
        rows_by_key[r["poruch_key"]] = r
    view_rows = list(rows_by_key.values())

    shadow = gp.PoruchRepo.shadow_map()
    changed = [
        r for r in view_rows
        if r["poruch_key"] not in shadow
        or shadow[r["poruch_key"]]["row_hash"] != r["row_hash"]
    ]
    if changed:
        gp.PoruchRepo.shadow_upsert_pending(changed)
    summary["new_or_changed"] = len(changed)

    pending_keys = gp.PoruchRepo.shadow_pending_keys(limit=100_000)
    _set(pending=len(pending_keys))
    if not pending_keys:
        _set(status="idle", lease_held=False)
        return summary

    # 2. Lease
    if not gp.SyncLockRepo.try_acquire(LOCK_KEY):
        logger.info("[Sync] Lease занят другим клиентом — выходим, дельта учтена")
        _set(status="idle", lease_held=False)
        return summary

    # 2б. Чистка сирот — ТОЛЬКО под lease. Раньше она шла до него, то есть
    # каждый процесс каждого аудитора выполнял разрушительный DELETE по общей
    # базе без всякого лока. И только при непустой витрине: пустая означает
    # сбой источника, а не «все поручения удалены».
    if view_rows:
        try:
            gp.PoruchRepo.shadow_cleanup_orphans(
                [r["poruch_key"] for r in view_rows])
        except Exception as e:
            logger.warning(f"[Sync] Чистка сирот не удалась: {e}")
    else:
        logger.error("[Sync] Витрина вернула 0 строк — чистка пропущена")
    summary["lease_acquired"] = True
    _set(status="embedding", lease_held=True)

    # 3. Обработка батчами
    try:
        from backend.indexing.embedder import embed_texts
        # детерминированный порядок (set давал случайный)
        batch_keys = sorted(k for k in set(pending_keys) if k in rows_by_key)

        for start in range(0, len(batch_keys), cfg.fu_sync_batch):
            if _stop_event.is_set():
                logger.info("[Sync] Остановка по stop_event")
                break
            # Продлеваем lease ДО записи батча: если он истёк и перехвачен
            # другим клиентом — не пишем поверх его работы, выходим
            if not gp.SyncLockRepo.renew(LOCK_KEY):
                logger.warning("[Sync] Lease потерян (истёк/перехвачен) — "
                               "останавливаю обработку, остаток доделает владелец")
                break
            batch = batch_keys[start:start + cfg.fu_sync_batch]

            # чанкуем весь батч, эмбеддим одним вызовом
            all_chunks: List[Dict] = []
            for k in batch:
                all_chunks.extend(build_poruch_chunks(rows_by_key[k]))
            if all_chunks:
                embs = embed_texts([c["chunk_text"] for c in all_chunks],
                                   normalize=True)
                for c, e in zip(all_chunks, embs):
                    c["emb"] = [float(x) for x in e]

            # запись по-поручённо (идемпотентная замена)
            by_key: Dict[str, List[Dict]] = {}
            for c in all_chunks:
                by_key.setdefault(c["poruch_key"], []).append(c)
            for k in batch:
                gp.PoruchRepo.chunks_replace(k, by_key.get(k, []))

            # done только если row_hash не изменился с момента нашего
            # снапшота (конкурент мог пере-пометить pending с новым хэшем)
            gp.PoruchRepo.shadow_mark_done(
                [(k, rows_by_key[k]["row_hash"]) for k in batch])
            summary["embedded"] += len(batch)
            _inc(len(batch))

        if summary["embedded"]:
            # Витрина изменилась — сбрасываем in-memory кэш карточек
            try:
                from backend.agents.execution_control import (
                    invalidate_registry_cache)
                invalidate_registry_cache()
            except Exception:
                pass

        _set(status="done", last_error=None)
    except Exception as e:
        logger.error(f"[Sync] Ошибка цикла: {e}", exc_info=True)
        _set(status="error", last_error=str(e))
        raise
    finally:
        try:
            gp.SyncLockRepo.release(LOCK_KEY)
        except Exception as e:
            logger.warning(f"[Sync] release lease: {e}")
        _set(lease_held=False)

    return summary


# ──────────────────────────────────────────────────────────────────
# Фоновый поток
# ──────────────────────────────────────────────────────────────────

def _loop() -> None:
    cfg = get_settings()
    # небольшая задержка на старт приложения (модели грузятся)
    _stop_event.wait(20)
    while not _stop_event.is_set():
        try:
            from backend.storage import gp
            if gp.gp_enabled():
                run_sync_once()
        except Exception as e:
            logger.error(f"[Sync] Фоновый цикл упал: {e}")
            _set(status="error", last_error=str(e))
        _stop_event.wait(cfg.fu_sync_interval_min * 60)


def start_background_sync() -> None:
    """Запуск фонового синка (вызывается из lifespan при gp_enabled).

    Синк ПИШЕТ в общую схему (теневик дельт, чистка сирот). У аудитора без
    прав на неё каждая попытка кончится отказом базы, а в логе будет стена
    ошибок, за которой не видно настоящих. Читатель получает готовые данные
    через представления — синкать ему нечего.
    """
    global _thread
    from backend.storage import gp
    if not gp.is_owner():
        logger.info("[Sync] Режим читателя — синк витрины не запускается "
                    "(данные приходят из общей схемы готовыми)")
        return
    if _thread is not None and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_loop, name="poruch-sync", daemon=True)
    _thread.start()
    logger.info("[Sync] Фоновый синк витрины поручений запущен")


def stop_background_sync() -> None:
    _stop_event.set()
