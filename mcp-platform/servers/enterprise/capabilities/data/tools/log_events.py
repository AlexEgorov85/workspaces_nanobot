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

Идентичность оборота приходит из контекста вызова, а не из ``events``:
в схеме элемента этих полей нет вовсе. Иначе батч, присланный под видом
журналирования оборота, записал бы события в чужую сессию.
"""

from __future__ import annotations

import json
from typing import Any

from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.execution.context import ToolExecutionContext
from libs.enterprise_common.registry import ToolDefinition
from servers.enterprise.capabilities.data.service.main import (
    AUDIENCE_RUNTIME,
    DataService,
)

#: Схема входа задана руками, а не ``build_input_schema``: та выводит из
#: подписи ``{"type": "array"}`` без описания элемента, а контракт здесь и
#: есть в элементе. Операция ``runtime-only``, так что схему модель не видит —
#: она нужна как исполняемый контракт для агента и как документация.
#:
#: С 2026-10-04 это объявление **исполняется**: загрузчик больше не затирает его
#: выводом из подписи. Оно обязано оставаться истинным — иначе правка в нём
#: снова потеряется молча, ровно как потерялась предыдущая. Проверка, что
#: ``items`` и nullable-маркеры не отвергают то, что реально шлёт агент, — в
#: ``mcp-platform/tests/test_operation_schema_permissiveness.py``.
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
        "metadata": {
            "type": "object",
            "description": "Служебные признаки события (source, component).",
        },
    },
    "required": ["event_type"],
}

INPUT_SCHEMA = {
    "type": "object",
    "properties": {"events": {"type": "array", "items": _EVENT_ITEM}},
    "required": ["events"],
}


def create_tool(registry_container: ToolContainer) -> ToolDefinition:
    # Сервис замыкается обработчиком: глобальная привязка означала, что вызов
    # уходит в сервис той регистрации, которая отработала последней, а не той,
    # ради которой эта операция объявлена.
    service: DataService = registry_container.get("data")

    def handle_log_events(ctx: ToolExecutionContext, events: list[dict[str, Any]]) -> str:
        """Записать пачку событий журнала и вернуть счётчики приёма.

        ``session_id``, ``user_id`` и ``request_id`` берутся из контекста вызова и
        достаются каждому событию: событие без них не связать с оборотом, а
        принимать их из тела батча — значит разрешить вызовцу подписать журнал
        чужой сессией.
        """
        counters = service.log_events(
            events,
            session_id=ctx.session_id,
            user_id=ctx.user_id,
            request_id=ctx.request_id,
            audience=AUDIENCE_RUNTIME,
        )
        return json.dumps({"status": "ok", **counters}, ensure_ascii=False)

    description = (
        "Записать пачку событий в долговечный журнал gateway одним вызовом. "
        "Служебная операция: вызывается агентом при пакетном сбросе буфера, "
        "не моделью. Неблокирующая: события встают в буфер, когда писать в базу "
        "— решает буфер. Возвращает счётчики accepted и dropped: переполнение "
        "видно вызывающему."
    )

    return ToolDefinition(
        name="data.log_events",
        description=description,
        handler=handle_log_events,
        capability="data",
        tags=("logging", "infrastructure", "runtime-only"),
        permissions=("data:log_events",),
        input_schema=INPUT_SCHEMA,
    )
