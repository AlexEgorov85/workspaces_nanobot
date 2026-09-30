"""Точки входа навыка: строки на входе, словари на выходе.

Транспорта здесь нет намеренно. MCP-сервер, CLI или HTTP ложатся сверху и
только перекладывают аргументы — вся защита остаётся внутри.

Главное решение модуля: **ход исполняется тем же кодом, что и в собственном
интерфейсе**. `_stream_agent_response` — единственный конвейер ответа, а
навык лишь собирает его события в плоский результат. Соблазн написать
«облегчённый ход для внешнего агента» велик, но кончается он одинаково: два
пути расходятся, чаще правят один, и внешний аудитор незаметно начинает
получать ответы по правилам полугодовой давности.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from backend.skill import render

logger = logging.getLogger(__name__)

# Потолок текста ответа. У внешнего агента результат инструмента режется по
# `maxToolResultChars` (16 000 у коллег), а всё, что длиннее
# `persist_threshold`, уезжает файлом и в контекст приходит ссылкой. Режем
# сами и говорим об этом вслух — молча обрезанный ответ выглядит как
# законченный.
#
# 12 000, а не половина от их потолка: карточка контроля исполнения — шесть
# блоков, и на пяти поручениях с цитатами она подходит к восьми тысячам
# вплотную. Обрезанная посередине плана карточка хуже длинной. Остаток до
# 16 000 уходит на прочие поля конверта (источники — около полутора тысяч).
_ANSWER_MAX_CHARS = 12000
_SOURCES_MAX = 12


# ──────────────────────────────────────────────────────────────────
# Сбор событий хода
# ──────────────────────────────────────────────────────────────────

def _parse_sse(chunk: str) -> Optional[Dict[str, Any]]:
    """Разобрать один SSE-кадр в `{"event": ..., "data": {...}}`."""
    event, data = None, None
    for line in chunk.splitlines():
        if line.startswith("event: "):
            event = line[7:].strip()
        elif line.startswith("data: "):
            try:
                data = json.loads(line[6:])
            except (ValueError, TypeError):
                data = {}
    if event is None:
        return None
    return {"event": event, "data": data or {}}


def _clip(text: str) -> tuple:
    if len(text) <= _ANSWER_MAX_CHARS:
        return text, False
    cut = text[:_ANSWER_MAX_CHARS].rsplit("\n", 1)[0]
    return cut + "\n\n_(ответ показан не полностью)_", True


async def _run_turn_collect(
    message: str,
    session_id: int,
    *,
    attachment_ids: Optional[List[str]] = None,
    model: Optional[str] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Прогнать ход и собрать результат в контракт навыка."""
    # Импорт внутри функции: модуль маршрутов тянет FastAPI, а навык должен
    # импортироваться и там, где сервера нет (CLI, тесты).
    from backend.api.routes.chat import _stream_agent_response

    text_parts: List[str] = []
    answer_text = ""
    out: Dict[str, Any] = {
        "ok": True, "answer_md": "", "sources": [], "coverage": "",
        "checks": [], "artifact": None, "insufficient": False, "reason": "",
        "status": "ok", "clarify": None,
    }

    async for chunk in _stream_agent_response(
            message, session_id, model, attachment_ids, surface="chat"):
        ev = _parse_sse(chunk)
        if ev is None:
            continue
        name, data = ev["event"], ev["data"]

        if name == "status" and progress:
            progress(str(data.get("text") or data.get("step") or ""))
        elif name == "answer":
            answer_text = data.get("text") or ""
            out["status"] = data.get("status") or "ok"
        elif name == "token":
            text_parts.append(data.get("text") or "")
        elif name == "sources":
            out["sources"] = (data.get("sources") or [])[:_SOURCES_MAX]
        elif name == "card":
            out.setdefault("card", {}).update(data)
            out["card"].setdefault("payloads", {})
        elif name == "card_update":
            # Блоки надо копить по имени, а не сливать в один словарь:
            # прежний `.update(data)` затирал payload предыдущего блока
            # следующим, и к концу карточки от неё оставался последний.
            card = out.setdefault("card", {})
            card.setdefault("payloads", {})
            block = data.get("block")
            if block and data.get("payload") is not None:
                card["payloads"][block] = data["payload"]
            if progress:
                progress(str(block or "собираю карточку"))
        elif name == "card_done":
            out.setdefault("card", {}).update(
                {k: v for k, v in data.items() if k != "payload"})
        elif name == "error":
            out["ok"] = False
            out["reason"] = data.get("message") or "ход не завершился"
        elif name == "done":
            out["checks"] = data.get("km_numbers") or []
            out["status"] = data.get("status") or out["status"]
            if data.get("needs_clarification"):
                out["clarify"] = data.get("clarify") or data.get("candidates")
            if data.get("budget"):
                out["budget"] = data["budget"]

    body = answer_text or "".join(text_parts)
    if not body.strip() and out.get("card"):
        # Карточка — ход без события `answer`: блоки приходят структурой и
        # рисуются своим интерфейсом. В общем чате рисовать нечем, поэтому
        # берём ту же свёртку, что уходит в историю диалога. Второй рендер
        # писать нельзя: он разойдётся с первым, и в чат поедет карточка
        # позапрошлой версии.
        body = _card_markdown(out["card"])
    out["answer_md"], clipped = _clip(body.strip())
    if clipped:
        out["reason"] = ("ответ длиннее потолка — показана верхняя часть; "
                         "уточните вопрос, чтобы получить нужную")
    if not out["answer_md"] and out["ok"]:
        out["ok"] = False
        out["reason"] = out["reason"] or "ход завершился без текста ответа"
    out["insufficient"] = out["status"] in ("degraded", "refused")
    return out


