"""Follow Up 2.0 — детерминированное распознавание ответа на уточнение.

Аудитор ответил «КМ-99-12345» на «уточните, по какой КМ». Раньше это
обрабатывалось как НОВЫЙ вопрос: исходная формулировка терялась, поиск шёл по
строке «КМ-99-12345», и аудитор получал «ничего не нашёл» на вопрос, которого
не задавал. Это и есть жалоба №1 целиком.

Здесь — ноль вызовов модели. Справочник `check_id` валидирует номер за
микросекунды, порядковое «второй» сопоставляется с сохранёнными кандидатами
точным совпадением. Отдавать это интерпретатору значило бы платить 9 секунд и
целый слот очереди за распознавание строки, которую можно проверить локально.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)

_ORDINALS = {
    "перв": 0, "втор": 1, "трет": 2, "четверт": 3, "пят": 4,
    "первый": 0, "первая": 0, "первое": 0,
}
_YES = re.compile(r"^\s*(да|ага|верно|точно|именно|подтверждаю|ок|ok)\s*[.!]?\s*$",
                  re.IGNORECASE)
_NO = re.compile(r"^\s*(нет|не|неверно|не то|другое|отмена)\s*[.!]?\s*$",
                 re.IGNORECASE)


@dataclass
class Filled:
    """Чем аудитор ответил на замороженный вопрос."""
    slot: str                    # check_id | confirm | reject
    value: Optional[str] = None
    original_query: str = ""
    how: str = ""                # как распознали — в журнал решений


def try_fill(text: str, open_question, ) -> Optional[Filled]:
    """Ответ на уточнение или новый вопрос?

    Возвращает None при малейшем сомнении: принять новый вопрос за ответ на
    старый хуже, чем переспросить — ответ уйдёт не на то, о чём спрашивали,
    и это будет незаметно.
    """
    if open_question is None:
        return None
    raw = (text or "").strip()
    if not raw or len(raw) > 200:
        # Длинная реплика — это новый вопрос, а не ответ «КМ-99-12345»
        return None

    from backend.core import identity

    kms = identity.parse(raw)
    if kms:
        # Номер назван. Если в реплике ТОЛЬКО номер (плюс служебные слова) —
        # это ответ; если вокруг него полноценный вопрос — новый ход.
        stripped = raw
        for km in kms:
            stripped = re.sub(re.escape(km), " ", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"[КKкk][МMмm][\s\-_]*\d{2}[\s\-_]*\d{4,6}", " ", stripped)
        words = [w for w in re.findall(r"[а-яёa-z]+", stripped.lower())
                 if w not in ("км", "по", "проверка", "проверке", "это", "вот")]
        if len(words) <= 2:
            return Filled("check_id", kms[0],
                          open_question.original_query, "номер в ответе")
        return None

    cands = list(open_question.candidates or [])
    low = raw.lower()
    for prefix, idx in _ORDINALS.items():
        if low.startswith(prefix) and idx < len(cands):
            return Filled("check_id", cands[idx],
                          open_question.original_query, f"порядковое «{raw}»")

    # Точное совпадение с предъявленным кандидатом
    for c in cands:
        if low == (c or "").lower():
            return Filled("check_id", c, open_question.original_query,
                          "выбран из списка")

    if _YES.match(raw) and len(cands) == 1:
        return Filled("check_id", cands[0], open_question.original_query,
                      "подтверждение единственного кандидата")
    if _YES.match(raw):
        return Filled("confirm", None, open_question.original_query,
                      "подтверждение")
    if _NO.match(raw):
        return Filled("reject", None, open_question.original_query, "отказ")
    return None
