"""Follow Up 2.0 — производные артефакты корпуса.

Два индекса, которые не нужны для ответа «в лоб», но без которых два режима
поиска работают неприемлемо:

**Обратный индекс упоминаний.** Без него `search_entity` сканирует все чанки на
каждый вопрос: на шести локальных актах незаметно, на 214 актах прод-корпуса —
секунды единственного процессора на каждый запрос, и это при одном ядре на всё.

**Эмбеддинги описаний отклонений.** Фасетный поиск по многословной теме
(«кредитные карты») почти всегда пуст: `ILIKE '%кредитные карты%'` требует
дословного совпадения, а в акте написано «операции по картам для детей».
Отклонений на порядок меньше, чем чанков (замерено на проде: ~5 000 против
24 596), поэтому эмбеддинги считаются за минуты, а не за полчаса.

Оба строятся ОФЛАЙН и порциями, с уступкой процессора аудитору: фоновая работа
не должна отбирать ядро у человека, который ждёт ответ.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_state: Dict = {"entity": {}, "dev_emb": {}}


def _cfg():
    from backend.config import get_settings
    return get_settings()


def _dev_emb_path() -> Path:
    return _cfg().index_dir / "deviation_emb.npz"


def _yield_to_user(stage: str) -> None:
    """Уступить процессор, пока аудитор работает.

    Не «пропустить работу», а подождать: производные индексы никуда не спешат,
    а ход аудитора идёт по тому же единственному ядру.
    """
    from backend.core import activity
    idle_need = _cfg().background_idle_sec
    waited = 0.0
    while activity.user_is_busy(idle_need) and waited < 300:
        time.sleep(1.0)
        waited += 1.0
    if waited:
        logger.debug(f"[derived] {stage}: уступал аудитору {waited:.0f} с")


# ──────────────────────────────────────────────────────────────────
# Обратный индекс упоминаний
# ──────────────────────────────────────────────────────────────────

def build_entity_index(progress: Optional[Callable[[str], None]] = None,
                       batch_docs: int = 20) -> Dict:
    """Перестраивает `entity_mentions` из чанков корпуса.

    Транзакция = один документ (см. storage/writer.py): батч под общим локом
    записи превратил бы построение индекса в стоп-кран для чата.
    """
    from backend.core.tools.facts import (EXTRACTORS, extract_entities,
                                          _looks_like_requisites)
    from backend.storage.database import (Chunk, Document, EntityMention,
                                          get_db)

    say = progress or (lambda s: logger.info(f"[derived] {s}"))
    t0 = time.monotonic()
    with get_db() as db:
        doc_ids = [d[0] for d in db.query(Document.id).all()]
    say(f"Индекс упоминаний: {len(doc_ids)} документов")

    total_mentions, done_docs = 0, 0
    with get_db() as db:
        db.query(EntityMention).delete()

    for i, doc_id in enumerate(doc_ids, 1):
        if i % batch_docs == 0:
            _yield_to_user("entity_index")
        with get_db() as db:
            doc = db.query(Document).filter(Document.id == doc_id).first()
            if doc is None:
                continue
            rows = (db.query(Chunk.chunk_index, Chunk.text, Chunk.header_path)
                    .filter(Chunk.document_id == doc_id).all())
            batch: List[Dict] = []
            for idx, body, header in rows:
                for ent in extract_entities(body or ""):
                    pos = ent["start"]
                    window = (body or "")[max(0, pos - 200): pos + 250].strip()
                    batch.append({
                        "kind": ent["kind"], "norm": ent["norm"],
                        "value": ent["value"], "check_id": doc.check_id,
                        "document_id": doc_id, "chunk_index": idx,
                        "header_path": header or "",
                        "where": ("requisites"
                                  if _looks_like_requisites(header, window)
                                  else "case_text"),
                        "quote": window,
                    })
            if batch:
                db.bulk_insert_mappings(EntityMention, batch)
                total_mentions += len(batch)
        done_docs += 1
        if done_docs % 50 == 0:
            say(f"  {done_docs}/{len(doc_ids)} документов, "
                f"{total_mentions} упоминаний")

    out = {"documents": done_docs, "mentions": total_mentions,
           "elapsed_sec": round(time.monotonic() - t0, 1),
           "by_kind": _mentions_by_kind()}
    with _lock:
        _state["entity"] = {**out, "built_at": time.time()}
    say(f"Индекс упоминаний готов: {total_mentions} за {out['elapsed_sec']} с")
    return out


def _mentions_by_kind() -> Dict[str, int]:
    from sqlalchemy import func
    from backend.storage.database import EntityMention, get_db
    with get_db() as db:
        return {k: n for k, n in
                db.query(EntityMention.kind, func.count(EntityMention.id))
                  .group_by(EntityMention.kind).all()}


# ──────────────────────────────────────────────────────────────────
# Эмбеддинги описаний отклонений
# ──────────────────────────────────────────────────────────────────

def build_deviation_embeddings(progress: Optional[Callable[[str], None]] = None,
                               batch: int = 64) -> Dict:
    """Считает эмбеддинги описаний отклонений и кладёт рядом с индексом.

    Отдельный файл, а не FAISS корпуса: пространство другое (описание
    отклонения ≠ фрагмент акта), и подмешивать их в один индекс значит портить
    оба поиска.
    """
    from backend.indexing.embedder import embed_texts
    from backend.storage.database import Deviation, get_db

    say = progress or (lambda s: logger.info(f"[derived] {s}"))
    t0 = time.monotonic()
    with get_db() as db:
        rows = (db.query(Deviation.id, Deviation.description)
                .filter(Deviation.description.isnot(None)).all())
    rows = [(int(i), d) for i, d in rows if (d or "").strip()]
    if not rows:
        say("Отклонений нет — эмбеддинги не нужны")
        return {"count": 0}

    say(f"Эмбеддинги отклонений: {len(rows)} описаний")
    ids = np.array([i for i, _ in rows], dtype=np.int64)
    vecs: List[np.ndarray] = []
    for start in range(0, len(rows), batch):
        _yield_to_user("dev_emb")
        part = [d[:1200] for _, d in rows[start:start + batch]]
        vecs.append(embed_texts(part, normalize=True))
        if (start // batch) % 10 == 0:
            say(f"  {min(start + batch, len(rows))}/{len(rows)}")
    mat = np.vstack(vecs).astype(np.float32)

    # Через файловый объект, а не по имени: np.savez сам дописывает «.npz»,
    # если имя на него не кончается, и атомарная замена искала бы файл,
    # которого нет.
    path = _dev_emb_path()
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as fh:
        np.savez(fh, ids=ids, emb=mat)
    tmp.replace(path)

    out = {"count": len(rows), "dim": int(mat.shape[1]),
           "elapsed_sec": round(time.monotonic() - t0, 1), "path": str(path)}
    with _lock:
        _state["dev_emb"] = {**out, "built_at": time.time()}
    say(f"Эмбеддинги готовы: {out['count']} за {out['elapsed_sec']} с")
    return out


_dev_cache: Optional[Dict] = None


def load_deviation_embeddings() -> Optional[Dict]:
    global _dev_cache
    if _dev_cache is not None:
        return _dev_cache
    path = _dev_emb_path()
    if not path.exists():
        return None
    try:
        data = np.load(path)
        _dev_cache = {"ids": data["ids"], "emb": data["emb"]}
        return _dev_cache
    except Exception as e:
        logger.warning(f"[derived] Эмбеддинги отклонений не читаются: {e}")
        return None


def invalidate() -> None:
    global _dev_cache
    _dev_cache = None


# ──────────────────────────────────────────────────────────────────
# Состояние
# ──────────────────────────────────────────────────────────────────

def status() -> Dict:
    from backend.storage.database import EntityMention, get_db
    out: Dict = {}
    try:
        with get_db() as db:
            n = db.query(EntityMention).count()
        out["entity_index"] = {"mentions": n, "by_kind": _mentions_by_kind(),
                               "built": n > 0}
    except Exception as e:
        out["entity_index"] = {"error": str(e)}
    dev = load_deviation_embeddings()
    out["deviation_embeddings"] = (
        {"built": True, "count": int(len(dev["ids"]))} if dev
        else {"built": False,
              "hint": "семантика по отклонениям недоступна — фасетный поиск "
                      "по многословной теме будет пустым"})
    with _lock:
        out["last_runs"] = dict(_state)
    return out