def _card_markdown(card: Dict[str, Any]) -> str:
    """Свёртка карточки в текст. Отказ рендера не должен ронять ход."""
    try:
        from backend.agents.execution_control import card_to_markdown
        return card_to_markdown(card)
    except Exception as e:                                  # noqa: BLE001
        logger.exception("[skill] свёртка карточки не собралась")
        km = card.get("km_id") or "?"
        return (f"## Контроль исполнения — {km}\n\n"
                f"Карточка собрана, но её текстовое представление не "
                f"построилось ({type(e).__name__}). Блоки: "
                f"{', '.join(sorted((card.get('payloads') or {}).keys())) or '—'}.")


def _session(session_key: str, user_id: Optional[str]) -> int:
    from backend.storage.database import ExternalSessionRepo, get_db
    with get_db() as db:
        return ExternalSessionRepo.resolve(db, session_key, user_id,
                                           source="nanobot")


def _need(value: Optional[str], name: str) -> Optional[Dict[str, Any]]:
    if not (value or "").strip():
        return {"ok": False, "reason": f"не передан обязательный аргумент "
                                       f"«{name}»"}
    return None


# ──────────────────────────────────────────────────────────────────
# Инструменты
# ──────────────────────────────────────────────────────────────────

async def ask(question: str, session_key: str,
              user_id: Optional[str] = None,
              model: Optional[str] = None,
              progress: Optional[Callable[[str], None]] = None) -> Dict:
    """Вопрос по корпусу актов и проверок.

    `session_key` обязателен: им ключуется память диалога. Без него каждый
    вопрос был бы первым, и уточнение «о какой КМ речь» никогда бы не
    доигрывалось до ответа.
    """
    for bad in (_need(question, "question"), _need(session_key, "session_key")):
        if bad:
            return bad
    sid = _session(session_key, user_id)
    res = await _run_turn_collect(question, sid, model=model, progress=progress)
    res["session_key"] = session_key
    return render.envelope(res)


async def hypotheses(topic: str, session_key: str,
                     user_id: Optional[str] = None,
                     progress: Optional[Callable[[str], None]] = None) -> Dict:
    """Гипотезы на старте новой проверки по теме.

    Отдельный инструмент, а не подсказка в `ask`: внешней модели нужен повод
    выбрать нас, а формулировку вопроса она бы придумала свою — и каждый раз
    другую. Шаблон тот же, что на приветственном экране интерфейса.
    """
    for bad in (_need(topic, "topic"), _need(session_key, "session_key")):
        if bad:
            return bad
    q = (f"Я начинаю новую проверку по теме «{topic}». "
         f"Какие гипотезы и что стоит проверить?")
    sid = _session(session_key, user_id)
    res = await _run_turn_collect(q, sid, progress=progress)
    res["session_key"] = session_key
    return render.envelope(res)


async def deviations(check_id: Optional[str] = None,
                     category: Optional[str] = None,
                     severity: Optional[str] = None,
                     min_rub: Optional[float] = None,
                     system: Optional[str] = None,
                     unit: Optional[str] = None,
                     limit: int = 50) -> Dict:
    """Реестр отклонений по полям. Без модели — значит без очереди к ней.

    Заполненность полей возвращается рядом с данными: «нарушений дороже
    десяти миллионов — три» при заполненности суммы в девять процентов это
    не ответ, а ловушка.
    """
    from backend.core.tools import facts

    r = await facts.query_deviations(
        check_id=check_id, category=category, severity=severity,
        min_rub=min_rub, system=system, unit=unit, limit=limit)
    rows = [{"check_id": e.check_id, "quote": e.quote, **e.fields}
            for e in r.evidence]
    # Таблицу собираем сами. Отдать модели голые строки — значит отдать ей
    # и переписывание сумм: ровно тот случай, ради которого тело ответа в
    # своём интерфейсе рендерит код (`backend/core/render.py`).
    out = render.envelope(
        {"ok": r.ok, "answer_md": render.deviations_table(rows),
         "coverage": r.coverage.line(),
         "checks": sorted({e.check_id for e in r.evidence}),
         "reason": r.error or ""},
        # Сырые строки НЕ отдаём. Таблица уже нарисована; отправить рядом
        # тот же массив словарями значит удвоить токены в чужом контексте и
        # пригласить модель нарисовать свою таблицу поверх нашей — ровно то,
        # ради чего рендер и забрали у неё.
        extra={"field_fill": r.coverage.field_fill} if r.coverage.field_fill
        else None,
    )
    return out


