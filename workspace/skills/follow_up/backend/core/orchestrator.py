"""Follow Up 2.0 — ход целиком: понять → исполнить → судить → ответить.

Порядок здесь и есть механизм «лучшего ответа»:

1. **понимание** — один вызов модели, план и контракт;
2. **пол доказательности** — контракт ужесточается детерминированно, сбой
   понимания не превращается в сбой доказательности;
3. **исполнение** — инструменты кладут типизированные доказательства в леджер;
4. **суд** — арифметика по леджеру: хватает ли материала, тот ли он, не смешаны
   ли проверки. При нехватке система чинит СЕБЯ, а не спрашивает аудитора:
   почти вся лестница заходов бесплатна;
5. **тело** — рендерится кодом и уходит в интерфейс ДО генерации;
6. **фрейм** — короткий вызов модели на шапку и вывод;
7. **проверка** — построчно вырезаются ссылки на проверки вне набора, после
   чего контракт перепроверяется по выжившему тексту.

Бюджет — три слота на всё. Переплан платный и разрешён раз за ход.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import AsyncGenerator, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Таблицу переписывать нельзя — она уже на экране и в ней числа. А вот на
# прозаический вопрос («какие кейсы были в акте») сырые фрагменты ответом не
# являются: аудитор получал титульный лист и таблицу тарифов вместо перечня
# случаев. Поэтому два разных промпта, а не один.
FRAME_SYSTEM = """Ты — помощник ИТ-аудитора банка. Тебе дают ГОТОВОЕ тело ответа
(таблицу) и вопрос. Напиши ТОЛЬКО две вещи:

1. первую строку-шапку: что найдено, одним предложением;
2. абзац вывода после тела: что это значит для аудитора.

ЗАПРЕЩЕНО: переписывать тело, менять числа, называть номера проверок, которых
нет в теле, добавлять факты от себя. Тело уже показано аудитору — не дублируй.

Формат:
ШАПКА: <одно предложение>
ВЫВОД: <2-4 предложения>"""

PROSE_SYSTEM = """Ты — помощник ИТ-аудитора банка. Тебе дают ВОПРОС и ФРАГМЕНТЫ
акта проверки. Ответь на вопрос по этим фрагментам.

КАК ОТВЕЧАТЬ:
- отвечай на ТОТ вопрос, который задан. Спросили про кейсы или нарушения —
  перечисли их по пунктам, а не пересказывай, о чём акт вообще;
- каждый пункт — что произошло, в каком объёме, чем это нарушает; если во
  фрагментах есть цифры и суммы, приводи их;
- после каждого пункта ставь ссылку на проверку в квадратных скобках, например
  [КМ-99-12348];
- если во фрагментах ответа на вопрос НЕТ — скажи это прямо и перечисли, что в
  них есть вместо этого. Не выдавай оглавление акта за ответ.

ЗАПРЕЩЕНО: выдумывать факты и цифры, которых нет во фрагментах; называть номера
проверок, которых нет во фрагментах; копировать таблицы целиком.

