"""
Follow Up 2.0 — FAISS + BM25 Index Builder.

Исправлена несовместимость API (SentenceTransformer vs FlagEmbedding).
Метаданные хранятся в SQLite (не в parquet).
"""
from __future__ import annotations

import logging
import pickle
import re
from pathlib import Path
from typing import Callable, Dict, List, Optional

from backend.indexing.lexicon import LEXICON_VERSION

import os
import threading

import faiss
import numpy as np
from rank_bm25 import BM25Okapi

from backend.config import get_settings
from backend.indexing.embedder import embed_texts

logger = logging.getLogger(__name__)

# Гонка записи (гидратация + бэкофилл + пайплайн одновременно) била
# индексы («pickle data was truncated» на проде): все операции
# чтения-модификации-записи идут под глобальным локом, файлы пишутся
# атомарно (tmp + os.replace)
_write_lock = threading.RLock()   # RLock: самолечение BM25 вызывается и из-под лока


def _atomic_write_faiss(index, path: Path) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    faiss.write_index(index, str(tmp))
    os.replace(tmp, path)


def _atomic_write_pickle(obj, path: Path) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as f:
        pickle.dump(obj, f)
    os.replace(tmp, path)


# ──────────────────────────────────────────────────────────────────
# BM25 tokenizer
# ──────────────────────────────────────────────────────────────────

def tokenize(text: str) -> List[str]:
    """ЕДИНСТВЕННЫЙ токенизатор корпуса — `indexing/lexicon.py`.

    Прежде их было два: этот и его дословная копия `retrieval._tokenize` с
    комментарием «копируем для консистентности». Копия — обещание, которое
    некому выполнять: правка одного разъехалась бы с другим МОЛЧА, и поиск
    начал бы терять документы без единой ошибки в логах.
    """
    from backend.indexing.lexicon import tokenize as _lex
    return _lex(text)


# ──────────────────────────────────────────────────────────────────
# Build
# ──────────────────────────────────────────────────────────────────

def build_indexes(
    chunks: List[Dict],
    progress_callback: Optional[Callable[[str], None]] = None,
) -> None:
    """
    Строит FAISS и BM25 индексы по списку чанков.
    Каждому чанку присваивается стабильный faiss_id.

    FAISS = IndexIDMap2(IndexFlatIP): id задаются явно через add_with_ids,
    что позволяет инкрементально добавлять чанки (append_to_indexes) без
    полного ребилда — например, при подтягивании общего корпуса из GP.
    """
    cfg = get_settings()
    cfg.index_dir.mkdir(parents=True, exist_ok=True)

    def log(msg: str):
        logger.info(msg)
        if progress_callback:
            progress_callback(msg)

    texts = [c["text"] for c in chunks]
    n = len(texts)
    log(f"[Index] Всего чанков: {n}")

    # ── FAISS ──────────────────────────────────────────────────────
    log("[Index] Вычисление эмбеддингов BGE-M3...")
    embeddings = embed_texts(texts, normalize=True)  # float32 [N, dim]
    dim = embeddings.shape[1]
    log(f"[Index] Размерность эмбеддингов: {dim}")

    # Присваиваем стабильные id (при полном ребилде — просто позиции,
    # но уже переданные через IDMap: дальше можно добавлять любые id)
    for i, chunk in enumerate(chunks):
        chunk["faiss_id"] = i
    ids = np.array([c["faiss_id"] for c in chunks], dtype=np.int64)

    index = faiss.IndexIDMap2(faiss.IndexFlatIP(dim))
    faiss.normalize_L2(embeddings)
    index.add_with_ids(embeddings, ids)

    log("[Index] Построение BM25...")
    tokenized = [tokenize(t) for t in texts]
    bm25 = BM25Okapi(tokenized)

    with _write_lock:
        _atomic_write_faiss(index, cfg.faiss_file)
        log(f"[Index] FAISS сохранён: {cfg.faiss_file}")
        # ids хранятся параллельно corpus: позиция BM25 → faiss_id
        _atomic_write_pickle({"bm25": bm25, "corpus": texts,
                              "tokenized": tokenized,
                              "lexicon_version": LEXICON_VERSION,
                              "ids": [int(i) for i in ids]}, cfg.bm25_file)
        log(f"[Index] BM25 сохранён: {cfg.bm25_file}")
        reset_index_cache()

    log("[Index] Индексы построены успешно.")


