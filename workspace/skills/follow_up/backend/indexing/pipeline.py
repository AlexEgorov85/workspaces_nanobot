"""
Follow Up 2.0 — Full Indexing Pipeline.

Оркестрирует полный процесс:
  1. Конвертация .docx → .md
  2. Умный чанкинг
  3. Построение FAISS + BM25 индексов
  4. Сохранение метаданных в SQLite
  5. LLM-извлечение отклонений
"""
from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Callable, Dict, List, Optional

from backend.config import get_settings
from backend.indexing.chunker import chunk_all_documents
from backend.indexing.converter import process_documents
from backend.indexing.extractor import extract_deviations_for_document
from backend.indexing.index_builder import build_indexes, reset_index_cache
from backend.storage.database import (
    ChunkRepo, DeviationRepo, DocumentRepo, init_db, get_db,
)

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────
# Pipeline State
# ──────────────────────────────────────────────────────────────────

class PipelineStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


@dataclass
class PipelineState:
    status: PipelineStatus = PipelineStatus.IDLE
    current_step: str = ""
    progress_pct: int = 0
    logs: List[str] = field(default_factory=list)
    error: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    def log(self, msg: str):
        logger.info(msg)
        self.logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
        # Держим только последние 200 строк
        if len(self.logs) > 200:
            self.logs = self.logs[-200:]


# Глобальное состояние пайплайна
_pipeline_state = PipelineState()
_pipeline_lock = threading.Lock()


def get_pipeline_state() -> PipelineState:
    return _pipeline_state


# ──────────────────────────────────────────────────────────────────
# Pipeline Steps
# ──────────────────────────────────────────────────────────────────

def _step_convert(state: PipelineState) -> List[Dict]:
    state.current_step = "Конвертация документов"
    state.progress_pct = 5
    state.log("=== Шаг 1: Конвертация .docx → .md ===")

    md_metas = process_documents(progress_callback=state.log)
    state.log(f"Конвертировано файлов: {len(md_metas)}")
    return md_metas


def _step_chunk(state: PipelineState, md_metas: List[Dict]) -> List[Dict]:
    state.current_step = "Чанкинг документов"
    state.progress_pct = 20
    state.log("=== Шаг 2: Умный чанкинг ===")

    chunks = chunk_all_documents(md_metas)
    state.log(f"Всего чанков: {len(chunks)}")

    # Статистика
    lengths = [c["char_count"] for c in chunks if c.get("char_count")]
    if lengths:
        avg = sum(lengths) / len(lengths)
        state.log(f"Средняя длина чанка: {avg:.0f} символов")
        state.log(f"Мин: {min(lengths)}, Макс: {max(lengths)}")

    return chunks


def _step_save_to_db(state: PipelineState, md_metas: List[Dict], chunks: List[Dict]) -> Dict:
    """Сохраняет документы и чанки в SQLite, возвращает mapping file_id→doc."""
    state.current_step = "Сохранение метаданных"
    state.progress_pct = 35
    state.log("=== Шаг 3: Сохранение в SQLite ===")

    init_db()
    file_id_to_doc: Dict[str, Dict] = {}

    with get_db() as db:
        # Upsert документов (file_id теперь детерминированный → idempotent)
        for meta in md_metas:
            doc = DocumentRepo.upsert(db, meta)
            file_id_to_doc[meta["file_id"]] = {
                "document_id": doc.id,
                "check_id": doc.check_id,
                "chunks": [],
            }

        # Удаляем старые чанки: FAISS пересобран — все faiss_id изменились.
        # Без этого в БД остались бы "зомби"-чанки со старыми faiss_id.
        doc_ids = [d["document_id"] for d in file_id_to_doc.values()]
        deleted = ChunkRepo.delete_by_document_ids(db, doc_ids)
        if deleted:
            state.log(f"Удалено устаревших чанков: {deleted}")

        # Группируем и вставляем новые чанки
        chunk_rows = []
        for chunk in chunks:
            doc_data = file_id_to_doc.get(chunk["file_id"])
            if not doc_data:
                continue
            chunk_rows.append({
                "document_id": doc_data["document_id"],
                "chunk_index": chunk["chunk_index"],
                "faiss_id": chunk.get("faiss_id"),
                "text": chunk["text"],
                "header_path": chunk.get("header_path", ""),
                "char_count": chunk.get("char_count", len(chunk["text"])),
            })
            doc_data["chunks"].append(chunk)

        if chunk_rows:
            ChunkRepo.insert_bulk(db, chunk_rows)
            state.log(f"Сохранено чанков: {len(chunk_rows)}")

    return file_id_to_doc


def _step_build_indexes(state: PipelineState, chunks: List[Dict]):
    state.current_step = "Построение индексов"
    state.progress_pct = 50
    state.log("=== Шаг 4: Построение FAISS + BM25 ===")

    build_indexes(chunks, progress_callback=state.log)
    reset_index_cache()
    state.log("Индексы построены и перезагружены")


