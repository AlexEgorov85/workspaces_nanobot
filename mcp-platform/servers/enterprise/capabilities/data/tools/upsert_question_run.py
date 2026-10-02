"""Операция ``upsert_question_run``: контекст вопроса в ``agent_question_runs``.

``runtime-only``: операцией пользуется сам агент при регистрации вопроса и при
завершении прогона, а не модель. Модельной она стала бы вторым путём записи в
таблицу, где она не должна ничего писать.
"""

from __future__ import annotations

import json
from typing import Any

from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.execution.context import ToolExecutionContext
from libs.enterprise_common.registry import ToolDefinition, build_input_schema

AUDIENCE_RUNTIME = "runtime"


def create_tool(container: ToolContainer) -> ToolDefinition:
    def upsert_question_run(
        ctx: ToolExecutionContext,
        status: str | None = None,
        summary: str | None = None,
        chat_id: str | None = None,
        channel: str | None = None,
        agent_id: str | None = None,
        parent_agent_id: str | None = None,
        parent_request_id: str | None = None,
        is_subagent: bool = False,
        question: str | None = None,
        response: str | None = None,
        media: list[Any] | None = None,
        update_only: bool = False,
    ) -> str:
        # ``request_id`` — ключ оборота, а не доменное поле: он приходит в
        # ``params._meta`` и подставляется конвейером. Объявление его параметром
        # означало бы, что модель может подменить оборот, записав контекст
        # чужого вопроса.
        data = container.get("data")
        # Исход записи — часть ответа, а не деталь реализации: «created» или
        # «updated» означают, что строка в таблице ЕСТЬ. Сервис бросает
        # ``InfrastructureError``, если не затронута ни одна строка, поэтому
        # ``status: ok`` ниже недостижим без фактической записи — раньше он
        # был единственным ответом операции при любом исходе, включая тот, где
        # записи не было вовсе.
        outcome = data.upsert_question_run(
            ctx.request_id,
            session_id=ctx.session_id,
            user_id=ctx.user_id,
            chat_id=chat_id,
            channel=channel,
            agent_id=agent_id,
            parent_agent_id=parent_agent_id,
            parent_request_id=parent_request_id,
            is_subagent=is_subagent,
            status=status,
            summary=summary,
            question=question,
            response=response,
            media=media,
            update_only=update_only,
            audience=AUDIENCE_RUNTIME,
        )
        return json.dumps(
            {
                "status": "ok",
                "request_id": ctx.request_id,
                "outcome": str(outcome),
            },
            ensure_ascii=False,
        )

    description = (
        "Записать или обновить контекст вопроса агента. Контекст пишется один "
        "раз на request_id и не дублируется на каждое событие журнала. "
        "update_only=true обновляет только статус, summary и ответ, не затирая "
        "ранее записанный контекст вопроса, — этим пользуется завершение "
        "прогона. Служебная операция: вызывается агентом, не моделью."
    )

    return ToolDefinition(
        name="upsert_question_run",
        description=description,
        handler=upsert_question_run,
        category="data",
        tags=("infrastructure", "runtime-only"),
        permissions=("data:upsert_question_run",),
        input_schema=build_input_schema(upsert_question_run),
        quality_policy="none",
    )
