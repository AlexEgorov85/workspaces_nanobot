"""Follow Up 2.0 — детерминированный судья достаточности.

Это и есть «механизм лучшего ответа»: система не отдаёт то, что нашлось первым,
а сверяет найденное с контрактом и, если не сходится, чинит сама. Ноль вызовов
модели — здесь только арифметика по леджеру.

Ключевое различение, без которого охватные вопросы невозможны: **число проверок
в леджере признаком НЕ является**. «В каких актах встречался Иванов» по природе
многопроверочный. Запрещено другое — когда цитата ОДНОЙ проверки подана как
относящаяся к ДРУГОЙ. Это проверяется построчно, а не по мощности множества.

Лестница заходов почти вся бесплатна: расширить порог, сменить режим, добрать
цитаты, вырезать лишнее — всё это локальная работа на доли секунды. Платный
переплан разрешён не чаще раза за ход, потому что стоит слота очереди.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

from backend.core.contract import (AGGREGATE_KINDS, AnswerContract,
                                   FOCUSED_KINDS, RENDER_BY_KIND)
from backend.core.evidence import Coverage, Evidence, Ledger

logger = logging.getLogger(__name__)


def _cfg():
    from backend.config import get_settings
    return get_settings()


# ──────────────────────────────────────────────────────────────────
# Разрешённый набор проверок
# ──────────────────────────────────────────────────────────────────

@dataclass
class CheckScope:
    """Какие проверки этому ходу вообще позволено называть.

    Владелец один — этот модуль. Поле, которое объявлено, но никем не
    вычисляется, хуже отсутствующего: оно создаёт видимость защиты.
    """
    mode: str                              # closed | corpus_wide
    ids: FrozenSet[str] = frozenset()
    family_of: Dict[str, FrozenSet[str]] = field(default_factory=dict)
    frozen: bool = False
    family_unavailable: bool = False

    def allows(self, check_id: Optional[str]) -> bool:
        if not check_id:
            return True
        if self.mode == "corpus_wide" and not self.frozen:
            return True                    # набор ещё не снят
        if check_id in self.ids:
            return True
        # Семья «головная + дочерние» расширяет допуск, но не набор
        return any(check_id in fam for fam in self.family_of.values())


def build_scope(c: AnswerContract, plan: List, focus_id: Optional[str],
                named_ids: Optional[Set[str]] = None) -> CheckScope:
    """Набор считается ДО исполнения плана — из памяти и плана, а не из выдачи.

    Иначе разность «леджер минус разрешённое» пуста при любом результате
    поиска, и защита от чужой проверки самоподтверждается.
    """
    from backend.core.tools import registry
    named = set(named_ids or ())
    seed = ({focus_id} if focus_id else set()) | named
    body = _body_step(plan)
    wide = body is not None and _is_corpus_wide(body)
    fam, fam_down = _family_map(seed)
    if not seed:
        return CheckScope("corpus_wide", frozenset(), {}, False, fam_down)
    return CheckScope("corpus_wide" if wide else "closed", frozenset(seed),
                      fam, False, fam_down)


def _body_step(plan: List):
    """Шаг тела задаётся РОЛЬЮ, а не типом результата.

    Единственное определение владельца тела во всём конвейере: если бы момент
    заморозки задавался типом результата, а состав — ролью, они разъехались бы.
    """
    from backend.core.tools import registry
    for step in plan or []:
        name = step.get("tool") if isinstance(step, dict) else getattr(step, "tool", None)
        if not name:
            continue
        try:
            spec, _ = registry.get(name)
        except KeyError:
            continue
        if spec.role == "body":
            return step
    return None


def _is_corpus_wide(step) -> bool:
    from backend.core.tools import registry
    name = step.get("tool") if isinstance(step, dict) else getattr(step, "tool", "")
    try:
        return registry.get(name)[0].corpus_wide
    except KeyError:
        return False


def _family_map(ids: Set[str]) -> Tuple[Dict[str, FrozenSet[str]], bool]:
    """Семьи «головная КМ + дочерние»: один акт на несколько номеров.

    Витрина недоступна → семьи пусты И об этом сказано: молча считать семью
    пустой значит превратить работающее доменное поведение в «чужая проверка».
    """
    if not ids:
        return {}, False
    try:
        from backend.agents.execution_control import fetch_rows, km_family
        from backend.core import identity
        rows = fetch_rows()
        if not rows:
            return {}, True
        out: Dict[str, FrozenSet[str]] = {}
        for cid in ids:
            bare = identity.to_bare(cid)
            mine = [r for r in rows if r.get("km_id") == bare]
            fam = km_family(mine, rows) if mine else []
            out[cid] = frozenset(identity.format(k) for k in fam) | {cid}
        return out, False
    except Exception as e:
        logger.debug(f"[critic] Семьи не построены: {e}")
        return {}, True


def freeze_scope(scope: CheckScope, ledger: Ledger) -> CheckScope:
    """Снять набор по возврату шага тела. Заморозка ОДНОСТОРОННЯЯ.

    Именно это делает сигнал «чужая проверка» непустым: он сравнивает результат
    второго захода с набором, снятым на первом, а не леджер сам с собой.
    """
    if scope.frozen:
        return scope
    return replace(scope, ids=frozenset(ledger.checks()) | scope.ids,
                   frozen=True)


# ──────────────────────────────────────────────────────────────────
# Пол доказательности
# ──────────────────────────────────────────────────────────────────

def enforce_floor(c: AnswerContract, plan: List, focus_id: Optional[str],
                  named_ids: Optional[Set[str]] = None) -> AnswerContract:
    """Умеет ужесточать, не умеет ослаблять."""
    from backend.core.tools import registry

    produces = {_produces(s) for s in (plan or [])}
    if produces & {"PASSAGE", "DOC", "ENTITY"}:
        c = replace(c, needs_quote=True, min_quotes=max(c.min_quotes, 1))

    # ДЕГРАДАЦИЯ ВИДА — строго до пола агрегата, иначе на контракте останется
    # требование строки полноты от вида, которого уже нет
    body = _body_step(plan)
    if (focus_id is None and not (named_ids or ())
            and body is not None and not _is_corpus_wide(body)):
        c = replace(c, kind="passages")

    if c.kind in AGGREGATE_KINDS:
        c = replace(c, needs_coverage_line=True,
                    min_field_fill=c.min_field_fill or _cfg().critic_min_field_fill)

    # ОБЛАСТЬ пересчитывается детерминированно; предложение модели — в журнал
    c = replace(c, proposed_scope=c.scope,
                scope=("focused" if (c.kind in FOCUSED_KINDS
                                     or (c.kind == "passages" and focus_id))
                       else "corpus"))
    scope = build_scope(c, plan, focus_id, named_ids)
    c = replace(c, allowed_checks=scope.ids)
    if c.kind in RENDER_BY_KIND:
        c = replace(c, render=RENDER_BY_KIND[c.kind])
    return c


def _produces(step) -> str:
    from backend.core.tools import registry
    name = step.get("tool") if isinstance(step, dict) else getattr(step, "tool", "")
    try:
        return registry.get(name)[0].produces
    except KeyError:
        return ""


# ──────────────────────────────────────────────────────────────────
# Строки тела
# ──────────────────────────────────────────────────────────────────

@dataclass
class Claim:
    """Строка тела ответа со своей проверкой и своими доказательствами.

    Единица проверки — строка, а не весь ответ: только так «перечисление многих
    проверок» отличается от «смешения артефактов разных проверок».
    """
    text: str
    check_id: Optional[str] = None
    evidence_uids: List[str] = field(default_factory=list)
    axis: Optional[str] = None      # строка про корпус в целом, не про проверку


# ──────────────────────────────────────────────────────────────────
# Вердикт
# ──────────────────────────────────────────────────────────────────

@dataclass
class Verdict:
    action: str                     # accept|widen|switch_mode|enrich|prune|ask|disclose|refuse|replan
    signals: List[str] = field(default_factory=list)
    gaps: List[str] = field(default_factory=list)
    suggestion: str = ""
    blocking: bool = False


def judge(c: AnswerContract, ledger: Ledger, claims: List[Claim],
          budget, scope: CheckScope, health: Optional[Dict] = None,
          relevance: Optional["Relevance"] = None,
          escalations: Optional[Dict[str, int]] = None) -> Verdict:
    """Контракт ⨯ леджер ⨯ строки → что делать дальше."""
    cfg = _cfg()
    esc = escalations or {}
    signals: List[str] = []
    gaps: List[str] = []
    cov = ledger.coverage()

    if len(ledger) == 0:
        signals.append("empty")

    quotes = [e for e in ledger.quotes()]
    if c.needs_quote and len(quotes) < max(1, c.min_quotes):
        signals.append("quote_gap")
        gaps.append("нет дословной цитаты")

    if c.min_docs and len(ledger.checks()) < c.min_docs:
        signals.append("doc_gap")

    if c.needs_coverage_line and cov.scanned <= 0:
        signals.append("no_coverage")

    if c.min_field_fill:
        worst = min(cov.field_fill.values()) if cov.field_fill else 1.0
        if worst < c.min_field_fill:
            signals.append("field_gap")
            gaps.append(f"поле заполнено у {worst:.0%} строк")

    if cov.truncated:
        signals.append("truncation")

    for src in ledger.degraded_sources():
        signals.append("source_unavailable")
        gaps.append(f"источник недоступен: {src}")
        break

    if health and not health.get("ok", True):
        signals.append("index_unhealthy")

    # КОНТАМИНАЦИЯ: построчно, а не по мощности множества
    contaminated = _contaminated(claims, ledger, scope, c)
    if contaminated:
        signals.append("check_contamination")

    if scope.frozen:
        alien = [cl for cl in claims
                 if cl.check_id and not scope.allows(cl.check_id)]
        if alien:
            signals.append("foreign_evidence")

    if relevance is not None:
        signals.extend(relevance.signals)

    # ── лестница ────────────────────────────────────────────────
    def can(name: str, limit: int) -> bool:
        return esc.get(name, 0) < limit

    if "check_contamination" in signals or "foreign_evidence" in signals:
        return Verdict("prune", signals, gaps,
                       "убираю строки, чья цитата относится к другой проверке")
    if ("quote_gap" in signals and ledger.checks()
            and can("enrich", 1)):
        return Verdict("enrich", signals, gaps, "добираю цитаты")
    if ("empty" in signals or "doc_gap" in signals or
            ("truncation" in signals and len(ledger) < 5)) and can("widen", cfg.critic_max_widen):
        return Verdict("widen", signals, gaps, "расширяю поиск")
    if "field_gap" in signals and can("switch_mode", cfg.critic_max_switch):
        return Verdict("switch_mode", signals, gaps,
                       "добираю значения из текста актов")
    if relevance is not None and relevance.hard_miss and can("replan", 1) \
            and budget.can_afford(1) and cfg.critic_relevance_enforced:
        return Verdict("replan", signals, gaps, "материал не по адресу")
    if "empty" in signals:
        return Verdict("ask", signals, gaps,
                       "в корпусе ничего не нашлось — уточните формулировку",
                       blocking=True)
    if gaps:
        return Verdict("disclose", signals, gaps, "отвечаю с оговоркой")
    return Verdict("accept", signals, gaps)


def _contaminated(claims: List[Claim], ledger: Ledger, scope: CheckScope,
                  c: AnswerContract) -> List[Claim]:
    """Цитата принадлежит ЧУЖОЙ проверке — вот это и запрещено.

    Многопроверочность признаком не является: coverage, entity_rollup и facets
    многопроверочны по природе, и считать их смешением значило бы запретить
    охватные вопросы целиком.
    """
    by_uid = {e.uid: e for e in ledger.evidence()}
    bad: List[Claim] = []
    for cl in claims:
        if cl.axis:
            continue
        if cl.check_id is None and cl.evidence_uids:
            bad.append(cl)
            continue
        fam = scope.family_of.get(cl.check_id or "", frozenset())
        allowed = {cl.check_id} | set(fam)
        for uid in cl.evidence_uids:
            ev = by_uid.get(uid)
            if ev and ev.check_id and ev.check_id not in allowed:
                bad.append(cl)
                break
        if c.scope == "focused" and cl.check_id and not scope.allows(cl.check_id):
            bad.append(cl)
    return bad


def prune(claims: List[Claim], ledger: Ledger, scope: CheckScope,
          c: AnswerContract) -> Tuple[List[Claim], List[Claim]]:
    """Вырезать строки, а не отказываться от ответа."""
    bad = {id(x) for x in _contaminated(claims, ledger, scope, c)}
    kept = [cl for cl in claims if id(cl) not in bad]
    dropped = [cl for cl in claims if id(cl) in bad]
    return kept, dropped


# ──────────────────────────────────────────────────────────────────
# Релевантность
# ──────────────────────────────────────────────────────────────────

@dataclass
class Relevance:
    term_coverage: float = 1.0
    uncovered_terms: List[str] = field(default_factory=list)
    absent_in_corpus: List[str] = field(default_factory=list)
    shape_ok: bool = True
    signals: List[str] = field(default_factory=list)
    hard_miss: bool = False


def relevance(question: str, c: AnswerContract, ledger: Ledger) -> Relevance:
    """Отвечает ли найденное на ЗАДАННОЕ.

    Первые сигналы измеряют объём, провенанс и форму. Ни один не спрашивает,
    про то ли вообще материал: обильный, процитированный и непротиворечивый
    ответ не по адресу проходил бы как хороший.

    Считается на уже оплаченном: токенизация вопроса и цитат — единицы
    миллисекунд, словарь IDF уже в памяти.
    """
    from backend.indexing.lexicon import content_terms

    terms = content_terms(question)
    if not terms:
        return Relevance()

    idf = _idf_map()
    content = [t for t in terms if idf.get(t, 0.0) >= _idf_floor(idf)]
    absent = [t for t in content if t not in idf]
    checkable = [t for t in content if t in idf]

    blob = " ".join(
        (e.quote or "") + " " + (e.header_path or "") + " " +
        " ".join(str(v) for v in (e.fields or {}).values())
        for e in ledger.evidence())
    found = set(content_terms(blob))
    covered = [t for t in checkable if t in found]

    weight = lambda ts: sum(idf.get(t, 1.0) for t in ts)  # noqa: E731
    cov = (weight(covered) / weight(checkable)) if checkable else 1.0

    signals: List[str] = []
    cfg = _cfg()
    if checkable and cov < cfg.critic_min_term_coverage:
        signals.append("question_terms_gap")

    # Форма ответа против формы вопроса: спросили «в каких актах» — в леджере
    # должны быть документы, а не пять абзацев одного акта
    shape_ok = True
    if c.kind in ("coverage", "entity_rollup") and len(ledger.checks()) <= 1:
        shape_ok = False
        signals.append("shape_gap")
    if c.kind == "facets" and not any(e.fields for e in ledger.evidence()):
        shape_ok = False
        signals.append("shape_gap")

    return Relevance(
        term_coverage=round(cov, 2),
        uncovered_terms=[t for t in checkable if t not in found],
        absent_in_corpus=absent, shape_ok=shape_ok, signals=signals,
        hard_miss=(not shape_ok) or (bool(checkable) and cov < 0.15))


_idf_cache: Optional[Dict[str, float]] = None
_idf_cache_version: Optional[int] = None


def _idf_map() -> Dict[str, float]:
    """Словарь IDF корпуса. Кэш привязан к версии лексикона.

    Без привязки кэш переживал бы пересборку BM25 со стеммером: покрытие термов
    считалось бы по НЕСТЕММИРОВАННОМУ словарю против стеммированных термов
    вопроса, совпадений не было бы почти никогда, и сигнал релевантности врал бы
    в одну сторону — «материал не по адресу» на нормальных ответах.
    """
    global _idf_cache, _idf_cache_version
    try:
        from backend.indexing.index_builder import load_bm25
        data = load_bm25()
        version = data.get("lexicon_version")
        if _idf_cache is not None and _idf_cache_version == version:
            return _idf_cache
        _idf_cache = dict(data["bm25"].idf)
        _idf_cache_version = version
    except Exception:
        _idf_cache, _idf_cache_version = {}, None
    return _idf_cache


def invalidate_idf_cache() -> None:
    global _idf_cache, _idf_cache_version
    _idf_cache, _idf_cache_version = None, None


def _idf_floor(idf: Dict[str, float]) -> float:
    """Содержательность терма — не список стоп-слов, а его же IDF.

    Списка в репозитории нет, а сочинённый был бы тем же магическим числом
    мимо конфига. «Какие», «по», «теме» отсекаются сами.
    """
    if not idf:
        return 0.0
    avg = sum(idf.values()) / len(idf)
    return _cfg().critic_terms_idf_floor * avg


def judge_final(c: AnswerContract, verified_text: str, claims: List[Claim],
                ledger: Ledger) -> Verdict:
    """Перепроверка ПОСЛЕ верификатора.

    Верификатор вырезает выдуманные ссылки — и ответ, требовавший цитаты,
    может остаться без единой. Без этой проверки он уехал бы к аудитору как
    полноценный.
    """
    signals: List[str] = []
    if c.needs_quote:
        has = any(cl.evidence_uids for cl in claims)
        if not has or not verified_text.strip():
            signals.append("quote_gap_after_verify")
            return Verdict("disclose", signals,
                           ["после проверки не осталось подтверждённых цитат"],
                           "тело пересобрано из леджера", blocking=True)
    return Verdict("accept", signals)