async def status() -> Dict:
    """Что есть в корпусе и в каком режиме работает база.

    Нужен внешнему агенту, чтобы отличать «не нашёл» от «этого тут вообще
    нет»: без такой опоры модель охотно объявляет пустой корпус ответом.
    """
    from backend.core.tools import facts

    r = await facts.corpus_profile()
    data = dict(r.evidence[0].fields) if r.evidence else {}
    try:
        from backend.storage import gp
        data["greenplum"] = (gp.access_mode() if gp.gp_enabled()
                             else "выключен")
    except Exception as e:                                  # noqa: BLE001
        data["greenplum"] = f"недоступен ({e})"
    return {"ok": r.ok, "corpus": data, "reason": r.error or ""}


async def card_start(session_key: str,
                     letter_path: Optional[str] = None,
                     letter_text: Optional[str] = None,
                     user_id: Optional[str] = None) -> Dict:
    """Карточка контроля исполнения по ответу профильника.

    Возвращает идентификатор задачи сразу. Карточка строится минутами, а у
    внешнего агента на неё один воркер: дождавшись нас синхронно, он
    остановил бы очередь всего подразделения.
    """
    bad = _need(session_key, "session_key")
    if bad:
        return bad
    if not (letter_path or letter_text):
        return {"ok": False,
                "reason": "передайте письмо профильника: letter_path (.docx, "
                          ".txt, .pdf) или letter_text"}

    from backend.api.routes.files import extract_text_from_path, put_attachment
    from backend.skill import jobs

    if letter_text:
        text, name = letter_text, "letter.txt"
    else:
        p = Path(letter_path).expanduser()
        try:
            text = extract_text_from_path(p)
        except Exception as e:                              # noqa: BLE001
            return {"ok": False, "reason": str(e)}
        name = p.name
    try:
        aid = put_attachment(text, name)
    except ValueError as e:
        return {"ok": False, "reason": str(e)}

    sid = _session(session_key, user_id)

    async def _work(progress: Callable[[str], None]) -> Dict:
        progress("разбираю письмо")
        res = await _run_turn_collect(
            "Проверь исполнение поручений по этому ответу профильника",
            sid, attachment_ids=[aid], progress=progress)
        res["session_key"] = session_key
        return res

    return await jobs.start(_work, label=f"карточка: {name}")


async def card_status(job_id: str,
                      wait_sec: Optional[float] = None) -> Dict:
    """Как продвигается карточка и её результат, когда готова.

    Вызов САМ ждёт готовности до `wait_sec` секунд (по умолчанию 20). Пауза
    между опросами должна быть у кого-то, и у агента нанобота её нет:
    получив «не готово», он зовёт снова немедленно и за секунды сжигает
    лимит итераций вместе с квотой модели.

    Пока идёт — стадия. Готова — тот же конверт, что у остальных
    инструментов: карточка попадает в общий чат текстом, отдельного окна
    для неё нет.
    """
    from backend.skill import jobs
    bad = _need(job_id, "job_id")
    if bad:
        return bad
    st = await jobs.wait(job_id, wait_sec)
    if not st.get("done"):
        return st
    result = st.get("result") or {}
    if not isinstance(result, dict) or not result.get("answer_md"):
        # Карточка не собралась: тот же отказ, что у остальных инструментов,
        # а не «готово» без текста.
        reason = (st.get("error")
                  or (result.get("reason") if isinstance(result, dict) else None)
                  or "карточка не собралась")
        return {"ok": False, "done": True, "job_id": job_id, "reason": reason}
    env = render.envelope(result)
    env["job_id"] = job_id
    env["done"] = True
    return env


def forget(session_key: str) -> Dict:
    """Забыть диалог: следующий вопрос начнётся с чистого листа.

    Управление контекстом принадлежит аудитору так же, как выбор проверки:
    подставленный номер должен быть отменяемым.
    """
    from backend.storage.database import ExternalSessionRepo, get_db
    bad = _need(session_key, "session_key")
    if bad:
        return bad
    with get_db() as db:
        found = ExternalSessionRepo.forget(db, session_key)
    return {"ok": True, "forgotten": found, "session_key": session_key}