def _step_extract_deviations(state: PipelineState, file_id_to_doc: Dict):
    state.current_step = "Извлечение отклонений (LLM)"
    state.progress_pct = 75
    state.log("=== Шаг 5: LLM-извлечение отклонений ===")

    all_devs = []
    docs = list(file_id_to_doc.values())
    total = len(docs)
    skipped = 0

    for i, doc_data in enumerate(docs):
        pct = 75 + int(20 * i / max(total, 1))
        state.progress_pct = pct

        # Пропускаем документы, для которых отклонения уже извлечены —
        # экономит дорогостоящие GigaChat-вызовы при повторной индексации.
        with get_db() as db:
            existing = DeviationRepo.count_by_document(db, doc_data["document_id"])
        if existing > 0:
            state.log(f"  [{i+1}/{total}] КМ {doc_data['check_id']} — пропуск ({existing} отклонений уже есть)")
            skipped += 1
            continue

        state.log(f"  [{i+1}/{total}] КМ {doc_data['check_id']} — извлекаем...")
        devs = extract_deviations_for_document(
            chunks=doc_data["chunks"],
            document_id=doc_data["document_id"],
            check_id=doc_data["check_id"],
            progress_callback=state.log,
        )
        all_devs.extend(devs)

    if all_devs:
        with get_db() as db:
            DeviationRepo.insert_bulk(db, all_devs)
        state.log(f"Сохранено новых отклонений: {len(all_devs)} (пропущено документов: {skipped})")
    else:
        state.log(f"Новых отклонений нет (пропущено: {skipped} из {total})")


# ──────────────────────────────────────────────────────────────────
# Main pipeline runner
# ──────────────────────────────────────────────────────────────────

def _collect_hydrated_chunks(state: PipelineState):
    """Векторы чанков, гидратированных из GP (faiss_id ≥ GP_ID_OFFSET),
    перед полным ребилдом индексов.

    build_indexes перезаписывает FAISS/BM25 только локальными документами —
    без этого шага общий корпус исчез бы из поиска навсегда: чанки остались
    бы в SQLite с мёртвыми faiss_id, а пройденный watermark не дал бы
    гидратации привезти их заново. Если старый индекс недоступен (векторы
    не восстановить) — гидратированные документы удаляются из SQLite и
    watermark сбрасывается: корпус приедет из GP заново."""
    import numpy as np
    from backend.sync.act_cache_sync import GP_ID_OFFSET, reset_watermark
    from backend.storage.database import get_db, Chunk, Document

    with get_db() as db:
        rows = (db.query(Chunk).filter(Chunk.faiss_id >= GP_ID_OFFSET)
                .order_by(Chunk.faiss_id).all())
        items = [{"faiss_id": int(c.faiss_id), "text": c.text} for c in rows]
    if not items:
        return None

    index = None
    try:
        from backend.indexing.index_builder import load_faiss
        index = load_faiss()
    except Exception:
        index = None

    keep, vecs = [], []
    if index is not None:
        for it in items:
            try:
                vecs.append(index.reconstruct(int(it["faiss_id"])))
                keep.append(it)
            except Exception:
                continue

    if keep:
        state.log(f"[GP] Гидратированный корпус: сохраняю {len(keep)} чанков "
                  f"перед ребилдом индексов")
        return keep, np.array(vecs, dtype=np.float32)

    with get_db() as db:
        docs = (db.query(Document)
                .filter(Document.original_path.like("greenplum://%")).all())
        n = len(docs)
        for d in docs:
            db.delete(d)   # каскадом удаляет чанки и отклонения
    reset_watermark()
    state.log(f"[GP] Старый индекс недоступен — {n} гидратированных "
              f"документов удалено, watermark сброшен: корпус приедет "
              f"из GP заново")
    return None


