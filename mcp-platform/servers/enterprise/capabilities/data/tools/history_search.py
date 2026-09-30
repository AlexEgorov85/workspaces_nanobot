"""Операция ``history_search`` — поиск по журналу в пределах области видимости.

Изоляция обязательна: поиск без ``user_id`` и без ``session_id`` отклоняется.
В журнале лежат вопросы пользователей и внутренние события, поэтому «покажи всё»
— это утечка, а не удобный режим.
"""

from __future__ import annotations

import json
from typing import Any

from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.registry import ToolDefinition
from servers.enterprise.capabilities.data.service.main import (
    AUDIENCE_MODEL,
    DataService,
)

#: Контейнер подставляется загрузчиком при регистрации операции.
container: ToolContainer | None = None


def handle_history_search(
    session_id: str,
    user_id: str | None = None,
    query: str = "",
    event_type: str | None = None,
    level: str | None = None,
    since: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> str:
    """Найти события журнала. Требует session_id либо user_id."""
    if container is None:  # pragma: no cover - защита от неверной сборки
        raise RuntimeError("контейнер не инициализирован")
    service: DataService = container.get("data")
    page = service.history_search(
        query=query,
        event_type=event_type,
        level=level,
        session_id=session_id,
        user_id=user_id,
        since=since,
        limit=limit,
        offset=offset,
        audience=AUDIENCE_MODEL,
    )
    return json.dumps(
        {
            "hits": [
                {
                    "id": hit.id,
                    "timestamp": str(hit.timestamp),
                    "event_type": hit.event_type,
                    "name": hit.name,
                    "level": hit.level,
                    "summary": hit.summary,
                    "payload": hit.payload,
                }
                for hit in page.hits
            ],
            "next_offset": page.next_offset,
            "truncated": page.truncated,
        },
        ensure_ascii=False,
    )


def create_tool(registry_container: ToolContainer) -> ToolDefinition:
    global container
    container = registry_container
    return ToolDefinition(
        name="history_search",
        description=(
            "Поиск по долговечному журналу gateway в пределах области видимости: "
            "требуется session_id или user_id."
        ),
        handler=handle_history_search,
        category="data",
        tags=("logging", "infrastructure"),
    )