def append_to_indexes(
    new_items: List[Dict],
    embeddings: Optional[np.ndarray] = None,
    defer_bm25: bool = False,
) -> None:
    """
    Инкрементально добавляет чанки в существующие индексы.

    new_items: [{"faiss_id": int (стабильный, уникальный), "text": str}, ...]
    embeddings: готовые нормализованные векторы [N, dim]; None → посчитать.
    defer_bm25: не пересобирать BM25 сейчас — только накопить чанки.
        Массовая гидратация вызывает append_to_indexes десятки раз подряд, а
        BM25Okapi не умеет дозаписи: пересборка на каждый батч даёт O(N²)
        (прод: 27 пересборок, 3 минуты CPU). С defer_bm25=True вызывающий
        обязан в конце вызвать flush_bm25().

    FAISS дозаписывается через add_with_ids — это дёшево и делается всегда.
    """
    if not new_items:
        return
    cfg = get_settings()
    cfg.index_dir.mkdir(parents=True, exist_ok=True)

    if embeddings is None:
        embeddings = embed_texts([c["text"] for c in new_items], normalize=True)
    embeddings = np.asarray(embeddings, dtype=np.float32)
    faiss.normalize_L2(embeddings)
    ids = np.array([int(c["faiss_id"]) for c in new_items], dtype=np.int64)

    # Чтение-модификация-запись целиком под локом: конкурентные дозаписи
    # (гидратация + бэкофилл) теряли чанки и били пиклы
    with _write_lock:
        # ── FAISS ──
        try:
            index = load_faiss()
        except FileNotFoundError:
            # Свежая инсталляция: гидратация из GP создаёт индекс с нуля —
            # пользователь НЕ обязан запускать индексацию вручную
            index = faiss.IndexIDMap2(faiss.IndexFlatIP(embeddings.shape[1]))
            logger.info("[Index] Локального FAISS не было — создаю новый (кэш из GP)")
        if not isinstance(index, faiss.IndexIDMap2):
            # Легаси-формат (плоский, позиционные id) — мигрируем на месте,
            # сохраняя старые id как позиции
            legacy_n = index.ntotal
            vecs = index.reconstruct_n(0, legacy_n)
            migrated = faiss.IndexIDMap2(faiss.IndexFlatIP(index.d))
            if legacy_n:
                migrated.add_with_ids(vecs, np.arange(legacy_n, dtype=np.int64))
            index = migrated
            logger.info(f"[Index] Легаси-FAISS мигрирован в IndexIDMap2 "
                        f"({legacy_n} векторов, id сохранены)")
        index.add_with_ids(embeddings, ids)
        _atomic_write_faiss(index, cfg.faiss_file)

        # ── BM25 ──
        if defer_bm25:
            # Копим до flush_bm25(): пересборка модели — самая дорогая часть
            _pending_bm25.extend({"faiss_id": int(c["faiss_id"]),
                                  "text": c["text"]} for c in new_items)
            logger.info(f"[Index] FAISS +{len(new_items)} чанков; "
                        f"BM25 отложен (в очереди {len(_pending_bm25)})")
            return
        _rebuild_bm25_locked([{"faiss_id": int(c["faiss_id"]), "text": c["text"]}
                              for c in new_items])
        reset_index_cache()
    logger.info(f"[Index] Инкрементально добавлено чанков: {len(new_items)}")


# Отложенные для BM25 чанки (защищены тем же _write_lock)
_pending_bm25: List[Dict] = []


