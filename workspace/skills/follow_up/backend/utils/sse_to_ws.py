"""Follow Up 2.0 — переходник SSE → WebSocket.

Зачем: корпоративный прокси DataLab буферит ВСЕ HTTP-responses целиком
(WAF инспектирует body и ставит Content-Length). Из-за этого прогрессивная
сборка карточки в проде не видна — пользователь получает всё разом в конце.
Ни `X-Accel-Buffering: no`, ни `no-transform`, ни отключение gzip, ни
padding-комментарии не помогают: буферится даже `text/plain`.

Единственный transport, который проходит, — WebSocket (Jupyter сам работает
с ядрами через WS, поэтому прокси обязан его стримить).

Этот модуль позволяет ПЕРЕИСПОЛЬЗОВАТЬ существующие SSE-генераторы без
изменения бизнес-логики: генератор отдаёт строки в формате SSE, мы парсим
`event:`/`data:` и шлём каждое событие как WS-сообщение.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator, Optional

from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

HEARTBEAT_SEC = 10.0


def parse_sse_block(block: str) -> Optional[dict]:
    """Один SSE-блок → {"event": str, "data": ...} или None для пингов.

    Формат блока (см. _sse() в execution_control / _sse_event в chat):
        event: card_update
        data: {"card_id": "...", ...}
    Комментарии (': ping') игнорируем — в WS они не нужны, там свой
    heartbeat.
    """
    event_name = "message"
    data_parts: list[str] = []
    for line in block.split("\n"):
        if not line or line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_parts.append(line[5:].lstrip())
    if not data_parts:
        return None
    raw = "\n".join(data_parts)
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        payload = raw
    return {"event": event_name, "data": payload}


async def relay_sse_to_ws(
    ws: WebSocket,
    sse_iter: AsyncIterator[str],
    heartbeat_interval: float = HEARTBEAT_SEC,
) -> None:
    """Проксирует async-генератор SSE-строк в WebSocket.

    Каждое SSE-событие уходит как отдельный text-frame
    {"event": "...", "data": {...}} — фронтенд отдаёт его в тот же
    обработчик, что и HTTP-SSE.

    Heartbeat: каждые `heartbeat_interval` секунд шлём {"event": "ping"} —
    без этого WAF/прокси закрывает idle-соединение во время долгого
    LLM-вызова (у нас блоки карточки считаются десятками секунд).

    Обрыв клиента детектится heartbeat'ом и ОТМЕНЯЕТ генератор: иначе
    сборка карточки продолжала бы жечь LLM-вызовы «в никуда».
    """
    disconnect = asyncio.Event()

    async def _heartbeat() -> None:
        try:
            while not disconnect.is_set():
                await asyncio.sleep(heartbeat_interval)
                try:
                    await ws.send_text(json.dumps({"event": "ping", "data": {}}))
                except (WebSocketDisconnect, RuntimeError):
                    disconnect.set()
                    return
        except asyncio.CancelledError:
            return

    async def _consume() -> None:
        buffer = ""
        async for chunk in sse_iter:
            buffer += chunk
            while "\n\n" in buffer:
                block, buffer = buffer.split("\n\n", 1)
                msg = parse_sse_block(block)
                if msg is not None:
                    await ws.send_text(json.dumps(msg, ensure_ascii=False))
        if buffer.strip():
            msg = parse_sse_block(buffer)
            if msg is not None:
                await ws.send_text(json.dumps(msg, ensure_ascii=False))

    hb_task = asyncio.create_task(_heartbeat())
    consume_task = asyncio.create_task(_consume())
    waiter = asyncio.create_task(disconnect.wait())
    try:
        done, _ = await asyncio.wait({consume_task, waiter},
                                     return_when=asyncio.FIRST_COMPLETED)
        if consume_task in done:
            consume_task.result()          # пробрасываем ошибку генератора
        else:
            logger.info("[WS] Клиент отключился — отменяю генерацию")
            consume_task.cancel()
            try:
                await consume_task
            except (asyncio.CancelledError, Exception):
                pass
            raise WebSocketDisconnect()
    finally:
        for t in (hb_task, consume_task, waiter):
            if not t.done():
                t.cancel()
        for t in (hb_task, consume_task, waiter):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
