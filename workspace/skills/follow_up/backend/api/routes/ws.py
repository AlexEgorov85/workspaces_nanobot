"""Follow Up 2.0 — WebSocket-транспорт.

Прод-факт: корпоративный прокси DataLab буферит HTTP-ответы целиком, поэтому
SSE не стримит — прогрессивная сборка карточки не видна. WebSocket проходит.

Эндпоинты:
  WS  /api/ws/chat      — тот же конвейер, что POST /api/chat/stream
  WS  /api/ws/ping      — эхо-тик, для проверки транспорта
  GET /api/ws/selftest  — страница-стенд: сравнивает SSE и WS вживую
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from backend.storage.database import SessionRepo, get_db
from backend.utils.sse_to_ws import relay_sse_to_ws

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ws", tags=["ws"])


@router.websocket("/chat")
async def ws_chat(ws: WebSocket):
    """Чат по WebSocket. Первым сообщением — тот же JSON, что у
    POST /api/chat/stream: {message, session_id?, model?, attachment_ids?}.

    Ответ — поток сообщений {"event": "...", "data": {...}}, идентичных
    SSE-событиям: status / card / card_update / card_done / resolve_card /
    token / sources / followups / done / error.
    """
    await ws.accept()
    try:
        raw = await ws.receive_text()
    except WebSocketDisconnect:
        return
    try:
        req = json.loads(raw)
    except (ValueError, TypeError):
        await ws.send_text(json.dumps(
            {"event": "error", "data": {"message": "Некорректный JSON запроса"}}))
        await ws.close()
        return

    message = (req.get("message") or "").strip()
    if not message:
        await ws.send_text(json.dumps(
            {"event": "error", "data": {"message": "Сообщение не может быть пустым"}}))
        await ws.close()
        return

    session_id = req.get("session_id")
    if session_id is None:
        with get_db() as db:
            session_id = SessionRepo.create(db).id
    # Клиенту нужен session_id так же, как из заголовка X-Session-Id в SSE
    await ws.send_text(json.dumps(
        {"event": "session", "data": {"session_id": session_id}}))

    from backend.api.routes.chat import _stream_agent_response
    from backend.core.pools import CancelToken

    # Токен доходит до CPU-воркера: без него «Стоп» освобождал слот к модели,
    # а реранк продолжал жечь единственное ядро ещё десяток секунд — то есть
    # отмена превращалась в отказ обслуживания для следующего аудитора.
    cancel = CancelToken()
    gen = _stream_agent_response(message, session_id, req.get("model"),
                                 req.get("attachment_ids") or [], cancel=cancel)
    try:
        await relay_sse_to_ws(ws, gen)
    except WebSocketDisconnect:
        logger.info("[WS] Клиент отключился во время генерации")
        cancel.cancel("клиент отключился")
        return
    except Exception as e:
        cancel.cancel("ошибка конвейера")
        logger.exception(f"[WS] Ошибка конвейера: {e}")
        try:
            await ws.send_text(json.dumps(
                {"event": "error", "data": {"message": str(e)}}))
        except Exception:
            pass
    try:
        await ws.close()
    except Exception:
        pass


@router.websocket("/ping")
async def ws_ping(ws: WebSocket, ticks: int = 10, interval: float = 1.0):
    """Диагностика транспорта: N тиков с паузой. Если тики приходят по
    одному — WS стримит; если пачкой в конце — режет прокси."""
    await ws.accept()
    try:
        for i in range(1, int(ticks) + 1):
            await ws.send_text(json.dumps(
                {"event": "tick", "data": {"i": i, "of": int(ticks)}}))
            await asyncio.sleep(float(interval))
        await ws.send_text(json.dumps({"event": "done", "data": {}}))
    except WebSocketDisconnect:
        return
    except Exception as e:
        logger.warning(f"[WS] ping: {e}")
    try:
        await ws.close()
    except Exception:
        pass


_SELFTEST_HTML = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>Follow Up — проверка транспорта</title>
<style>
 body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;background:#FAFAF8;
      color:#0E0E0C;max-width:860px;margin:0 auto;padding:32px}
 h1{font-weight:600;font-size:24px} p{color:#3A3A36;line-height:1.55}
 button{font:inherit;font-size:13px;background:#B8651C;color:#fff;border:0;
        border-radius:999px;padding:8px 18px;cursor:pointer;margin-right:8px}
 button.sec{background:transparent;color:#8C4D14;border:1px solid rgba(14,14,12,.15)}
 pre{background:#fff;border:1px solid rgba(14,14,12,.10);border-radius:12px;
     padding:14px;font-size:12px;line-height:1.5;max-height:50vh;overflow:auto}
 .ok{color:#1B7F4B}.bad{color:#B3261E}.mut{color:#6F6F68}
</style></head><body>
<h1>Проверка транспорта</h1>
<p>Тики должны приходить <b>по одному раз в секунду</b>. Если появляются пачкой
в конце — транспорт буферится прокси и прогресс в интерфейсе не будет виден.</p>
<p><button data-t="ws">WebSocket</button>
   <button class="sec" data-t="sse">HTTP SSE</button>
   <button class="sec" data-t="clear">Очистить</button></p>
<pre id="log">Выберите транспорт…</pre>
<script>
const logEl=document.getElementById('log');let t0=0;
const base=location.pathname.replace(/\\/api\\/ws\\/selftest$/,'');
function log(cls,msg){const dt=((performance.now()/1000)-t0).toFixed(2);
 logEl.innerHTML+=`\\n<span class="${cls}">+${dt}s</span> ${msg}`;
 logEl.scrollTop=logEl.scrollHeight;}
function start(name){logEl.innerHTML=name;t0=performance.now()/1000;}
async function runWS(){start('WebSocket…');
 const proto=location.protocol==='https:'?'wss:':'ws:';
 const url=`${proto}//${location.host}${base}/api/ws/ping?ticks=10&interval=1`;
 log('mut',url);const ws=new WebSocket(url);
 ws.onopen=()=>log('ok','открыт');
 ws.onmessage=e=>{const m=JSON.parse(e.data);
   log(m.event==='done'?'ok':'', m.event==='done'?'готово':`тик ${m.data.i}/${m.data.of}`);};
 ws.onerror=()=>log('bad','ошибка WebSocket');
 ws.onclose=e=>log('mut',`закрыт (${e.code})`);}
async function runSSE(){start('HTTP SSE…');
 const r=await fetch(`${base}/api/ws/ping-sse?ticks=10&interval=1`);
 const rd=r.body.getReader();const dec=new TextDecoder();
 for(;;){const{done,value}=await rd.read();if(done)break;
  dec.decode(value,{stream:true}).split('\\n').filter(l=>l.startsWith('data:'))
    .forEach(l=>{const m=JSON.parse(l.slice(5));
      log(m.event==='done'?'ok':'',m.event==='done'?'готово':`тик ${m.data.i}/${m.data.of}`);});}}
document.querySelectorAll('button[data-t]').forEach(b=>b.onclick=()=>{
 const t=b.dataset.t;if(t==='ws')runWS();else if(t==='sse')runSSE();
 else logEl.innerHTML='Выберите транспорт…';});
</script></body></html>"""


@router.get("/selftest", response_class=HTMLResponse)
async def selftest_page():
    """Стенд: сравнить SSE и WS на текущем контуре."""
    return HTMLResponse(_SELFTEST_HTML)


@router.get("/ping-sse")
async def ping_sse(ticks: int = 10, interval: float = 1.0):
    """SSE-двойник /ws/ping — для сравнения на стенде."""
    from fastapi.responses import StreamingResponse

    async def gen():
        for i in range(1, int(ticks) + 1):
            yield ("data: " + json.dumps({"event": "tick",
                                          "data": {"i": i, "of": int(ticks)}})
                   + "\n\n")
            await asyncio.sleep(float(interval))
        yield "data: " + json.dumps({"event": "done", "data": {}}) + "\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform",
                                      "X-Accel-Buffering": "no"})
