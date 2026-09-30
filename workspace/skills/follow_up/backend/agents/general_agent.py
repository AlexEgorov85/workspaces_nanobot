"""Follow Up 2.0 — General Agent (общие вопросы)."""
from __future__ import annotations
from typing import Dict, List, Optional
from backend.agents.base import BaseAgent
from backend.llm.client import generate_async
from backend.llm.prompts._shared import FOLLOWUPS_INSTRUCTION, HISTORY_BLOCK_TEMPLATE
from backend.llm.prompts.general import GENERAL_SYSTEM, GENERAL_USER_TEMPLATE
from backend.rag.context_builder import build_context
from backend.rag.query_understanding import QueryContext


class GeneralAgent(BaseAgent):
    agent_type = "general"

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
        user_content = history_block + GENERAL_USER_TEMPLATE.format(
            query=query_ctx.raw_query,
            context=context,
        )
        messages = [
            {"role": "system", "content": GENERAL_SYSTEM + FOLLOWUPS_INSTRUCTION},
            {"role": "user", "content": user_content},
        ]
        return await generate_async(messages, model=model)
