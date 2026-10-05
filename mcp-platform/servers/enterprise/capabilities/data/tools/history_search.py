"""Операция ``history_search`` — поиск по журналу в пределах области видимости.

Изоляция обязательна: поиск без ``user_id`` и без ``session_id`` отклоняется.
В журнале лежат вопросы пользователей и внутренние события, поэтому «покажи всё»
— это утечка, а не удобный режим.
"""

from __future__ import annotations

import json

from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.execution.context import ToolExecutionContext
from libs.enterprise_common.registry import ToolDefinition, build_input_schema
from servers.enterprise.capabilities.data.service.main import (
    AUDIENCE_MODEL,
    DataService,
)


#: Сервис замыкается обработчиком: модульная глобальная переменная зависела бы
#: от порядка регистрации операций, и вторая регистрация в процессе тихо
#: переписала бы первую.
def create_tool(registry_container: ToolContainer) -> ToolDefinition:
    service: DataService = registry_container.get("data")

    def handle_history_search(
        ctx: ToolExecutionContext,
        query: str = "",
        event_type: str | None = None,
        level: str | None = None,
        tool_name: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> str:
        """Найти события журнала в пределах сессии вызова.

        Идентичность приходит в ``params._meta`` и подставляется конвейером в
        ``context`` (§ ``runtime/call-contract``). Объявлять её параметрами
        обработчика нельзя: в опубликованной схеме она стала бы полем, которое
        модель заполняет, а область видимости должна задаваться вызывающей
        стороной, а не моделью.
        """
        page = service.history_search(
            query=query,
            event_type=event_type,
            level=level,
            tool_name=tool_name,
            session_id=ctx.session_id,
            user_id=ctx.user_id,
            since=since,
            until=until,
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

    return ToolDefinition(
        name="data.history_search",
        description=(
            "Поиск по долговечному журналу gateway в пределах сессии вызова. "
            "Область видимости задаёт вызывающая сторона, у модели её нет."
        ),
        handler=handle_history_search,
        capability="data",
        tags=("logging", "infrastructure"),
        quality_policy="vector_result",
        input_schema=build_input_schema(handle_history_search),
    )
