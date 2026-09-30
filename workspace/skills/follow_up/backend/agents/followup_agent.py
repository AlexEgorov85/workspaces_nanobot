"""Follow Up 2.0 — FollowUp Agent (поиск по прошлым проверкам)."""
from __future__ import annotations
from typing import Dict, List, Optional
from backend.agents.base import BaseAgent
from backend.llm.client import generate_async
from backend.llm.prompts._shared import FOLLOWUPS_INSTRUCTION, HISTORY_BLOCK_TEMPLATE
from backend.llm.prompts.followup import FOLLOWUP_SYSTEM, FOLLOWUP_USER_TEMPLATE
from backend.rag.context_builder import build_context
from backend.rag.query_understanding import QueryContext


class FollowUpAgent(BaseAgent):
    agent_type = "followup"

    async def _generate(
        self,
        query_ctx: QueryContext,
        chunks: List[Dict],
        model: Optional[str] = None,
        history: Optional[List[Dict]] = None,
    ) -> str:
        context = build_context(chunks)
        history_block = HISTORY_BLOCK_TEMPLATE.format(
            history=self._format_history(history or []),
        )
        user_content = history_block + FOLLOWUP_USER_TEMPLATE.format(
            query=query_ctx.raw_query,
            context=context,
        )
        messages = [
            {"role": "system", "content": FOLLOWUP_SYSTEM + FOLLOWUPS_INSTRUCTION},
            {"role": "user", "content": user_content},
        ]
        return await generate_async(messages, model=model)