def _rebuild_bm25_locked(items: List[Dict]) -> None:
    """Пересборка BM25 с добавлением items. Вызывать ТОЛЬКО под _write_lock."""
    cfg = get_settings()
    try:
        data = load_bm25()
    except FileNotFoundError:
        data = {"corpus": [], "tokenized": [], "ids": []}
    corpus = data["corpus"] + [c["text"] for c in items]
    tokenized = data["tokenized"] + [tokenize(c["text"]) for c in items]
    old_ids = data.get("ids") or list(range(len(data["corpus"])))
    all_ids = old_ids + [int(c["faiss_id"]) for c in items]
    _atomic_write_pickle({"bm25": BM25Okapi(tokenized), "corpus": corpus,
                          "tokenized": tokenized, "ids": all_ids,
                          "lexicon_version": LEXICON_VERSION},
                         cfg.bm25_file)


def flush_bm25() -> int:
    """Досборка BM25 из накопленного defer_bm25. Возвращает число чанков.
    Идемпотентна: без очереди ничего не делает."""
    with _write_lock:
        if not _pending_bm25:
            return 0
        items = list(_pending_bm25)
        _pending_bm25.clear()
        _rebuild_bm25_locked(items)
        reset_index_cache()
    logger.info(f"[Index] BM25 пересобран один раз: +{len(items)} чанков")
    return len(items)


# ──────────────────────────────────────────────────────────────────
# Load
# ──────────────────────────────────────────────────────────────────

_faiss_index: Optional[faiss.Index] = None
_bm25_data: Optional[Dict] = None


def load_faiss() -> faiss.Index:
    global _faiss_index
    if _faiss_index is None:
        cfg = get_settings()
        if not cfg.faiss_file.exists():
            raise FileNotFoundError(f"FAISS индекс не найден: {cfg.faiss_file}")
        _faiss_index = faiss.read_index(str(cfg.faiss_file))
        logger.info(f"[Index] FAISS загружен: {_faiss_index.ntotal} векторов")
    return _faiss_index


def rebuild_bm25_from_db() -> Dict:
    """Самолечение: пересборка BM25 из чанков SQLite (битый пикл после
    аварийного завершения не должен убивать поиск до ручного вмешательства)."""
    from backend.storage.database import get_db, Chunk
    cfg = get_settings()
    with get_db() as db:
        rows = (db.query(Chunk.faiss_id, Chunk.text)
                .filter(Chunk.faiss_id.isnot(None)).all())
    if not rows:
        raise FileNotFoundError("BM25: нечего пересобирать (чанков нет)")
    corpus = [t for (_, t) in rows]
    ids = [int(fid) for (fid, _) in rows]
    tokenized = [tokenize(t) for t in corpus]
    data = {"bm25": BM25Okapi(tokenized), "corpus": corpus,
            "tokenized": tokenized, "ids": ids,
            "lexicon_version": LEXICON_VERSION}
    with _write_lock:
        _atomic_write_pickle(data, cfg.bm25_file)
        global _bm25_data
        _bm25_data = data      # тёплая подмена: без неё процесс продолжит
                               # искать по старому пиклу до перезапуска
    try:
        from backend.core.critic import invalidate_idf_cache
        invalidate_idf_cache()
    except Exception:
        pass
    logger.info(f"[Index] BM25 пересобран из SQLite: {len(corpus)} чанков, "
                f"лексикон v{LEXICON_VERSION}")
    return {"chunks": len(corpus)}


def load_bm25() -> Dict:
    global _bm25_data
    if _bm25_data is None:
        cfg = get_settings()
        if not cfg.bm25_file.exists():
            raise FileNotFoundError(f"BM25 индекс не найден: {cfg.bm25_file}")
        try:
            with open(cfg.bm25_file, "rb") as f:
                _bm25_data = pickle.load(f)
        except Exception as e:
            logger.error(f"[Index] BM25 повреждён ({e}) — пересобираю из SQLite")
            _bm25_data = rebuild_bm25_from_db()
        logger.info(f"[Index] BM25 загружен: {len(_bm25_data['corpus'])} документов")
    return _bm25_data


def reset_index_cache():
    """Сбрасывает кэш индексов (после перестройки)."""
    global _faiss_index, _bm25_data
    _faiss_index = None
    _bm25_data = None
