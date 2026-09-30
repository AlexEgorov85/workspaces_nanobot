"""Follow Up 2.0 — идентичность проверки (КМ).

Единственный источник правды о номере проверки. До этого модуля свой регекс жил
в шести местах, и они расходились: `card_to_markdown` печатал «КМ 99-12345» через
пробел, а память искала номер регексом, требующим дефис, — собственный вывод
системы был невидим её же памяти.

Доменное правило, ради которого модуль существует: каждая КМ — отдельная проверка.
Ни одна функция здесь не «угадывает» номер и не подставляет ближайший: при
неоднозначности возвращается `Ambiguous` со списком кандидатов, а выбор делает
аудитор.

Формы записи:
    канон           КМ-99-12345     (кириллические КМ, дефис, 2 + 4-6 цифр)
    голая           99-12345        (внутреннее представление витрины и карточки)
    входные         «КМ 99-12345», «KM_99_12345», «км-99-12345», «КМ99-12345»

Почему хвост 4-6 цифр при явном префиксе и 5-6 без него: витрина пропускает
`^\\d{2}-\\d{4,6}$` (`gp.py:844`), поэтому короткий номер обязан распознаваться;
но голые «01-2024» в имени файла номером проверки не являются, и расширять
беспрефиксный шаблон до 4 знаков нельзя (`converter.py:40`).
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Iterable, List, Literal, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────
# Формы записи
# ──────────────────────────────────────────────────────────────────

CANONICAL_RE = re.compile(r"^КМ-\d{2}-\d{4,6}$")

# С явным префиксом: разделитель между префиксом и цифрами необязателен,
# между группами цифр — обязателен (иначе «КМ 9912345» стало бы номером).
# Разделители: дефис, подчёркивание, а также en/em dash — модель охотно
# ставит «КМ–99–12345», и без них номер из её ответа не распознавался.
_SEP = r"[-_\u2010-\u2015]"
_WITH_PREFIX = re.compile(
    rf"[КKкk][МMмm]\s*(?:{_SEP}|\s)?\s*(\d{{2}})\s*{_SEP}\s*(\d{{4,6}})(?!\d)",
    re.UNICODE,
)

# Без префикса — только 5-6 знаков в хвосте: «01-2024» проверкой не является.
_BARE = re.compile(rf"(?<!\d)(\d{{2}}){_SEP}(\d{{5,6}})(?!\d)")

# Голая форма целиком (для значений из витрины и km_id карточки)
_BARE_FULL = re.compile(rf"^(\d{{2}}){_SEP}(\d{{4,6}})$")


def canonical(head: str, tail: str) -> str:
    return f"КМ-{head}-{tail}"


def normalize(text: Optional[str]) -> Optional[str]:
    """Одно значение → канон `КМ-99-12345`, либо None если номера нет.

    Принимает и голую форму витрины (`99-12345`), и любую из входных.
    `UNKNOWN`, пустая строка и мусор дают None — и это не ошибка, а класс
    «нераспознанные»: такие документы участвуют в поиске, но приписать их
    проверке нельзя.

    ВАЖНО про хвост из 4 цифр. Значение ЦЕЛИКОМ вида `99-1234` принимается —
    иначе не пройдут номера витрины (`^\\d{2}-\\d{4,6}$`, `gp.py:844`). Побочно
    это значит, что `normalize('01-2024')` вернёт `КМ-01-2024`: для одиночного
    значения поля отличить его от номера проверки нечем. Защита — `validate()`
    против справочника: несуществующий номер даёт `Unknown`. Поэтому `normalize`
    зовут на значениях полей, а на свободном тексте — `parse()`, где голый
    шаблон требует 5-6 знаков и дату не подхватывает.
    """
    if not text:
        return None
    s = str(text).strip()
    if not s or s.upper() == "UNKNOWN":
        return None

    m = _BARE_FULL.match(s)          # чистое значение витрины — самый частый вход
    if m:
        return canonical(m.group(1), m.group(2))
    m = _WITH_PREFIX.search(s)
    if m:
        return canonical(m.group(1), m.group(2))
    m = _BARE.search(s)
    if m:
        return canonical(m.group(1), m.group(2))
    return None


def parse(text: Optional[str]) -> List[str]:
    """Все номера из текста, канонизированные, в порядке появления, без повторов.

    Сначала вычитываются вхождения с явным префиксом, затем голые — из остатка
    текста, чтобы «КМ-99-12345» не дал ещё и голое совпадение внутри себя.
    """
    if not text:
        return []
    out: List[str] = []
    rest_parts: List[str] = []
    pos = 0
    for m in _WITH_PREFIX.finditer(text):
        km = canonical(m.group(1), m.group(2))
        if km not in out:
            out.append(km)
        rest_parts.append(text[pos:m.start()])
        pos = m.end()
    rest_parts.append(text[pos:])

    for m in _BARE.finditer("\n".join(rest_parts)):
        km = canonical(m.group(1), m.group(2))
        if km not in out:
            out.append(km)
    return out


def format(km: Optional[str], *, bare: bool = False) -> str:
    """Канон для показа человеку, голая форма — для витрины, GP и `km_id` карточки.

    Незнакомое значение возвращается как есть: печатать «КМ-None» хуже, чем
    напечатать то, что пришло.
    """
    norm = normalize(km)
    if norm is None:
        return str(km or "")
    return norm[3:] if bare else norm


def to_bare(km: Optional[str]) -> str:
    return format(km, bare=True)


# ──────────────────────────────────────────────────────────────────
# Справочник: три источника
# ──────────────────────────────────────────────────────────────────

Outcome = Literal["Exact", "Ambiguous", "KnownNotHydrated", "Unknown"]


@dataclass(frozen=True)
class Verdict:
    outcome: Outcome
    check_id: Optional[str] = None
    candidates: Tuple[str, ...] = ()
    degraded_sources: Tuple[str, ...] = ()

    @property
    def is_exact(self) -> bool:
        return self.outcome == "Exact"


@dataclass(frozen=True)
class Registry:
    """Что система вообще знает о существующих проверках.

    `hydrated` — есть в корпусе, можно отвечать по содержимому.
    `vitrina`  — витрина знает, гидратация ещё не довезла: штатное состояние
                 контура (`act_backfill.missing_kms()`), а не сбой.
    """
    hydrated: frozenset
    vitrina: frozenset
    degraded_sources: Tuple[str, ...] = ()
    ts: float = 0.0

    @property
    def known(self) -> frozenset:
        return self.hydrated | self.vitrina


_lock = threading.Lock()
_cached: Optional[Registry] = None


def _ttl() -> float:
    try:
        from backend.config import get_settings
        return float(get_settings().identity_registry_ttl_sec)
    except Exception:
        return 60.0


def _collect() -> Registry:
    hydrated: set = set()
    vitrina: set = set()
    degraded: List[str] = []

    try:
        from backend.storage.database import DocumentRepo, get_db
        with get_db() as db:
            for v in DocumentRepo.list_check_ids(db):
                n = normalize(v)
                if n:
                    hydrated.add(n)
    except Exception as e:
        logger.warning(f"[identity] Локальный справочник недоступен: {e}")
        degraded.append("sqlite")

    try:
        from backend.storage import gp
        if gp.gp_enabled():
            for v in gp.ActGPRepo.corpus_check_ids():
                n = normalize(v)
                if n:
                    hydrated.add(n)
            if gp.ActVitrinaRepo.available():
                for v in gp.ActVitrinaRepo.distinct_kms():
                    n = normalize(v)
                    if n:
                        vitrina.add(n)
    except Exception as e:
        # Витрина/корпус недоступны — работаем на локальных, но молчать нельзя:
        # иначе «акт есть в витрине» превратится в «такой проверки нет».
        logger.warning(f"[identity] Greenplum недоступен: {e}")
        degraded.append("gp")

    return Registry(frozenset(hydrated), frozenset(vitrina - hydrated),
                    tuple(degraded), time.time())


def registry(force: bool = False) -> Registry:
    global _cached
    with _lock:
        if (not force and _cached is not None
                and time.time() - _cached.ts < _ttl()):
            return _cached
        _cached = _collect()
        return _cached


def invalidate() -> None:
    """Зовут гидратация и бэкофилл после добавления документов."""
    global _cached
    with _lock:
        _cached = None


# ──────────────────────────────────────────────────────────────────
# Валидация
# ──────────────────────────────────────────────────────────────────

def validate(raw: Optional[str], reg: Optional[Registry] = None) -> Verdict:
    """Номер → один из четырёх исходов. Ближайшую проверку НЕ подставляет.

    `Ambiguous` возникает на коротком хвосте: витрина пропускает 4 знака, и
    «КМ-99-1234» может оказаться началом нескольких реальных номеров. Выбор
    в этом случае принадлежит аудитору — вернуть «наверное, вот эта» означало
    бы подставить чужую проверку.
    """
    reg = reg or registry()
    norm = normalize(raw)
    if norm is None:
        return Verdict("Unknown", None, (), reg.degraded_sources)
    if norm in reg.hydrated:
        return Verdict("Exact", norm, (), reg.degraded_sources)
    if norm in reg.vitrina:
        return Verdict("KnownNotHydrated", norm, (), reg.degraded_sources)

    head, tail = norm[3:5], norm[6:]
    if len(tail) < 5:
        cands = tuple(sorted(
            k for k in reg.known
            if k[3:5] == head and k[6:].startswith(tail)))
        if len(cands) == 1:
            outcome = "Exact" if cands[0] in reg.hydrated else "KnownNotHydrated"
            return Verdict(outcome, cands[0], (), reg.degraded_sources)
        if len(cands) > 1:
            return Verdict("Ambiguous", None, cands, reg.degraded_sources)
    return Verdict("Unknown", norm, (), reg.degraded_sources)


def known_check_ids() -> frozenset:
    return registry().known
