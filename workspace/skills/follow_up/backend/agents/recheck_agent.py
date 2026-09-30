"""Follow Up 2.0 — Recheck Agent (анализ для репроверки)."""
from __future__ import annotations
from typing import Dict, List, Optional
from backend.agents.base import BaseAgent
from backend.agents.hypothesis_agent import _deviation_to_dict
from backend.llm.client import generate_async
from backend.llm.prompts._shared import FOLLOWUPS_INSTRUCTION, HISTORY_BLOCK_TEMPLATE
from backend.llm.prompts.recheck import RECHECK_SYSTEM, RECHECK_USER_TEMPLATE
from backend.rag.context_builder import build_context, build_deviations_context
from backend.rag.query_understanding import QueryContext
from backend.storage.database import DeviationRepo, get_db


class RecheckAgent(BaseAgent):
    agent_type = "recheck"

    async def _generate(
        self,
        query_ctx: QueryContext,
        chunks: List[Dict],
        model: Optional[str] = None,
        history: Optional[List[Dict]] = None,
    ) -> str:
        context = build_context(chunks)

        with get_db() as db:
            km = query_ctx.km_numbers[0] if query_ctx.km_numbers else None
            topic = query_ctx.topic or query_ctx.raw_query
            deviations = DeviationRepo.search(db, query=topic[:100], check_id=km)
            devs_data = [_deviation_to_dict(d) for d in deviations[:30]]

        deviations_text = build_deviations_context(devs_data)
        history_block = HISTORY_BLOCK_TEMPLATE.format(
            history=self._format_history(history or []),
        )
        user_content = history_block + RECHECK_USER_TEMPLATE.format(
            query=query_ctx.raw_query,
            context=context,
            deviations=deviations_text,
        )
        messages = [
            {"role": "system", "content": RECHECK_SYSTEM + FOLLOWUPS_INSTRUCTION},
            {"role": "user", "content": user_content},
        ]
        return await generate_async(messages, model=model)
