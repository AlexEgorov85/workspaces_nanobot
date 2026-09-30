"""Follow Up 2.0 — рабочая память сессии.

Отвечает на вопрос, которого система до сих пор себе не задавала: **о чём сейчас
разговор**. Раньше контекст выводился заново регексами по последним двенадцати
сообщениям, а `contexts_json` с карточкой писался в базу и никем не читался.

Две вещи, ради которых модуль существует:

**Фокус с провенансом.** Не просто «последний упомянутый КМ», а откуда он взялся:
`user_said` — аудитор назвал сам, `confirmed` — подтвердил выбором,
`inferred` — система подставила. Провенанс печатается в ответе и в чипе
контекста: подстановка обязана быть видимой и обратимой, потому что выбор
проверки принадлежит аудитору.

**Открытый вопрос как состояние, а не тупик.** Сегодня «Уточните, по какой КМ»
теряет исходную формулировку: ответ «КМ-99-12345» обрабатывается как новый
запрос, и аудитор получает «ничего не нашёл» на вопрос, которого не задавал.
Здесь вопрос замораживается целиком и размораживается ответом.

Разморозка требует ТРЁХ условий сразу, и третье сильнее первых двух: последнее
сообщение ассистента должно быть именно тем вопросом. Без него вечерний
«покажи все акты про лимиты» перехватил бы утреннее «КМ-99-12345» и уверенно
ответил охватом — та же потеря контекста, только незаметная.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

BASIS_USER = "user_said"
BASIS_CONFIRMED = "confirmed"
BASIS_INFERRED = "inferred"

BASIS_RU = {
    BASIS_USER: "вы назвали",
    BASIS_CONFIRMED: "вы подтвердили",
    BASIS_INFERRED: "подставлено системой",
}


def _cfg():
    from backend.config import get_settings
    return get_settings()


@dataclass
class OpenQuestion:
    question: str                 # что спросили у аудитора
    original_query: str           # ИСХОДНЫЙ вопрос, ради которого спрашивали
    awaiting: str                 # что ждём: check_id | choice | confirm
    candidates: List[str] = field(default_factory=list)
    created_ts: float = 0.0
    created_turn_id: Optional[str] = None
    asked_message_id: Optional[int] = None

    def as_dict(self) -> Dict:
        return {"question": self.question, "original_query": self.original_query,
                "awaiting": self.awaiting, "candidates": self.candidates,
                "created_ts": self.created_ts,
                "created_turn_id": self.created_turn_id,
                "asked_message_id": self.asked_message_id}

    @staticmethod
    def from_dict(d: Optional[Dict]) -> Optional["OpenQuestion"]:
        if not d:
            return None
        try:
            return OpenQuestion(
                question=d.get("question", ""),
                original_query=d.get("original_query", ""),
                awaiting=d.get("awaiting", "check_id"),
                candidates=list(d.get("candidates") or []),
                created_ts=float(d.get("created_ts") or 0),
                created_turn_id=d.get("created_turn_id"),
                asked_message_id=d.get("asked_message_id"))
        except Exception:
            return None


@dataclass
class DialogFocus:
    check_id: Optional[str] = None
    basis: Optional[str] = None
    turn_id: Optional[str] = None
    topic: Optional[str] = None
    open_question: Optional[OpenQuestion] = None
    shown_sources: List[str] = field(default_factory=list)

    @property
    def is_inferred(self) -> bool:
        return self.basis == BASIS_INFERRED

    def human_basis(self) -> str:
        return BASIS_RU.get(self.basis or "", "")


# ──────────────────────────────────────────────────────────────────
# Чтение и запись
# ──────────────────────────────────────────────────────────────────

def load(session_id: int) -> DialogFocus:
    from backend.storage.database import DialogState, get_db
    try:
        with get_db() as db:
            row = db.query(DialogState).filter(
                DialogState.session_id == session_id).first()
            if row is None:
                return DialogFocus()
            return DialogFocus(
                check_id=row.focus_check_id, basis=row.focus_basis,
                turn_id=row.focus_turn_id, topic=row.topic,
                open_question=OpenQuestion.from_dict(
                    json.loads(row.open_question) if row.open_question else None),
                shown_sources=json.loads(row.shown_sources or "[]"))
    except Exception as e:
        logger.warning(f"[memory] Состояние сессии {session_id} не прочитано: {e}")
        return DialogFocus()


def _upsert(session_id: int, **fields) -> None:
    from backend.storage.database import DialogState, get_db
    try:
        with get_db() as db:
            row = db.query(DialogState).filter(
                DialogState.session_id == session_id).first()
            if row is None:
                row = DialogState(session_id=session_id)
                db.add(row)
            for k, v in fields.items():
                setattr(row, k, v)
    except Exception as e:
        # Память не должна ронять ход: без неё ответ хуже, но он есть
        logger.warning(f"[memory] Состояние сессии {session_id} не записано: {e}")


def set_focus(session_id: int, check_id: Optional[str], basis: str,
              turn_id: Optional[str] = None) -> None:
    """Запомнить проверку, о которой идёт разговор, и ОТКУДА она взялась."""
    from backend.core import identity
    canon = identity.format(check_id) if check_id else None
    _upsert(session_id, focus_check_id=canon or None, focus_basis=basis,
            focus_turn_id=turn_id)


def drop_focus(session_id: int) -> None:
    """Аудитор сбросил контекст — подстановки больше нет."""
    _upsert(session_id, focus_check_id=None, focus_basis=None,
            focus_turn_id=None)


def set_topic(session_id: int, topic: Optional[str]) -> None:
    _upsert(session_id, topic=(topic or None))


def remember_sources(session_id: int, uids: List[str], limit: int = 40) -> None:
    if not uids:
        return
    st = load(session_id)
    merged = list(dict.fromkeys(list(st.shown_sources) + list(uids)))[-limit:]
    _upsert(session_id, shown_sources=json.dumps(merged, ensure_ascii=False))


# ──────────────────────────────────────────────────────────────────
# Открытый вопрос
# ──────────────────────────────────────────────────────────────────

def freeze_question(session_id: int, question: str, original_query: str,
                    awaiting: str = "check_id",
                    candidates: Optional[List[str]] = None,
                    turn_id: Optional[str] = None,
                    asked_message_id: Optional[int] = None) -> None:
    """Спросили у аудитора — запомнили, РАДИ ЧЕГО спрашивали."""
    oq = OpenQuestion(question=question, original_query=original_query,
                      awaiting=awaiting, candidates=list(candidates or []),
                      created_ts=time.time(), created_turn_id=turn_id,
                      asked_message_id=asked_message_id)
    _upsert(session_id, open_question=json.dumps(oq.as_dict(),
                                                 ensure_ascii=False))


def clear_question(session_id: int) -> None:
    _upsert(session_id, open_question=None)


def question_is_live(session_id: int, oq: Optional[OpenQuestion]) -> bool:
    """Можно ли считать следующее сообщение ответом на этот вопрос.

    Три условия сразу. Третье сильнее первых двух: оно ловит случай «аудитор
    между делом задал другой вопрос и получил ответ», где TTL ещё не истёк.
    """
    if oq is None:
        return False
    cfg = _cfg()
    if time.time() - (oq.created_ts or 0) > cfg.open_question_ttl_sec:
        return False
    from backend.storage.database import ChatMessage, get_db
    try:
        with get_db() as db:
            last = (db.query(ChatMessage)
                    .filter(ChatMessage.session_id == session_id,
                            ChatMessage.role == "assistant")
                    .order_by(ChatMessage.id.desc()).first())
            turns_since = (db.query(ChatMessage)
                           .filter(ChatMessage.session_id == session_id,
                                   ChatMessage.role == "user",
                                   ChatMessage.id > (oq.asked_message_id or 0))
                           .count())
    except Exception:
        return False
    if turns_since > _cfg().open_question_ttl_turns:
        return False
    if oq.asked_message_id is not None:
        # Последний ответ ассистента должен быть ИМЕННО тем вопросом
        return bool(last and last.id == oq.asked_message_id)
    return True


# ──────────────────────────────────────────────────────────────────
# Ходы
# ──────────────────────────────────────────────────────────────────

def open_turn(session_id: int, text: str,
              turn_id: Optional[str] = None) -> str:
    from backend.storage.database import Turn, get_db
    tid = turn_id or uuid.uuid4().hex[:12]
    try:
        with get_db() as db:
            db.add(Turn(id=tid, session_id=session_id, text=text,
                        status="open"))
    except Exception as e:
        logger.warning(f"[memory] Ход {tid} не открыт: {e}")
    return tid


def close_turn(turn_id: str, status: str,
               understanding: Optional[Dict] = None,
               body_md: Optional[str] = None) -> None:
    from datetime import datetime
    from backend.storage.database import Turn, get_db
    try:
        with get_db() as db:
            row = db.query(Turn).filter(Turn.id == turn_id).first()
            if row is None:
                return
            row.status = status
            row.closed_at = datetime.utcnow()
            if understanding is not None:
                row.understanding = json.dumps(understanding, ensure_ascii=False)
            if body_md is not None:
                row.body_md = body_md
    except Exception as e:
        logger.warning(f"[memory] Ход {turn_id} не закрыт: {e}")


def reap_abandoned() -> int:
    """Ходы, оставшиеся открытыми после гашения процесса.

    DataLab гасит неактивные серверы, и без реапера метрики «вызовов на ход»
    считались бы по грязным данным — калибровать пороги было бы не по чему.
    """
    from datetime import datetime, timedelta
    from backend.storage.database import Turn, get_db
    try:
        limit = datetime.utcnow() - timedelta(seconds=_cfg().turn_deadline_sec)
        with get_db() as db:
            rows = (db.query(Turn)
                    .filter(Turn.status == "open", Turn.created_at < limit)
                    .all())
            for r in rows:
                r.status = "abandoned"
                r.closed_at = datetime.utcnow()
            return len(rows)
    except Exception as e:
        logger.warning(f"[memory] Реапер ходов: {e}")
        return 0
