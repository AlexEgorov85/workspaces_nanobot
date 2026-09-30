"""Follow Up 2.0 — построчная проверка того, что модель написала.

Прод-факт, ради которого модуль существует: модель дописывает правдоподобные
номера проверок, которых в корпусе нет. В банковском аудите ссылка на
несуществующий акт — это не косметика.

Проверяется ПОСТРОЧНО, а не по всему тексту: строка, назвавшая чужую проверку,
вырезается целиком, а не превращается в текст без ссылки. Ответ без ссылки
выглядит утверждением от себя, и это хуже, чем его отсутствие.

После вырезания контракт перепроверяется (`critic.judge_final`): ответ,
требовавший цитаты, мог остаться без единой.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import FrozenSet, List, Optional, Set

from backend.core.critic import Claim
from backend.core.evidence import Ledger

logger = logging.getLogger(__name__)


@dataclass
class VerifiedAnswer:
    text: str
    dropped_claims: List[str] = field(default_factory=list)
    foreign_km: List[str] = field(default_factory=list)
    unverified_numbers: List[str] = field(default_factory=list)
    body_replaced_after_verify: bool = False


def verify(md: str, claims: List[Claim], ledger: Ledger,
           allowed_checks: FrozenSet[str],
           known_check_ids: Optional[Set[str]] = None) -> VerifiedAnswer:
    """Вырезает строки с чужими и выдуманными номерами проверок."""
    from backend.core import identity

    known = known_check_ids
    if known is None:
        try:
            known = set(identity.known_check_ids())
        except Exception:
            known = set()

    ledger_checks = set(ledger.checks())
    allowed = set(allowed_checks or ()) | ledger_checks

    foreign: List[str] = []
    kept_lines: List[str] = []
    dropped: List[str] = []

    for line in (md or "").split("\n"):
        mentioned = identity.parse(line)
        bad = [km for km in mentioned
               if (known and km not in known) or (allowed and km not in allowed)]
        if bad:
            foreign.extend(b for b in bad if b not in foreign)
            dropped.append(line.strip()[:120])
            continue
        kept_lines.append(line)

    text = "\n".join(kept_lines).strip()
    if foreign:
        logger.warning(f"[verify] Вырезаны ссылки на проверки вне набора: "
                       f"{foreign}")
    return VerifiedAnswer(text=text, dropped_claims=dropped,
                          foreign_km=foreign)


def rebuild_body_if_needed(verified: VerifiedAnswer, body_md: str,
                           frame_md: str) -> VerifiedAnswer:
    """Если после вырезания текста не осталось — собрать заново из леджера.

    Тело детерминированное, и пересборка стоит миллисекунды: платить за неё
    ещё одним вызовом модели незачем.
    """
    if verified.text.strip():
        return verified
    verified.text = (frame_md.strip() + "\n\n" + body_md.strip()).strip()
    verified.body_replaced_after_verify = True
    return verified