def _step_push_to_gp(state: PipelineState) -> None:
    """Публикует в GP документы, которых там ещё нет: чанки с эмбеддингами
    (реконструкция из FAISS — без пересчёта) + отклонения.

    Ошибки не роняют пайплайн: локальная индексация самоценна,
    публикация повторится при следующем прогоне."""
    try:
        from backend.storage import gp
        if not gp.gp_enabled():
            return
        from backend.indexing.index_builder import load_faiss
        from backend.storage.database import get_db, Document, Chunk, Deviation

        state.log("[GP] Публикация новых актов в общий корпус...")
        known = set(gp.ActGPRepo.known_file_ids())
        index = load_faiss()

        pushed = 0
        with get_db() as db:
            docs = db.query(Document).all()
            for doc in docs:
                if doc.file_id in known:
                    continue
                if (doc.original_path or "").startswith("greenplum://"):
                    continue  # сам гидратирован из GP
                chunks = (db.query(Chunk)
                          .filter(Chunk.document_id == doc.id)
                          .order_by(Chunk.chunk_index).all())
                chunk_dicts = []
                for c in chunks:
                    if c.faiss_id is None:
                        continue
                    try:
                        emb = index.reconstruct(int(c.faiss_id))
                    except Exception:
                        continue
                    chunk_dicts.append({
                        "chunk_index": c.chunk_index,
                        "header_path": c.header_path,
                        "text": c.text,
                        "emb": [float(x) for x in emb],
                    })
                devs = db.query(Deviation).filter(
                    Deviation.document_id == doc.id).all()
                # Полный акт вытесняет автособранный из витрины
                try:
                    n_vit = gp.ActGPRepo.delete_vitrina_doc(doc.check_id)
                    if n_vit:
                        state.log(f"[GP] Витринный акт {doc.check_id} "
                                  f"заменён полным (.docx)")
                except Exception as e:
                    state.log(f"[GP] Чистка витринного акта: {e}")
                gp.ActGPRepo.push_document(
                    {"file_id": doc.file_id, "filename": doc.filename,
                     "check_id": doc.check_id, "topic": doc.topic},
                    chunk_dicts,
                    [{"check_id": v.check_id, "category": v.category,
                      "description": v.description, "severity": v.severity,
                      "financial_impact_rub": v.financial_impact_rub,
                      "affected_systems": v.affected_systems,
                      "regulation_refs": v.regulation_refs,
                      "affected_count": v.affected_count,
                      "responsible_unit": v.responsible_unit,
                      "recommendation": v.recommendation,
                      # Без этого поля отклонение в GP теряет привязку к
                      # фрагменту акта, и дословную цитату к нему не поднять
                      "source_chunk_index": v.source_chunk_index}
                     for v in devs])
                pushed += 1
        state.log(f"[GP] Опубликовано в общий корпус: {pushed} документов")
    except Exception as e:
        state.log(f"[GP] Публикация не удалась (повторится при след. прогоне): {e}")


def run_pipeline_sync(extract_deviations: bool = True) -> PipelineState:
    """
    Запускает полный пайплайн синхронно.
    Для вызова из фонового потока.
    """
    global _pipeline_state

    with _pipeline_lock:
        if _pipeline_state.status == PipelineStatus.RUNNING:
            raise RuntimeError("Пайплайн уже запущен")
        _pipeline_state = PipelineState(
            status=PipelineStatus.RUNNING,
            started_at=datetime.utcnow(),
        )

    state = _pipeline_state
    try:
        # Шаг 1: Конвертация
        md_metas = _step_convert(state)
        if not md_metas:
            state.log("Нет файлов для обработки. Проверьте папку data/raw/")
            state.status = PipelineStatus.ERROR
            state.error = "Нет файлов для обработки"
            return state

        # Шаг 2: Чанкинг
        chunks = _step_chunk(state, md_metas)
        if not chunks:
            state.log("Не удалось создать чанки. Проверьте содержимое документов.")
            state.status = PipelineStatus.ERROR
            state.error = "Нет чанков"
            return state

        # Шаг 3: Индексы (присваивает faiss_id каждому чанку in-place).
        # ВАЖНО: должен идти ДО save_to_db, иначе чанки сохранятся в БД с
        # faiss_id=NULL и retrieval не сможет связать FAISS-результаты с метаданными.
        # Гидратированный из GP корпус сохраняем до ребилда и возвращаем после
        hydrated = None
        try:
            hydrated = _collect_hydrated_chunks(state)
        except Exception as e:
            state.log(f"[GP] Не удалось сохранить гидратированный корпус: {e}")
        _step_build_indexes(state, chunks)
        if hydrated:
            try:
                from backend.indexing.index_builder import append_to_indexes
                append_to_indexes(hydrated[0], hydrated[1])
                state.log(f"[GP] Гидратированный корпус возвращён в индексы: "
                          f"{len(hydrated[0])} чанков")
            except Exception as e:
                state.log(f"[GP] Возврат гидратированного корпуса не удался: {e}")

        # Шаг 4: БД
        file_id_to_doc = _step_save_to_db(state, md_metas, chunks)

        # Шаг 5: Отклонения (опционально)
        if extract_deviations:
            _step_extract_deviations(state, file_id_to_doc)

        # Шаг 6: Публикация новых актов в общий корпус Greenplum —
        # проиндексированное одним пользователем доступно всем
        _step_push_to_gp(state)

        state.progress_pct = 100
        state.status = PipelineStatus.DONE
        state.finished_at = datetime.utcnow()
        state.log("=== Пайплайн завершён успешно! ===")

    except Exception as e:
        logger.exception(f"[Pipeline] Критическая ошибка: {e}")
        state.status = PipelineStatus.ERROR
        state.error = str(e)
        state.log(f"ОШИБКА: {e}")

    return state


def start_pipeline_background(extract_deviations: bool = True):
    """Запускает пайплайн в отдельном потоке."""
    thread = threading.Thread(
        target=run_pipeline_sync,
        args=(extract_deviations,),
        daemon=True,
        name="indexing-pipeline",
    )
    thread.start()
