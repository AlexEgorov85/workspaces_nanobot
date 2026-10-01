"""Операция ``log_events``: батчевая запись событий журнала.

Служебная, ``runtime-only``: пишет в журнал поток оборота, а не модельный
запрос. Значит, и ``permission`` своя — ``data:log_events``.

Зачем батч рядом с ``log_event``: агент копит события локально (пункт 7.2
плана ``enterprise-mcp-platform``) и отправляет их одним вызовом. Без этой
операции каждый чих оборота платил бы отдельный круговой оборот по stdio,
а ход с десятком tool-вызовов — десяток оборотов.

Вход — список объектов. Валидация per-event и fail-fast: негодное событие
означает ошибку на стороне агента, и молча выбросить его значит похоронить
дефект. Счётчики ``accepted``/``dropped`` в ответе: переполнение буфера
должно быть видно вызывающему, а не теряться между процессами.
"""

from __future__ import annotations

import json
from typing import Any

from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.registry import ToolDefinition

#: Схема входа задана руками, а не ``build_input_schema``: та выводит из
#: подписи ``{"type": "array"}`` без описания элемента, а контракт здесь и
#: есть в элементе. Операция ``runtime-only``, так что схему модель не видит —
#: она нужна как исполняемый контракт для агента и как документация.
_EVENT_ITEM = {
    "type": "object",
    "properties": {
        "id": {
            "type": ["string", "null"],
            "description": "Ключ события (UUID). Не задан — сгенерирует сервер.",
        },
        "event_type": {"type": "string", "description": "Тип события, обязателен."},
        "name": {"type": "string", "description": "Имя producer'а."},
        "level": {
            "type": "string",
            "description": (
                "Уровень: DEBUG, INFO, WARN или ERROR (WARNING приходит как "
                "WARN). Пусто — INFO. Неизвестное значение — отказ, а не тихая "
                "замена: опечатка, съеденная молча, выглядит в журнале как "
                "событие иной важности."
            ),
        },
        "summary": {"type": "string", "description": "Краткое описание одной строкой."},
        "payload": {"type": "object", "description": "Данные события."},
        "session_id": {"type": ["string", "null"]},
        "user_id": {"type": ["string", "null"]},
    },
    "required": ["event_type"],
}

INPUT_SCHEMA = {
    "type": "object",
    "properties": {"events": {"type": "array", "items": _EVENT_ITEM}},
    "required": ["events"],
}


def create_tool(container: ToolContainer) -> ToolDefinition:
    def log_events(events: list[dict[str, Any]]) -> str:
        counters = container.get("data").log_events(events, audience="runtime")
        return json.dumps({"status": "ok", **counters}, ensure_ascii=False)

    description = (
        "Записать пачку событий в долговечный журнал gateway одним вызовом. "
        "Служебная операция: вызывается агентом при пакетном сбросе буфера, "
        "не моделью. Неблокирующая: события встают в буфер, когда писать в базу "
        "— решает буфер. Возвращает счётчики accepted и dropped: переполнение "
        "видно вызывающему."
    )

    return ToolDefinition(
        name="log_events",
        description=description,
        handler=log_events,
        category="data",
        tags=("logging", "infrastructure", "runtime-only"),
        permissions=("data:log_events",),
        input_schema=INPUT_SCHEMA,
    )
