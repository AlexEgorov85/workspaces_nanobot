"""
Follow Up 2.0 — SQLite Storage Layer.

Таблицы:
  documents    — загруженные документы (мета)
  chunks       — чанки документов с привязкой к FAISS индексу
  deviations   — извлечённые LLM отклонения по каждому документу
  sessions     — чат-сессии
  messages     — сообщения в сессиях
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Generator, List, Optional, Dict, Any

logger = logging.getLogger(__name__)

from sqlalchemy import (
    Column, DateTime, ForeignKey, Integer, String, Text, Boolean, Float,
    create_engine, func, text
)
from sqlalchemy.orm import DeclarativeBase, Session, relationship, sessionmaker

from backend.config import get_settings
from backend.core import identity

# ──────────────────────────────────────────────────────────────────
# Base model
# ──────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


class Document(Base):
    __tablename__ = "documents"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    file_id     = Column(String, unique=True, nullable=False)
    filename    = Column(String, nullable=False)
    check_id    = Column(String, nullable=False, index=True)  # КМ-99-XXXXX
    topic       = Column(String, nullable=True)               # извлечённая тема (LLM)
    original_path = Column(Text, nullable=False)
    md_path     = Column(Text, nullable=False)
    created_at  = Column(DateTime, default=datetime.utcnow)

    chunks      = relationship("Chunk", back_populates="document", cascade="all, delete-orphan")
    deviations  = relationship("Deviation", back_populates="document", cascade="all, delete-orphan")


class Chunk(Base):
    __tablename__ = "chunks"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    document_id  = Column(Integer, ForeignKey("documents.id"), nullable=False)
    chunk_index  = Column(Integer, nullable=False)
    faiss_id     = Column(Integer, nullable=True)   # позиция в FAISS индексе
    text         = Column(Text, nullable=False)
    header_path  = Column(String, nullable=True)    # "Раздел 1 > Подраздел 2"
    char_count   = Column(Integer, nullable=True)

    document     = relationship("Document", back_populates="chunks")


class Deviation(Base):
    __tablename__ = "deviations"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    document_id  = Column(Integer, ForeignKey("documents.id"), nullable=False)
    check_id     = Column(String, nullable=False, index=True)
    category     = Column(String, nullable=True)   # тип нарушения
    description  = Column(Text, nullable=False)    # текст отклонения
    severity     = Column(String, nullable=True)   # критичное / существенное / формальное
    source_chunk_index = Column(Integer, nullable=True)
    # Расширенные поля (JSON-строки для list-полей, чтобы остаться в SQLite)
    financial_impact_rub = Column(Float, nullable=True)   # сумма потерь в рублях
    affected_systems     = Column(Text, nullable=True)    # JSON list: ["АБС БИСквит", "ДБО"]
    regulation_refs      = Column(Text, nullable=True)    # JSON list: ["716-П п.4.2", "152-ФЗ"]
    affected_count       = Column(Integer, nullable=True) # кол-во затронутых записей/учёток
    responsible_unit     = Column(String, nullable=True)  # ответственное подразделение
    recommendation       = Column(Text, nullable=True)    # предписание / срок устранения
    created_at   = Column(DateTime, default=datetime.utcnow)

    document     = relationship("Document", back_populates="deviations")


class EntityMention(Base):
    """Обратный индекс упоминаний: сущность → где встречается.

    Без него `search_entity` сканирует все чанки корпуса на каждый вопрос: на
    локальных шести актах это незаметно, на 214 актах прод-корпуса — секунды
    единственного процессора на каждый запрос. Строится офлайн (`derived.py`)
    и обновляется гидратацией по мере появления документов.
    """
    __tablename__ = "entity_mentions"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    kind         = Column(String, nullable=False, index=True)   # person|system|…
    norm         = Column(String, nullable=False, index=True)   # нормализованная форма
    value        = Column(String, nullable=False)               # как написано в акте
    check_id     = Column(String, nullable=False, index=True)
    document_id  = Column(Integer, ForeignKey("documents.id"), nullable=False)
    chunk_index  = Column(Integer, nullable=False)
    header_path  = Column(String, nullable=True)
    where        = Column(String, nullable=True)   # case_text | requisites
    quote        = Column(Text, nullable=True)     # окно вокруг упоминания


class DialogState(Base):
    """Одна строка на сессию: о чём сейчас разговор.

    Модель «нитей» с активацией и затуханием сознательно НЕ реализуется: в базе
    71 сессия, из них 49 по два сообщения — калибровать затухание не на чем, а
    несколько параллельных нитей это новый способ потерять привязку ответа к
    проверке, только молча. Одна строка фокуса с провенансом даёт тот же
    результат по жалобе «теряется контекст».
    """
    __tablename__ = "dialog_state"

    session_id     = Column(Integer, ForeignKey("sessions.id"), primary_key=True)
    focus_check_id = Column(String, nullable=True)   # канон через identity.format
    focus_basis    = Column(String, nullable=True)   # user_said|confirmed|inferred
    focus_turn_id  = Column(String, nullable=True)
    topic          = Column(Text, nullable=True)
    open_question  = Column(Text, nullable=True)     # JSON замороженного вопроса
    shown_sources  = Column(Text, nullable=True)     # JSON [chunk_uid]
    updated_at     = Column(DateTime, default=datetime.utcnow,
                            onupdate=datetime.utcnow)


class Turn(Base):
    """Ход = ДВЕ записи: INSERT при приёме и UPDATE при завершении.

    Ошибка или отмена больше не стирают вопрос: раньше он писался последним
    шагом, и любое падение уносило и вопрос, и номер проверки.
    """
    __tablename__ = "turns"

    id            = Column(String, primary_key=True)     # turn_id
    session_id    = Column(Integer, nullable=False, index=True)
    text          = Column(Text, nullable=False)
    status        = Column(String, nullable=False, default="open")
    understanding = Column(Text, nullable=True)
    body_md       = Column(Text, nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow)
    closed_at     = Column(DateTime, nullable=True)


class ChatSession(Base):
    __tablename__ = "sessions"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    title      = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    messages   = relationship("ChatMessage", back_populates="session", cascade="all, delete-orphan",
                               order_by="ChatMessage.id")


class ExternalSession(Base):
    """Сессия внешнего агента (nanobot) → наша сессия диалога.

    Память диалога (`core/memory/state.py`) ключуется нашим `session_id`.
    Внешний агент своего не знает — у него собственный ключ вида
    `webui:12345678` или `telegram:123456789`. Без этой таблицы каждый
    вызов из единого окна был бы новым диалогом, и вернулось бы ровно то,
    ради чего писался ЭТАП 3: «уточните, о какой КМ» → ответ → «ничего не
    нашёл».

    `user_id` хранится отдельно от ключа: ключ отвечает за память, автор —
    за то, чьё это действие. Смешивать их нельзя, иначе смена интерфейса
    у того же аудитора начнёт выглядеть сменой человека.
    """
    __tablename__ = "external_sessions"

    key        = Column(String, primary_key=True)
    session_id = Column(Integer, ForeignKey("sessions.id", ondelete="CASCADE"),
                        nullable=False)
    user_id    = Column(String, nullable=True)
    source     = Column(String, nullable=True)      # nanobot | иной вызывающий
    created_at = Column(DateTime, default=datetime.utcnow)
    last_seen  = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ChatMessage(Base):
    __tablename__ = "messages"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    session_id   = Column(Integer, ForeignKey("sessions.id"), nullable=False)
    role         = Column(String, nullable=False)    # user / assistant
    content      = Column(Text, nullable=False)
    agent_type   = Column(String, nullable=True)    # followup / hypothesis / recheck / ...
    contexts_json  = Column(Text, nullable=True)    # JSON список источников
    followups_json = Column(Text, nullable=True)    # JSON список follow-up подсказок (чипы)
    created_at   = Column(DateTime, default=datetime.utcnow)

    session      = relationship("ChatSession", back_populates="messages")


# ──────────────────────────────────────────────────────────────────
# Engine & session factory
# ──────────────────────────────────────────────────────────────────

_engine = None
_SessionLocal = None


def _prepare_db(db_path: Path) -> None:
    """
    Подготовка БД перед подключением (NFS-safe, без sqlite3.connect).

    В DataLab домашние каталоги монтированы через NFS.
    SQLite WAL mode использует fcntl-locks на SHM файле, которые
    на NFS работают ненадёжно или зависают навсегда.

    Если БД ранее работала в WAL mode, в заголовке .db файла
    (байты 18-19) стоит значение 2 (WAL). При следующем connect()
    SQLite пытается создать SHM и захватить NFS lock → зависание.

    Решение:
    1. Патчим байты 18-19 в заголовке: 2 (WAL) → 1 (DELETE)
       Это бинарная операция, не требует sqlite3.connect().
    2. Удаляем WAL/SHM файлы (больше не нужны).

    Документация формата: https://www.sqlite.org/fileformat.html
    Offset 18: File format write version (1=rollback, 2=WAL)
    Offset 19: File format read version  (1=rollback, 2=WAL)
    """
    # -- Шаг 1: Патч заголовка .db файла из WAL → DELETE --
    if db_path.exists() and db_path.stat().st_size >= 100:
        with open(db_path, "r+b") as f:
            header = f.read(20)
            # Проверяем magic string "SQLite format 3\000"
            if header[:16] == b"SQLite format 3\x00":
                write_ver = header[18]
                read_ver = header[19]
                if write_ver == 2 or read_ver == 2:
                    f.seek(18)
                    f.write(b"\x01\x01")  # 1 = rollback journal (DELETE)
                    logger.info(
                        "[DB] Заголовок БД: WAL → DELETE journal mode "
                        "(WAL несовместим с NFS/DataLab)"
                    )

    # -- Шаг 2: Удаляем WAL/SHM файлы (артефакты предыдущего WAL режима) --
    for suffix in ("-wal", "-shm"):
        stale = Path(str(db_path) + suffix)
        if stale.exists():
            try:
                stale.unlink()
                logger.info(f"[DB] Удалён {stale.name}")
            except Exception as e:
                logger.warning(f"[DB] Не удалось удалить {stale.name}: {e}")


def init_db() -> None:
    global _engine, _SessionLocal
    settings = get_settings()
    db_path = settings.db_file
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Конвертируем WAL → DELETE и чистим артефакты (без connect, NFS-safe)
    _prepare_db(db_path)

    def creator():
        # Ключевой фикс для NFS (DataLab): vfs=unix-none в URI полностью отключает
        # fcntl/posix блокировки, из-за которых SQLite зависала намертво.
        # Так как сервер работает в 1 процесс, нам эти блокировки не нужны.
        return sqlite3.connect(
            f"file:{db_path}?mode=rwc&vfs=unix-none",
            uri=True,
            check_same_thread=False,
            timeout=30.0
        )

    _engine = create_engine(
        "sqlite://",  # URL игнорируется, когда передан creator
        creator=creator,
        echo=False,
    )
    with _engine.connect() as conn:
        # DELETE mode — надёжно работает на NFS, в отличие от WAL
        conn.execute(text("PRAGMA journal_mode=DELETE"))
        conn.execute(text("PRAGMA foreign_keys=ON"))
        conn.execute(text("PRAGMA busy_timeout=30000"))
    logger.info("[DB] SQLite подключена (journal_mode=DELETE).")

    Base.metadata.create_all(_engine)
    _migrate_deviations_columns(_engine)
    _migrate_messages_columns(_engine)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)


def _migrate_deviations_columns(engine) -> None:
    """Авто-миграция: добавляет недостающие колонки в deviations для старых БД.

    SQLite не умеет ALTER TABLE с проверкой, поэтому делаем через PRAGMA table_info.
    Для каждой новой колонки — отдельный ALTER TABLE ADD COLUMN.
    """
    expected = {
        "financial_impact_rub": "FLOAT",
        "affected_systems":     "TEXT",
        "regulation_refs":      "TEXT",
        "affected_count":       "INTEGER",
        "responsible_unit":     "VARCHAR",
        "recommendation":       "TEXT",
    }
    with engine.connect() as conn:
        rows = conn.execute(text("PRAGMA table_info(deviations)")).fetchall()
        existing = {r[1] for r in rows}
        for col, sql_type in expected.items():
            if col not in existing:
                conn.execute(text(f"ALTER TABLE deviations ADD COLUMN {col} {sql_type}"))
        conn.commit()


def _migrate_messages_columns(engine) -> None:
    """Авто-миграция: добавляет недостающие колонки в messages для старых БД."""
    expected = {
        "followups_json": "TEXT",
    }
    with engine.connect() as conn:
        rows = conn.execute(text("PRAGMA table_info(messages)")).fetchall()
        existing = {r[1] for r in rows}
        for col, sql_type in expected.items():
            if col not in existing:
                conn.execute(text(f"ALTER TABLE messages ADD COLUMN {col} {sql_type}"))
        conn.commit()


def get_engine():
    if _engine is None:
        init_db()
    return _engine


@contextmanager
def get_db() -> Generator[Session, None, None]:
    """Сессия под общим локом записи.

    Лок берётся ДО открытия сессии и отпускается ПОСЛЕ commit/rollback — иначе
    две транзакции пишут в один rollback-журнал без единого лока (`vfs=unix-none`
    отключает блокировки SQLite целиком). Читатели берут тот же лок: без
    блокировок читатель не защищён от чужой полузаписанной страницы.

    Правило «одна транзакция ≤ один документ» соблюдает вызывающий: батч на 2000
    строк под общим локом превратил бы гидратацию в стоп-кран для чата.
    """
    if _SessionLocal is None:
        init_db()
    from backend.storage.writer import write_tx
    with write_tx("get_db"):
        db: Session = _SessionLocal()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


# ──────────────────────────────────────────────────────────────────
# CRUD helpers
# ──────────────────────────────────────────────────────────────────

def _check_id_filter(column, check_id: str):
    """Условие «этот check_id» — точное сравнение или прежний ILIKE.

    ILIKE '%КМ-99-1234%' совпадал с КМ-99-12345, КМ-99-12346 и ещё тремя
    проверками: пять чужих актов в одном ответе. Каждая КМ — отдельная
    проверка, поэтому сравнение точное, по канонической форме.

    Флаг identity_exact_match=false возвращает прежнее поведение: аудит
    значений идёт на прод-данных, и откат должен существовать до того, как
    он понадобится (docs/AGENT_ARCHITECTURE_PLAN.md, ЭТАП 0 п.6).
    """
    try:
        from backend.config import get_settings
        exact = get_settings().identity_exact_match
    except Exception:
        exact = True
    if not exact:
        return column.ilike(f"%{check_id}%")
    canon = identity.normalize(check_id)
    return column == (canon or check_id)


class DocumentRepo:
    @staticmethod
    def upsert(db: Session, data: Dict[str, Any]) -> Document:
        doc = db.query(Document).filter_by(file_id=data["file_id"]).first()
        if doc is None:
            doc = Document(**{k: v for k, v in data.items()
                              if k in Document.__table__.columns.keys()})
            db.add(doc)
        else:
            for k, v in data.items():
                if hasattr(doc, k):
                    setattr(doc, k, v)
        db.flush()
        return doc

    @staticmethod
    def list_all(db: Session) -> List[Document]:
        return db.query(Document).order_by(Document.created_at.desc()).all()

    @staticmethod
    def get_by_check_id(db: Session, check_id: str) -> List[Document]:
        return db.query(Document).filter(
            _check_id_filter(Document.check_id, check_id)).all()

    @staticmethod
    def get_canonical(db: Session, check_id: str) -> Optional[Document]:
        """Один документ проверки — тот, по которому отвечаем.

        Переиндексация одного и того же акта с другим путём плодит близнецов
        (upsert ключуется по file_id, не по check_id): в локальной базе каждый
        КМ лежал в трёх копиях, и drill-down склеивал 24 чанка вместо 8.
        Канон — самый свежий документ с наибольшим числом чанков: пустой
        близнец от прерванной индексации не должен победить полный акт.
        """
        docs = DocumentRepo.get_by_check_id(db, check_id)
        if not docs:
            return None
        if len(docs) == 1:
            return docs[0]
        counts = dict(
            db.query(Chunk.document_id, func.count(Chunk.id))
              .filter(Chunk.document_id.in_([d.id for d in docs]))
              .group_by(Chunk.document_id).all())
        return max(docs, key=lambda d: (counts.get(d.id, 0), d.id))

    @staticmethod
    def count(db: Session) -> int:
        return db.query(Document).count()

    @staticmethod
    def list_check_ids(db: Session) -> List[str]:
        """Уникальные номера КМ в базе (для подсказки аудитору при пустом retrieval)."""
        rows = db.query(Document.check_id).distinct().all()
        return sorted({r[0] for r in rows if r[0]})


class ChunkRepo:
    @staticmethod
    def insert_bulk(db: Session, chunks: List[Dict[str, Any]]) -> None:
        db.bulk_insert_mappings(Chunk, chunks)
        db.flush()

    @staticmethod
    def get_by_faiss_ids(db: Session, faiss_ids: List[int]) -> List[Chunk]:
        return db.query(Chunk).filter(Chunk.faiss_id.in_(faiss_ids)).all()

    @staticmethod
    def get_by_check_id(db: Session, check_id: str,
                        canonical_only: bool = False) -> List[Chunk]:
        """Все чанки документа по номеру КМ — для drill-down.

        canonical_only=True берёт чанки ОДНОГО документа (DocumentRepo.
        get_canonical): при трёх копиях акта в базе без этого приходило 24
        чанка вместо 8, и один и тот же фрагмент попадал в контекст трижды.
        """
        if canonical_only:
            doc = DocumentRepo.get_canonical(db, check_id)
            if doc is None:
                return []
            return (
                db.query(Chunk)
                .filter(Chunk.document_id == doc.id)
                .order_by(Chunk.chunk_index)
                .all()
            )
        return (
            db.query(Chunk)
            .join(Document, Chunk.document_id == Document.id)
            .filter(_check_id_filter(Document.check_id, check_id))
            .order_by(Chunk.chunk_index)
            .all()
        )

    @staticmethod
    def count(db: Session) -> int:
        return db.query(Chunk).count()

    @staticmethod
    def delete_by_document_ids(db: Session, document_ids: List[int]) -> int:
        """Удаляет все чанки для указанных документов. Нужно при перестройке FAISS."""
        if not document_ids:
            return 0
        n = db.query(Chunk).filter(Chunk.document_id.in_(document_ids)).delete(
            synchronize_session=False
        )
        return n


class DeviationRepo:
    @staticmethod
    def insert_bulk(db: Session, devs: List[Dict[str, Any]]) -> None:
        db.bulk_insert_mappings(Deviation, devs)
        db.flush()

    @staticmethod
    def search(db: Session, query: str, check_id: Optional[str] = None) -> List[Deviation]:
        q = db.query(Deviation)
        if check_id:
            q = q.filter(_check_id_filter(Deviation.check_id, check_id))
        if query:
            q = q.filter(Deviation.description.ilike(f"%{query}%"))
        return q.order_by(Deviation.created_at.desc()).limit(50).all()

    @staticmethod
    def get_by_check_id(db: Session, check_id: str) -> List[Deviation]:
        return db.query(Deviation).filter(
            _check_id_filter(Deviation.check_id, check_id)
        ).all()

    @staticmethod
    def count(db: Session) -> int:
        return db.query(Deviation).count()

    @staticmethod
    def count_by_document(db: Session, document_id: int) -> int:
        return db.query(Deviation).filter_by(document_id=document_id).count()

    @staticmethod
    def get_categories_stats(db: Session) -> List[Dict]:
        from sqlalchemy import func
        rows = (
            db.query(Deviation.category, func.count(Deviation.id).label("cnt"))
            .group_by(Deviation.category)
            .order_by(func.count(Deviation.id).desc())
            .all()
        )
        return [{"category": r.category or "Не классифицировано", "count": r.cnt} for r in rows]

    @staticmethod
    def get_severity_stats(db: Session) -> List[Dict]:
        from sqlalchemy import func
        rows = (
            db.query(Deviation.severity, func.count(Deviation.id).label("cnt"))
            .group_by(Deviation.severity)
            .order_by(func.count(Deviation.id).desc())
            .all()
        )
        return [{"severity": r.severity or "не указано", "count": r.cnt} for r in rows]

    @staticmethod
    def get_financial_impact_total(db: Session) -> float:
        from sqlalchemy import func
        val = db.query(func.coalesce(func.sum(Deviation.financial_impact_rub), 0.0)).scalar()
        return float(val or 0.0)

    @staticmethod
    def get_financial_impact_by_category(db: Session) -> List[Dict]:
        from sqlalchemy import func
        rows = (
            db.query(
                Deviation.category,
                func.coalesce(func.sum(Deviation.financial_impact_rub), 0.0).label("total"),
                func.count(Deviation.id).label("cnt"),
            )
            .filter(Deviation.financial_impact_rub.isnot(None))
            .group_by(Deviation.category)
            .order_by(func.sum(Deviation.financial_impact_rub).desc())
            .all()
        )
        return [
            {"category": r.category or "Не классифицировано",
             "total_rub": float(r.total or 0.0),
             "count": r.cnt}
            for r in rows
        ]

    @staticmethod
    def get_top_systems(db: Session, limit: int = 10) -> List[Dict]:
        """Топ затронутых систем — парсит JSON-поле affected_systems."""
        from collections import Counter
        rows = db.query(Deviation.affected_systems).filter(
            Deviation.affected_systems.isnot(None)
        ).all()
        counter: Counter = Counter()
        for (raw,) in rows:
            try:
                items = json.loads(raw) if raw else []
            except (ValueError, TypeError):
                continue
            for s in items:
                if s:
                    counter[s.strip()] += 1
        return [{"system": s, "count": c} for s, c in counter.most_common(limit)]

    @staticmethod
    def get_top_regulations(db: Session, limit: int = 10) -> List[Dict]:
        """Топ упоминаемых нормативов — парсит JSON-поле regulation_refs."""
        from collections import Counter
        rows = db.query(Deviation.regulation_refs).filter(
            Deviation.regulation_refs.isnot(None)
        ).all()
        counter: Counter = Counter()
        for (raw,) in rows:
            try:
                items = json.loads(raw) if raw else []
            except (ValueError, TypeError):
                continue
            for r in items:
                if r:
                    counter[r.strip()] += 1
        return [{"regulation": r, "count": c} for r, c in counter.most_common(limit)]


class ExternalSessionRepo:
    """Соответствие «ключ внешнего агента → наша сессия»."""

    @staticmethod
    def resolve(db: Session, key: str, user_id: Optional[str] = None,
                source: Optional[str] = None) -> int:
        """Вернуть наш session_id для ключа, создав сессию при первом вызове.

        Идемпотентно: второй вызов с тем же ключом отдаёт ту же сессию, иначе
        фокус диалога терялся бы между ходами.
        """
        row = db.query(ExternalSession).filter_by(key=key).first()
        if row is not None:
            if user_id and row.user_id != user_id:
                # Тот же ключ у другого человека — это не «переименование»,
                # а чужой диалог. Заводим отдельную сессию, а не подставляем
                # чужую память.
                logger.warning(
                    f"[skill] Ключ {key} сменил владельца "
                    f"({row.user_id} → {user_id}) — новая сессия")
                db.delete(row)
                db.flush()
            else:
                row.last_seen = datetime.utcnow()
                db.flush()
                return int(row.session_id)

        s = ChatSession(title=f"nanobot: {key}")
        db.add(s)
        db.flush()
        db.add(ExternalSession(key=key, session_id=s.id, user_id=user_id,
                               source=source))
        db.flush()
        return int(s.id)

    @staticmethod
    def get(db: Session, key: str) -> Optional[ExternalSession]:
        return db.query(ExternalSession).filter_by(key=key).first()

    @staticmethod
    def forget(db: Session, key: str) -> bool:
        row = db.query(ExternalSession).filter_by(key=key).first()
        if row is None:
            return False
        db.delete(row)
        db.flush()
        return True


class SessionRepo:
    @staticmethod
    def create(db: Session, title: Optional[str] = None) -> ChatSession:
        s = ChatSession(title=title)
        db.add(s)
        db.flush()
        return s

    @staticmethod
    def list_all(db: Session, limit: int = 50) -> List[ChatSession]:
        return db.query(ChatSession).order_by(ChatSession.updated_at.desc()).limit(limit).all()

    @staticmethod
    def get(db: Session, session_id: int) -> Optional[ChatSession]:
        return db.query(ChatSession).filter_by(id=session_id).first()

    @staticmethod
    def update_title(db: Session, session_id: int, title: str) -> None:
        db.query(ChatSession).filter_by(id=session_id).update({"title": title})

    @staticmethod
    def delete(db: Session, session_id: int) -> None:
        s = db.query(ChatSession).filter_by(id=session_id).first()
        if s:
            db.delete(s)


class MessageRepo:
    @staticmethod
    def add(db: Session, session_id: int, role: str, content: str,
            agent_type: Optional[str] = None,
            contexts: Optional[List[Dict]] = None,
            followups: Optional[List[str]] = None) -> ChatMessage:
        msg = ChatMessage(
            session_id=session_id,
            role=role,
            content=content,
            agent_type=agent_type,
            contexts_json=json.dumps(contexts, ensure_ascii=False) if contexts else None,
            followups_json=json.dumps(followups, ensure_ascii=False) if followups else None,
        )
        db.add(msg)
        # Список сессий сортируется по updated_at, а он обновлялся только
        # onupdate самой строки сессии — то есть при переименовании.
        # Активная переписка уезжала вниз списка.
        db.query(ChatSession).filter(ChatSession.id == session_id).update(
            {ChatSession.updated_at: datetime.utcnow()})
        db.flush()
        return msg

    @staticmethod
    def get_session_messages(db: Session, session_id: int) -> List[ChatMessage]:
        return (
            db.query(ChatMessage)
            .filter_by(session_id=session_id)
            .order_by(ChatMessage.id)
            .all()
        )