Пиши по-русски, по делу, без вводных фраз вроде «в предоставленных фрагментах»."""


@dataclass
class TurnResult:
    text: str = ""
    body_md: str = ""
    frame_md: str = ""
    status: str = "ok"                    # ok | degraded | refused
    contract: Optional[object] = None
    verdicts: List[str] = field(default_factory=list)
    signals: List[str] = field(default_factory=list)
    sources: List[Dict] = field(default_factory=list)
    checks: List[str] = field(default_factory=list)
    coverage_line: str = ""
    budget: Dict = field(default_factory=dict)
    understanding: Dict = field(default_factory=dict)
    body_replaced_after_verify: bool = False


async def run_turn(question: str, session_id: int, *, model=None,
                   cancel=None, emit: Optional[Callable] = None,
                   focus: Optional[str] = None,
                   named_ids: Optional[set] = None,
                   surface: str = "web") -> TurnResult:
    from backend.core import critic, interpreter, render, verify as verifier
    from backend.core.budget import TurnBudget
    from backend.core.contract import AnswerContract
    from backend.core.evidence import Ledger
    from backend.core.tools import registry
    from backend.config import get_settings

    cfg = get_settings()
    say = emit or (lambda *_a, **_k: None)
    budget = TurnBudget(max_llm_calls=cfg.turn_max_llm_calls,
                        deadline_sec=cfg.turn_deadline_sec)

    # 1. Понимание
    await say("interpreting", "Понимаю вопрос относительно диалога…")
    digest = _memory_digest(session_id, focus)
    u = await interpreter.understand(question, digest, budget, cancel, model)
    if u.degraded or not u.plan:
        u = interpreter.fallback_plan(question, focus)
        await say("degraded_plan", "Отвечаю по упрощённому плану")
    if u.focus and not focus:
        focus = u.focus

    # 2. Пол доказательности
    c = AnswerContract(kind=(u.contract or {}).get("kind", "passages"),
                       needs_quote=bool((u.contract or {}).get("needs_quote")))
    c = critic.enforce_floor(c, u.plan, focus, named_ids)
    scope = critic.build_scope(c, u.plan, focus, named_ids)

    # 3. Исполнение
    ledger = Ledger()
    escalations: Dict[str, int] = {}
    await say("searching", _plan_human(u.plan))
    await _execute(u.plan, ledger, scope, cancel, say)

    # 4. Суд и заходы
    body_md, claims = render.render_body(c, ledger)
    rel = critic.relevance(question, c, ledger)
    verdicts: List[str] = []
    for _ in range(3):
        v = critic.judge(c, ledger, claims, budget, scope,
                         health=_index_health(), relevance=rel,
                         escalations=escalations)
        verdicts.append(v.action)
        if v.action in ("accept", "disclose", "refuse", "ask"):
            break
        escalations[v.action] = escalations.get(v.action, 0) + 1
        await say(v.action, v.suggestion or v.action)
        changed = await _escalate(v, u, c, ledger, scope, cancel, say, budget,
                                  question)
        if not changed:
            break
        body_md, claims = render.render_body(c, ledger)
        rel = critic.relevance(question, c, ledger)
    final_verdict = verdicts[-1] if verdicts else "accept"

    if v.action == "prune":
        claims, dropped = critic.prune(claims, ledger, scope, c)
        if dropped:
            await say("pruned", f"Убрал строк: {len(dropped)} — цитата "
                                f"относилась к другой проверке")
            body_md = "\n".join(cl.text for cl in claims)

    cov_line = render.coverage_line(ledger, rel, _index_health())

    # 5. Тело. Таблица уходит на экран СРАЗУ — она и есть ответ, а её длина
    # это чистое ожидание. Для прозаического вопроса фрагменты ответом не
    # являются: аудитор спрашивал про кейсы, а получал титульный лист. Их
    # читает модель, а на экран они идут свёрнутым списком источников.
    is_prose = (c.render or "quotes") == "quotes"
    if not is_prose:
        await say("facts", {"body_md": body_md, "coverage": cov_line,
                            "checks": sorted(ledger.checks())}, structured=True)

    # 6. Ответ
    frame = ""
    if budget.can_afford(1) and len(ledger):
        frame = (await _prose(question, ledger, cov_line, budget, model)
                 if is_prose
                 else await _frame(question, body_md, cov_line, budget, model))
    if not frame:
        frame = _template_frame(c, ledger, cov_line)

    if is_prose:
        # Источники под ответом, а не вместо него
        src = render.sources_block(ledger, surface)
        text = "\n\n".join(p for p in (frame, src, f"_{cov_line}_") if p).strip()
        body_md = src
    else:
        text = f"{frame}\n\n{body_md}\n\n_{cov_line}_".strip()

    # 7. Проверка построчно
    ver = verifier.verify(text, claims, ledger, c.allowed_checks)
    ver = verifier.rebuild_body_if_needed(ver, body_md, frame)
    fin = critic.judge_final(c, ver.text, claims, ledger)
    status = "ok"
    if ver.foreign_km or fin.action != "accept":
        status = "degraded"
    if final_verdict in ("disclose",) and fin.action == "accept":
        status = "degraded"

    return TurnResult(
        text=ver.text, body_md=body_md, frame_md=frame, status=status,
        contract=c, verdicts=verdicts, signals=list(dict.fromkeys(
            [s for s in (rel.signals or [])] + verdicts)),
        sources=[{"check_id": e.check_id, "chunk_index": 0,
                  "text_preview": (e.quote or "")[:200],
                  "header_path": e.header_path}
                 for e in ledger.evidence()[:10]],
        checks=sorted(ledger.checks()), coverage_line=cov_line,
        budget=budget.report(),
        understanding={"focus": focus, "why": u.why,
                       "plan": [s.get("tool") for s in u.plan],
                       "contract": c.as_dict(), "degraded": u.degraded},
        body_replaced_after_verify=ver.body_replaced_after_verify)


# ──────────────────────────────────────────────────────────────────

async def _execute(plan, ledger, scope, cancel, say) -> None:
    from backend.core import critic
    from backend.core.tools import registry
    body_seen = False
    for step in plan[:3]:
        name = step.get("tool")
        try:
            spec, fn = registry.get(name)
            args = registry.validate(name, step.get("args") or {})
        except (KeyError, ValueError) as e:
            logger.warning(f"[orchestrator] Шаг {name} пропущен: {e}")
            continue
        try:
            res = await fn(cancel=cancel, **args)
        except TypeError:
            res = await fn(**args)
        except Exception as e:
            logger.warning(f"[orchestrator] {name}: {e}")
            continue
        ledger.add(res)
        if spec.role == "body" and not body_seen:
            body_seen = True
            frozen = critic.freeze_scope(scope, ledger)
            scope.ids, scope.frozen = frozen.ids, frozen.frozen


async def _escalate(v, u, c, ledger, scope, cancel, say, budget,
                    question: str = "") -> bool:
    """Бесплатные заходы. Возвращает True, если материал изменился."""
    from backend.core.tools import registry
    if v.action == "widen":
        # Охватный поиск НЕ годится как добор для вопроса про упоминание:
        # на одной фамилии эмбеддинг слабо совпадает со всем подряд, и в ответ
        # приходят десятки актов с пустыми цитатами — выглядит как найденные
        # упоминания, хотя это шум. Пустой ответ честнее такого списка.
        if c.kind == "entity_rollup":
            await say("widen_skipped",
                      "Расширять поиск нечем: упоминание либо есть, либо нет")
            return False
        step = next((s for s in u.plan if s.get("tool") == "search_coverage"),
                    None) or {"tool": "search_coverage", "args": {}}
        args = dict(step.get("args") or {})
        # Запрос берётся из любого шага плана, а при отсутствии — из самого
        # вопроса. Иначе заход был бы холостым для всех планов без
        # search_coverage, то есть для большинства: критик просил бы расширить
        # поиск, ничего не происходило бы, и цикл крутился впустую.
        args["query"] = (args.get("query")
                         or next((s.get("args", {}).get("query")
                                  for s in u.plan
                                  if s.get("args", {}).get("query")), None)
                         or question)
        if not args["query"]:
            return False
        args["max_docs"] = int(args.get("max_docs") or 30) * 2
        try:
            _, fn = registry.get("search_coverage")
            ledger.add(await fn(cancel=cancel, **args))
            return True
        except Exception:
            return False
    if v.action == "switch_mode":
        try:
            _, fn = registry.get("semantic_deviations")
            q = next((s.get("args", {}).get("query") for s in u.plan
                      if s.get("args", {}).get("query")), None) or question
            if not q:
                return False
            ledger.add(await fn(query=q, cancel=cancel))
            return True
        except Exception:
            return False
    if v.action == "enrich":
        try:
            _, fn = registry.get("read_document")
            for cid in list(ledger.checks())[:2]:
                ledger.add(await fn(check_id=cid, cancel=cancel))
            return True
        except Exception:
            return False
    return False


async def _frame(question, body_md, cov_line, budget, model) -> str:
    from backend.llm.client import LLMUnavailable, generate_async
    msgs = [{"role": "system", "content": FRAME_SYSTEM},
            {"role": "user", "content":
             f"Вопрос: {question}\n\nТЕЛО ОТВЕТА:\n{body_md[:6000]}\n\n"
             f"ПОЛНОТА: {cov_line}"}]
    try:
        raw = await generate_async(msgs, model=model, profile="frame")
        budget.charge(1, "frame")
    except LLMUnavailable:
        return ""
    head = re.sub(r"^\s*ШАПКА:\s*", "", raw.split("ВЫВОД:")[0]).strip()
    tail = raw.split("ВЫВОД:", 1)[1].strip() if "ВЫВОД:" in raw else ""
    return (f"{head}\n\n{tail}" if tail else head).strip()


async def _prose(question, ledger, cov_line, budget, model) -> str:
    """Модель ЧИТАЕТ фрагменты и отвечает на вопрос.

    Раньше сюда шёл тот же промпт, что и для таблицы: «напиши шапку и вывод к
    готовому телу». Телом были сырые куски акта, и на «какие кейсы» аудитор
    получал оглавление документа с припиской.
    """
    from backend.llm.client import LLMUnavailable, generate_async
    msgs = [{"role": "system", "content": PROSE_SYSTEM},
            {"role": "user", "content":
             f"ВОПРОС: {question}\n\nФРАГМЕНТЫ АКТОВ:\n"
             f"{ledger.to_context(budget_chars=14000)}\n\n"
             f"ПОЛНОТА ПОИСКА: {cov_line}"}]
    try:
        raw = await generate_async(msgs, model=model, profile="frame")
        budget.charge(1, "prose")
        return (raw or "").strip()
    except LLMUnavailable:
        return ""


def _template_frame(c, ledger, cov_line) -> str:
    n = len(ledger.checks())
    if not len(ledger):
        return "По этому вопросу в корпусе ничего не нашлось."
    return (f"Найдено в {n} проверк{'е' if n == 1 else 'ах'}."
            if n else "Найденное ниже.")


def _plan_human(plan) -> str:
    from backend.core.tools import registry
    names = []
    for s in plan[:3]:
        try:
            names.append(registry.get(s.get("tool"))[0].human_label.lower())
        except KeyError:
            pass
    return "Ищу: " + ", ".join(names) if names else "Ищу…"


def _memory_digest(session_id: int, focus) -> str:
    from backend.core.memory import state as memory
    st = memory.load(session_id)
    bits = []
    if st.check_id or focus:
        bits.append(f"В фокусе диалога проверка {st.check_id or focus} "
                    f"({st.human_basis() or 'из вопроса'}).")
    if st.topic:
        bits.append(f"Тема: {st.topic}.")
    return ("ПАМЯТЬ ДИАЛОГА: " + " ".join(bits) + "\n") if bits else ""


def _index_health() -> Dict:
    try:
        from backend.storage.writer import integrity
        rep = integrity()
        return rep.as_dict() if rep else {"ok": True}
    except Exception:
        return {"ok": True}

