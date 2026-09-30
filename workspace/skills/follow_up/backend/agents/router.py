"""
Follow Up 2.0 — Agent Router.

Принимает QueryContext (с уже определённым intent) и создаёт нужный агент.
"""
from __future__ import annotations
from backend.agents.analytics_agent import AnalyticsAgent
from backend.agents.base import BaseAgent
from backend.agents.followup_agent import FollowUpAgent
from backend.agents.general_agent import GeneralAgent
from backend.agents.hypothesis_agent import HypothesisAgent
from backend.agents.km_detail_agent import KMDetailAgent
from backend.agents.recheck_agent import RecheckAgent
from backend.rag.query_understanding import Intent, QueryContext


def get_agent(query_ctx: QueryContext) -> BaseAgent:
    """Возвращает нужный агент по intent из QueryContext."""
    intent_map = {
        Intent.FOLLOWUP: FollowUpAgent,
        Intent.HYPOTHESIS: HypothesisAgent,
        Intent.RECHECK: RecheckAgent,
        Intent.ANALYTICS: AnalyticsAgent,
        Intent.KM_DETAIL: KMDetailAgent,
        Intent.GENERAL: GeneralAgent,
        Intent.REPORT: GeneralAgent,  # TODO: добавить отдельный Report agent
    }
    agent_cls = intent_map.get(query_ctx.intent, GeneralAgent)
    return agent_cls()
