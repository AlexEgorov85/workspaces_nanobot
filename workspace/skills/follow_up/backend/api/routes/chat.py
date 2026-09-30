"""
Follow Up 2.0 — Chat API Route.

POST /api/chat           — основной чат (streaming SSE)
GET  /api/chat/sessions  — список сессий (через history route)
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import TYPE_CHECKING, AsyncGenerator, Optional

if TYPE_CHECKING:                       # аннотация, а не импорт на старте:
    from backend.core.pools import CancelToken   # pools тянет torch-соседей

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend.agents.router import get_agent
from backend.rag.query_understanding import understand_query
from backend.storage.database import MessageRepo, SessionRepo, get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])

# Последние завершённые ходы: фронт после обрыва WS спрашивает, чем кончилось.
# В памяти процесса — этого достаточно: дочитка нужна в пределах минут.
_TURNS: dict = {}


def _gs_cfg():
    from backend.config import get_settings
    return get_settings()


# ──────────────────────────────────────────────────────────────────
# Schemas
# ──────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    session_id: Optional[int] = None
    model: Optional[str] = None         # Если None — автоопределение
    attachment_ids: list[str] = []      # вложения из POST /api/files/upload


class NewSessionRequest(BaseModel):
    title: Optional[str] = None


# ──────────────────────────────────────────────────────────────────
# SSE helpers
# ──────────────────────────────────────────────────────────────────

def _sse_event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _save_user_turn(session_id: int, message: str, has_attachment: bool) -> None:
    """Пишет вопрос аудитора до начала работы.

    Отдельная функция, а не строчка в генераторе: запись не должна ронять ход.
    Если история недоступна, ответить всё равно лучше, чем упасть.
    """
    try:
        content = message
        if has_attachment:
            content += "\n\n[Вложение: ответ профильного подразделения]"
        with get_db() as db:
            MessageRepo.add(db, session_id=session_id, role="user",
                            content=content)
    except Exception as e:
        logger.warning(f"[Chat] Вопрос не сохранён в историю: {e}")


def _save_core_answer(session_id: int, core, question: str) -> None:
    """Ответ ядра в историю. Не должен ронять ход: без записи ответ уже показан."""
    try:
        with get_db() as db:
            MessageRepo.add(db, session_id=session_id, role="assistant",
                            content=core.text, agent_type="core",
                            contexts=core.sources, followups=[])
            session = SessionRepo.get(db, session_id)
            if session and not session.title:
                SessionRepo.update_title(
                    db, session_id,
                    question[:60] + ("..." if len(question) > 60 else ""))
    except Exception as e:
        logger.warning(f"[Chat] Ответ ядра не сохранён: {e}")


def _load_history(session_id: int, limit_messages: int = 12) -> list[dict]:
    """Возвращает последние N сообщений сессии в формате [{role, content}]."""
    with get_db() as db:
        msgs = MessageRepo.get_session_messages(db, session_id)
        tail = msgs[-limit_messages:] if msgs else []
        return [{"role": m.role, "content": m.content} for m in tail]


async def _stream_agent_response(
    message: str,
    session_id: int,
    model: Optional[str] = None,
    attachment_ids: Optional[list[str]] = None,
    cancel: Optional["CancelToken"] = None,
    surface: str = "web",
) -> AsyncGenerator[str, None]:
    """
    Генератор SSE событий для стриминга ответа агента.

    События:
      - status        : {"step": "...", "text": "..."}
      - token         : {"text": "..."}
      - sources       : {"sources": [...]}
      - followups     : {"items": ["...", "...", "..."]}
      - card / card_update / card_done : карточка контроля исполнения
      - done          : {"agent_type": "...", ...}
      - error         : {"message": "..."}

    ``surface`` — куда поедет текст: "web" (свой интерфейс) или "chat"
    (общий чат AW через шину нанобота, где санитайзер вырезает `<details>`).
    Влияет только на оформление; содержание ответа одно и то же.
    """
    from backend.core import activity, llm_gateway as gw
    from backend.core.pools import CancelToken, Cancelled
    from backend.llm.client import LLMUnavailable

    cancel = cancel or CancelToken()
    turn_id = uuid.uuid4().hex[:12]
    t_turn = time.monotonic()
    deadline_at = t_turn + _gs_cfg().turn_deadline_sec

    async def _on_wait(eta: float, depth: int) -> None:
        # Пока ход стоит в очереди к модели, аудитор видит не тишину, а
        # честную оценку. `depth × 9` тут врал бы: блок карточки держит слот
        # десятками секунд.
        await progress_q.put({"step": "queued",
                              "text": f"Жду очереди к модели: примерно "
                                      f"{eta:.0f} с, в очереди {depth}"})

    progress_q: asyncio.Queue = asyncio.Queue()
    ctx_token = gw.set_context(gw.CallContext(
        fairness_key=f"session:{session_id}", profile="skill",
        deadline_at=deadline_at, cancel=cancel, on_wait=_on_wait,
        stage="turn"))

    yield _sse_event("turn", {"turn_id": turn_id,
                              "deadline_sec": _gs_cfg().turn_deadline_sec})
    # Активный ход: фоновая гидратация уступает CPU, пока он идёт. Раньше это
    # знала только карточка (_last_card_activity), а обычный вопрос шёл по
    # тому же единственному процессору и ни на что не влиял.
    _act = activity.turn("chat")
    _act.__enter__()
    try:
        # 0. История диалога (для резолвинга местоимений и контекста промпта).
        #    Читается ДО записи текущего хода, иначе вопрос попадёт сам себе
        #    в контекст.
        history = _load_history(session_id)

        # 0-бис. Ход сохраняется СРАЗУ при приёме, а не после успешной
        # генерации. Раньше запись шла шагом 7: любое падение — таймаут LLM,
        # обрыв WS, 503 — и вопрос аудитора исчезал бесследно, вместе с
        # возможностью понять, на чём именно сломалось.
        _save_user_turn(session_id, message, bool(attachment_ids))

        # 0a. Вложения (ответ профильника)
        attachment_text = None
        if attachment_ids:
            from backend.api.routes.files import get_attachment
            parts = []
            for aid in attachment_ids:
                att = get_attachment(aid)
                if att:
                    parts.append(att["text"])
                else:
                    yield _sse_event("status", {
                        "step": "attachment_expired",
                        "text": "Вложение устарело — загрузите файл заново."})
            attachment_text = "\n\n---\n\n".join(parts) if parts else None

        # 1. Понимание запроса
        yield _sse_event("status", {"step": "understanding", "text": "Анализирую запрос…"})
        await asyncio.sleep(0)

        # 1-бис. РАЗМОРОЗКА: если это ответ на уточнение, отвечаем на ИСХОДНЫЙ
        # вопрос, а не на строку «КМ-99-12345». Ноль вызовов модели: справочник
        # проверок валидирует номер локально.
        from backend.core import slots
        from backend.core.memory import bridge, state as memory
        mem = memory.load(session_id)
        resumed = None
        effective_message = message
        if mem.open_question and memory.question_is_live(session_id,
                                                         mem.open_question):
            filled = slots.try_fill(message, mem.open_question)
            if filled and filled.slot == "check_id" and filled.value:
                resumed = filled
                effective_message = filled.original_query or message
                memory.clear_question(session_id)
                memory.set_focus(session_id, filled.value,
                                 memory.BASIS_CONFIRMED, turn_id)
                yield _sse_event("status", {
                    "step": "resumed",
                    "text": f"Продолжаю ваш вопрос: «{effective_message[:80]}» "
                            f"по {filled.value}"})
                yield _sse_event("resumed", {
                    "original_query": effective_message,
                    "check_id": filled.value, "how": filled.how})
            elif filled and filled.slot == "reject":
                memory.clear_question(session_id)

        query_ctx = understand_query(
            effective_message, history=history,
            has_attachment=bool(attachment_text),
            attachment_text=attachment_text)
        if resumed:
            query_ctx.km_numbers = [resumed.value]
            query_ctx.resolved_from_history = True

        # 1-тер. ФОКУС: подстановка проверки из памяти ВЫШЕ интента. Внутри
        # агента было бы поздно — до него фраза доедет уже без номера.
        inferred = None
        if not resumed:
            inferred = bridge.fill_focus(query_ctx, session_id)
        focus_now = (query_ctx.km_numbers or [None])[0]
        yield _sse_event("context", {
            "check_id": focus_now,
            "basis": (memory.BASIS_CONFIRMED if resumed else
                      memory.BASIS_INFERRED if inferred else
                      memory.BASIS_USER if focus_now else None),
            "basis_human": (memory.BASIS_RU.get(
                memory.BASIS_CONFIRMED if resumed else
                memory.BASIS_INFERRED if inferred else
                memory.BASIS_USER) if focus_now else None),
            "corpus_wide": bridge.is_corpus_wide(effective_message)})
        _INTENT_RU = {
            "FOLLOWUP": "поиск по прошлым проверкам",
            "HYPOTHESIS": "гипотезы для проверки",
            "RECHECK": "анализ репроверки",
            "ANALYTICS": "аналитика",
            "KM_DETAIL": "детали проверки",
            "EXECUTION_CONTROL": "контроль исполнения",
            "REPORT": "отчёт", "GENERAL": "общий вопрос",
        }
        yield _sse_event("status", {
            "step": "classified",
            "text": f"Тип запроса: {_INTENT_RU.get(query_ctx.intent, query_ctx.intent)}"
                    + (" (контекст из истории)" if query_ctx.resolved_from_history else ""),
            "intent": query_ctx.intent,
            "km_numbers": query_ctx.km_numbers,
            "topic": query_ctx.topic or "",
            "resolved_from_history": query_ctx.resolved_from_history,
        })

        # 1г. ТУПИК KM_DETAIL. Прогон регексов показывает: «какие системы
        # упоминались по теме кредитных карт» матчится _KM_DETAIL_KEYWORDS
        # раньше FOLLOWUP, номера в запросе нет, фокуса нет — и агент отвечает
        # «Уточните, по какой КМ» за НОЛЬ вызовов модели. Это гарантированный
        # тупик, а не экономия. Переводим такой ход в поиск по теме: платим
        # один вызов там, где раньше платили потерянным ходом аудитора.
        if (query_ctx.intent == "KM_DETAIL" and not query_ctx.km_numbers
                and not bridge.is_corpus_wide(effective_message)):
            from backend.rag.query_understanding import Intent as _I
            query_ctx.intent = _I.FOLLOWUP
            logger.info("[Chat] KM_DETAIL без номера и фокуса → FOLLOWUP "
                        "(тупик «уточните, по какой КМ» снят)")
            yield _sse_event("status", {
                "step": "reframed",
                "text": "Номер проверки не назван — ищу по теме во всех актах"})

        # 1a. Контроль исполнения поручений — отдельный конвейер с карточкой
        from backend.config import get_settings as _gs
        if query_ctx.intent == "EXECUTION_CONTROL" and _gs().exec_control_enabled:
            from backend.agents.execution_control import stream_execution_control
            async for ev in stream_execution_control(query_ctx, session_id, model):
                yield ev
            return

        # 2. НОВОЕ ЯДРО: понимание → контракт → инструменты → критик → ответ.
        # Включено только для интентов, которые сегодня и так деградируют:
        # KM_DETAIL падал с TypeError, остальные три отвечают пятью чанками.
        # Регрессия ограничена по построению — хуже сломанного не станет.
        _cfg = _gs_cfg()
        _core_on = (_cfg.core_enabled and query_ctx.intent in
                    {i.strip() for i in _cfg.core_enabled_intents.split(",")})
        if _core_on:
            from backend.core import orchestrator

            async def _emit(step, text, structured=False):
                if structured:
                    await progress_q.put({"__event__": step, "data": text})
                else:
                    await progress_q.put({"step": step, "text": text})

            core_task = asyncio.ensure_future(orchestrator.run_turn(
                effective_message, session_id, model=model, cancel=cancel,
                emit=_emit, focus=(query_ctx.km_numbers or [None])[0],
                surface=surface))
            while not core_task.done():
                if time.monotonic() > deadline_at or cancel.cancelled:
                    core_task.cancel()
                    break
                try:
                    item = await asyncio.wait_for(progress_q.get(), timeout=2.0)
                    if "__event__" in item:
                        yield _sse_event(item["__event__"], item["data"])
                    else:
                        yield _sse_event("status", item)
                except asyncio.TimeoutError:
                    yield _sse_event("tick", {
                        "elapsed": round(time.monotonic() - t_turn, 1),
                        "eta_queue": gw.eta_sec()})
            while not progress_q.empty():
                it = progress_q.get_nowait()
                yield _sse_event(it.get("__event__", "status"),
                                 it.get("data", it))
            try:
                core = core_task.result()
            except (asyncio.CancelledError, Exception) as e:
                logger.warning(f"[Chat] Ядро не отработало ({e}) — "
                               f"откат на прежний путь")
                core = None
            if core is not None:
                yield _sse_event("sources", {"sources": core.sources})
                yield _sse_event("answer", {"text": core.text,
                                            "status": core.status})
                _save_core_answer(session_id, core, effective_message)
                try:
                    if core.checks and len(core.checks) == 1:
                        memory.set_focus(session_id, core.checks[0],
                                         memory.BASIS_CONFIRMED if resumed
                                         else memory.BASIS_USER, turn_id)
                except Exception:
                    pass
                yield _sse_event("done", {
                    "agent_type": "core", "intent": query_ctx.intent,
                    "km_numbers": core.checks, "needs_clarification": False,
                    "followups": [], "turn_id": turn_id,
                    "status": core.status, "verdicts": core.verdicts,
                    "budget": core.budget})
                return

        # 2-бис. Прежний путь
        agent = get_agent(query_ctx)

        # 3. Поиск
        await asyncio.sleep(0)

        # Агент рассказывает, чем занят: GigaChat не стримит токены, поэтому
        # единственный способ показать работу — этапы. Колбэк складывает
        # события в очередь, а мы отдаём их в SSE по мере поступления.
        # Плюс тик раз в 2 с: держит соединение живым (прокси рвёт «тихое»)
        # и питает секундомер в интерфейсе.
        async def _on_progress(step: str, text: str) -> None:
            await progress_q.put({"step": step, "text": text})

        task = asyncio.ensure_future(
            agent.execute(query_ctx, model=model, history=history,
                          progress=_on_progress))
        t_start = time.monotonic()
        # Жёсткий дедлайн вместо `while not task.done()`. Прежний цикл крутился,
        # пока не отвалится клиент: llm_request_timeout=180 с двумя ретраями
        # давал до девяти минут на один зависший вызов.
        while not task.done():
            if time.monotonic() > deadline_at:
                cancel.cancel("превышен дедлайн хода")
                task.cancel()
                logger.warning(f"[Chat] Ход {turn_id}: дедлайн "
                               f"{_gs_cfg().turn_deadline_sec:.0f} с исчерпан")
                yield _sse_event("error", {
                    "message": f"Ответ не собрался за "
                               f"{_gs_cfg().turn_deadline_sec:.0f} с. "
                               f"Попробуйте сузить вопрос или повторить позже.",
                    "turn_id": turn_id, "reason": "deadline"})
                return
            if cancel.cancelled:
                task.cancel()
                yield _sse_event("done", {"agent_type": "aborted",
                                          "intent": query_ctx.intent,
                                          "km_numbers": query_ctx.km_numbers,
                                          "needs_clarification": False,
                                          "followups": [],
                                          "turn_id": turn_id,
                                          "status": "aborted"})
                return
            try:
                item = await asyncio.wait_for(progress_q.get(), timeout=2.0)
                yield _sse_event("status", item)
            except asyncio.TimeoutError:
                yield _sse_event("tick",
                                 {"elapsed": round(time.monotonic() - t_start, 1),
                                  "eta_queue": gw.eta_sec(),
                                  "queue_depth": gw.queue_depth()})
        while not progress_q.empty():
            yield _sse_event("status", progress_q.get_nowait())
        try:
            response = task.result()
        except asyncio.CancelledError:
            yield _sse_event("done", {"agent_type": "aborted",
                                      "intent": query_ctx.intent,
                                      "km_numbers": [], "followups": [],
                                      "needs_clarification": False,
                                      "turn_id": turn_id, "status": "aborted"})
            return
        except LLMUnavailable as e:
            # Типизированный исход шлюза, а не «Ошибка»: 503 после трёх попыток
            # и протухший токен требуют разного поведения и разных слов.
            logger.warning(f"[Chat] Ход {turn_id}: модель недоступна "
                           f"({e.outcome.kind})")
            yield _sse_event("status", {"step": "degraded",
                                        "text": e.outcome.human})
            yield _sse_event("token", {"text": e.outcome.human})
            yield _sse_event("done", {"agent_type": "degraded",
                                      "intent": query_ctx.intent,
                                      "km_numbers": query_ctx.km_numbers,
                                      "needs_clarification": False,
                                      "followups": [], "turn_id": turn_id,
                                      "status": "degraded",
                                      "outcome": e.outcome.kind})
            return

        # Если retrieval не нашёл источников — отдаём UI подсказку
        if response.needs_clarification:
            yield _sse_event("status", {
                "step": "clarification",
                "text": "Запрос неоднозначен — задаю уточняющий вопрос.",
            })
        elif not response.sources:
            yield _sse_event("status", {
                "step": "no_matches",
                "text": "Точных совпадений в базе не найдено — отвечу на основе доступных проверок.",
            })

        # 4. Отправляем источники
        if response.sources:
            yield _sse_event("sources", {"sources": response.sources})

        # 5. Стримим ответ по словам
        yield _sse_event("status", {"step": "generating", "text": "Формирую ответ…"})
        words = response.content.split(" ")
        chunk_size = 5
        for i in range(0, len(words), chunk_size):
            chunk = " ".join(words[i:i + chunk_size]) + " "
            yield _sse_event("token", {"text": chunk})
            await asyncio.sleep(0.01)

        # 6. Follow-up подсказки (чипы)
        if response.followups:
            yield _sse_event("followups", {"items": response.followups})

        # 7. Сохраняем ответ (вопрос уже записан шагом 0-бис)
        assistant_message_id = None
        with get_db() as db:
            msg_row = MessageRepo.add(
                db,
                session_id=session_id,
                role="assistant",
                content=response.content,
                agent_type=response.agent_type,
                contexts=response.sources,
                followups=response.followups or [],
            )
            db.flush()
            assistant_message_id = msg_row.id
            session = SessionRepo.get(db, session_id)
            if session and not session.title:
                title = message[:60] + ("..." if len(message) > 60 else "")
                SessionRepo.update_title(db, session_id, title)

        # 7-бис. Память: фокус после ответа и заморозка при уточнении.
        # Раньше «Уточните, по какой КМ» теряло исходную формулировку, и ответ
        # аудитора обрабатывался как новый запрос — это и есть жалоба №1.
        try:
            answered_km = (response.km_numbers or [None])[0]
            if response.needs_clarification:
                cands = [k for k in (response.km_numbers or []) if k]
                memory.freeze_question(
                    session_id,
                    question=response.content[:200],
                    original_query=effective_message,
                    awaiting="check_id", candidates=cands,
                    turn_id=turn_id, asked_message_id=assistant_message_id)
            elif answered_km:
                basis = (memory.BASIS_CONFIRMED if resumed else
                         memory.BASIS_INFERRED if inferred else
                         memory.BASIS_USER)
                memory.set_focus(session_id, answered_km, basis, turn_id)
            if response.sources:
                memory.remember_sources(
                    session_id,
                    [f"{s_.get('check_id')}:{s_.get('chunk_index')}"
                     for s_ in response.sources])
        except Exception as _e:
            logger.warning(f"[Chat] Память не обновлена: {_e}")

        # 8. Финальный event
        yield _sse_event("done", {
            "agent_type": response.agent_type,
            "intent": response.intent,
            "km_numbers": response.km_numbers,
            "needs_clarification": response.needs_clarification,
            "followups": response.followups,
            "turn_id": turn_id,
            "status": "ok",
        })

    except Exception as e:
        err_msg = str(e)
        if "429" in err_msg or "rate" in err_msg.lower():
            err_msg = "Превышен лимит запросов к LLM API. Подождите несколько секунд и повторите."
        elif "api_key" in err_msg.lower() or "authentication" in err_msg.lower():
            err_msg = "Ошибка аутентификации LLM. Проверьте API ключ в .env файле."
        elif "connection" in err_msg.lower() or "timeout" in err_msg.lower():
            err_msg = "Нет соединения с LLM сервером. Проверьте подключение к интернету."
        logger.exception(f"[Chat] Ошибка: {e}")
        yield _sse_event("error", {"message": err_msg, "turn_id": turn_id})
    finally:
        _act.__exit__(None, None, None)
        gw.reset_context(ctx_token)
        _TURNS[turn_id] = {"session_id": session_id, "turn_id": turn_id,
                           "elapsed_sec": round(time.monotonic() - t_turn, 1),
                           "finished_at": time.time()}
        while len(_TURNS) > 200:
            _TURNS.pop(next(iter(_TURNS)))


# ──────────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────────

@router.post("/sessions")
async def create_session(req: NewSessionRequest):
    """Создать новую сессию."""
    with get_db() as db:
        session = SessionRepo.create(db, title=req.title)
        return {"session_id": session.id, "title": session.title}


@router.get("/turn/{turn_id}")
async def get_turn(turn_id: str):
    """Чем кончился ход — для дочитки после обрыва соединения.

    Прокси рвёт «тихий» стрим, и фронт до сих пор считал обрыв успехом:
    `ws.onclose` → `finish(gotAny)`, где `gotAny` истинно после первого же
    события. Ответ на этот эндпоинт позволяет отличить «доехало» от «оборвалось».
    """
    turn = _TURNS.get(turn_id)
    if turn is None:
        return {"turn_id": turn_id, "status": "unknown"}
    return {**turn, "status": turn.get("status", "finished")}


@router.post("/stream")
async def chat_stream(req: ChatRequest):
    """
    Основной чат-эндпоинт с SSE стримингом.
    Если session_id не передан — создаёт новую сессию.
    """
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Сообщение не может быть пустым")

    session_id = req.session_id
    if session_id is None:
        with get_db() as db:
            session = SessionRepo.create(db)
            session_id = session.id

    return StreamingResponse(
        _stream_agent_response(req.message, session_id, req.model,
                               req.attachment_ids),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Session-Id": str(session_id),
        },
    )


@router.delete("/context/{session_id}")
async def clear_context(session_id: int):
    """Сброс фокуса диалога.

    Управление контекстом принадлежит аудитору ровно так же, как выбор
    проверки: подставленный номер должен быть не только видимым, но и
    отменяемым одним движением.
    """
    from backend.core.memory import state as memory
    memory.drop_focus(session_id)
    memory.clear_question(session_id)
    return {"ok": True, "session_id": session_id}
