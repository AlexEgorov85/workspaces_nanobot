"""Операция ``claim_task``: атомарный захват задач очереди.

Служебная, ``runtime-only``: очередь обслуживает канал агента, а не модель.
Значит, и ``permission`` своя — ``data:claim_task``.

Возвращается пачка захваченных задач в доменном виде либо пустой список.
Пустой список — не ошибка: пустая очередь это нормальное состояние, и
оборачивать его в исключение заставило бы канал отличать «нечего делать» от
«сломалось» по тексту ответа.

**Батч здесь — требование, а не украшение.** Change
``2026-10-02-task-queue-into-mcp`` переносит очередь в capability ``data``,
и каждый опрос и каждое обновление статуса после этого платят круговой
оборот по stdio. По одной задаче на оборот опрос стоил бы дороже, чем
прямой SQL, который он заменяет, — то есть миграция ускорила бы ровно то,
что должна была убрать. Пачка возвращает до ``batch`` задач за один оборот,
и ``batch=1`` остаётся поведением одиночного захвата.

Имя таблицы — из ``platform.json`` (``data.task_table``), а не из тела вызова.
Именно это и было причиной удаления обеих операций очереди в пункте 2.18
спеки ``enterprise-mcp-platform``: объявляя имя таблицы, вызывающая сторона
выбирала, чьи данные трогать, то есть вход в данные агента шёл мимо его
конфигурации. Change 2026-10-02-task-queue-into-mcp возвращает операции с
другой схемой — имя объявляет платформа.
"""

from __future__ import annotations

import json

from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.execution.context import ToolExecutionContext
from libs.enterprise_common.registry import ToolDefinition
from servers.enterprise.capabilities.data.service.main import (
    AUDIENCE_RUNTIME,
    DataService,
)

#: Схема входа задана руками, а не ``build_input_schema``: та выводит из
#: подписи ``{"type": "array"}`` без описания элемента, а контракт здесь и
#: есть в элементах. Операция ``runtime-only``, так что схему модель не
#: видит — она нужна как исполняемый контракт для канала.
INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "error_retry_delay_sec": {
            "type": "number",
            "description": (
                "Через сколько секунд задача в статусе error становится "
                "доступной повторно. По умолчанию 5."
            ),
        },
        "priority_contents": {
            # null — законное значение, а не ошибка: обработчик объявляет
            # ``list[str] | None = None``, и именно None означает «без
            # фильтра по содержимому» (обычный поллинг). Описание поля
            # ниже говорит ровно это: «Не задан — обычный поллинг».
            #
            # Раньше здесь стоял только "array", и обычный поллинг падал
            # на каждом опросе с «None is not of type 'array'».
            #
            # Отличать «не задан» (null) от «задан пустым» ([]) обязан
            # не тип, а исполнение: пустой список добавляет
            # ``AND content = ANY(%s)``, то есть отбор, не находящий
            # ничего. Это закреплено тестом
            # ``TestClaimTaskPriority::test_empty_priority_list_still_claims``.
            #
            # Объявление стало исполнимым 2026-10-04: загрузчик больше не
            # затирает его выводом из подписи. До этой правки объявление было
            # верным и неиспользуемым — и именно поэтому обычный поллинг падал,
            # хотя ``null`` был объявлен и обработчиком принимался.
            "type": ["array", "null"],
            "items": {"type": "string"},
            "description": (
                "Список команд, которые берутся раньше очереди (priority-"
                "поллинг канала). Не задан (null) — обычный поллинг по "
                "времени создания."
            ),
        },
        "batch": {
            "type": "integer",
            "minimum": 1,
            "description": (
                "Сколько задач взять за один вызов. По умолчанию 1 — "
                "поведение одиночного захвата. Значения выше 1 уменьшают "
                "число круговых оборотов по stdio на опрос; в пачке не более "
                "одной задачи на чат."
            ),
        },
        "cursor": {
            # null — законное значение и здесь: курсор не задан на первом
            # опросе, и именно это означает «начать с головы очереди».
            "type": ["string", "null"],
            "description": (
                "Курсор продолжения из ответа предыдущего вызова "
                "(next_cursor). Не задан (null) — начать с самой старой "
                "доступной задачи. Нужен, когда очередь не помещается в "
                "один батч."
            ),
        },
    },
}


def create_tool(registry_container: ToolContainer) -> ToolDefinition:
    # Сервис замыкается обработчиком: глобальная привязка означала бы, что
    # вызов уходит в сервис той регистрации, которая отработала последней.
    service: DataService = registry_container.get("data")

    def handle_claim_task(
        ctx: ToolExecutionContext,
        error_retry_delay_sec: float = 5.0,
        priority_contents: list[str] | None = None,
        batch: int = 1,
        cursor: str | None = None,
    ) -> str:
        """Захватить до ``batch`` задач очереди и вернуть их.

        Захват атомарный: два конкурирующих вызова не получат одну строку.
        Задача считается занятой, если в том же чате уже есть необработанное
        сообщение, — поэтому в пачке не более одной задачи на чат, даже
        если слотов несколько.

        ``session_id``/``user_id`` из контекста вызова в захват не входят:
        отбор ведётся по данным самой строки задачи, и подмена личности
        вызывающим здесь ничего бы не меняла.
        """
        claimed = service.claim_tasks(
            audience=AUDIENCE_RUNTIME,
            error_retry_delay_sec=error_retry_delay_sec,
            priority_contents=priority_contents,
            batch=batch,
            cursor=cursor,
        )
        return json.dumps(
            {
                "status": "ok",
                "claimed": claimed.tasks,
                "next_cursor": claimed.next_cursor,
            },
            ensure_ascii=False,
            default=str,
        )

    description = (
        "Атомарно захватить задачи очереди: до batch самых старых доступных "
        "задач пользователя, если их чаты не заняты. Служебная операция — "
        "вызывается каналом при поллинге, не моделью. Возвращает пачку задач "
        "и курсор продолжения, когда батч заполнен целиком."
    )

    return ToolDefinition(
        name="claim_task",
        description=description,
        handler=handle_claim_task,
        category="data",
        tags=("queue", "infrastructure", "runtime-only"),
        permissions=("data:claim_task",),
        input_schema=INPUT_SCHEMA,
    )
