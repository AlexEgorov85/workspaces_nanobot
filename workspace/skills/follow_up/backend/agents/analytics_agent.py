"""Follow Up 2.0 — Analytics Agent (статистика и аналитика)."""
from __future__ import annotations
from typing import Dict, List, Optional
from backend.agents.base import BaseAgent
from backend.llm.client import generate_async
from backend.llm.prompts._shared import FOLLOWUPS_INSTRUCTION, HISTORY_BLOCK_TEMPLATE
from backend.llm.prompts.analytics import ANALYTICS_SYSTEM, ANALYTICS_USER_TEMPLATE
from backend.rag.context_builder import build_context
from backend.rag.query_understanding import QueryContext
from backend.storage.database import DeviationRepo, DocumentRepo, get_db


def _fmt_money(value: Optional[float]) -> str:
    """Форматирует сумму в человеко-читаемом виде."""
    if not value or value <= 0:
        return "—"
    if value >= 1_000_000_000:
        return f"{value/1_000_000_000:.2f} млрд руб."
    if value >= 1_000_000:
        return f"{value/1_000_000:.2f} млн руб."
    if value >= 1_000:
        return f"{value/1_000:.0f} тыс. руб."
    return f"{value:.0f} руб."


class AnalyticsAgent(BaseAgent):
    agent_type = "analytics"

    async def _generate(
        self,
        query_ctx: QueryContext,
        chunks: List[Dict],
        model: Optional[str] = None,
        history: Optional[List[Dict]] = None,
    ) -> str:
        context = build_context(chunks)

        with get_db() as db:
            cat_stats = DeviationRepo.get_categories_stats(db)
            sev_stats = DeviationRepo.get_severity_stats(db)
            total_docs = DocumentRepo.count(db)
            total_devs = DeviationRepo.count(db)
            financial_total = DeviationRepo.get_financial_impact_total(db)
            financial_by_cat = DeviationRepo.get_financial_impact_by_category(db)
            top_systems = DeviationRepo.get_top_systems(db, limit=10)
            top_regs = DeviationRepo.get_top_regulations(db, limit=10)
            km = query_ctx.km_numbers[0] if query_ctx.km_numbers else None
            topic = query_ctx.topic or query_ctx.raw_query
            deviations = DeviationRepo.search(db, query=topic[:100], check_id=km)

        stats_lines = [
            f"Всего документов в базе: {total_docs}",
            f"Всего выявленных отклонений: {total_devs}",
            f"Суммарный финансовый ущерб (по упомянутым в актах): {_fmt_money(financial_total)}",
            "",
            "Распределение по категориям:",
        ]
        for s in cat_stats[:12]:
            stats_lines.append(f"  - {s['category']}: {s['count']}")

        stats_lines.append("\nРаспределение по критичности:")
        for s in sev_stats:
            stats_lines.append(f"  - {s['severity']}: {s['count']}")

        if financial_by_cat:
            stats_lines.append("\nФинансовый ущерб по категориям (где упомянуто):")
            for s in financial_by_cat[:10]:
                stats_lines.append(
                    f"  - {s['category']}: {_fmt_money(s['total_rub'])} ({s['count']} нарушений)"
                )

        if top_systems:
            stats_lines.append("\nТоп систем под риском:")
            for s in top_systems:
                stats_lines.append(f"  - {s['system']}: {s['count']} упоминаний")

        if top_regs:
            stats_lines.append("\nТоп упоминаемых нормативов:")
            for s in top_regs:
                stats_lines.append(f"  - {s['regulation']}: {s['count']} нарушений")

        stats_lines.append("\nРелевантные отклонения по запросу:")
        for d in deviations[:20]:
            money_part = f" [{_fmt_money(d.financial_impact_rub)}]" if d.financial_impact_rub else ""
            stats_lines.append(
                f"  [{d.check_id}] [{d.category}] [{d.severity}]{money_part} {d.description[:120]}"
            )

        deviations_stats = "\n".join(stats_lines)
        history_block = HISTORY_BLOCK_TEMPLATE.format(
            history=self._format_history(history or []),
        )
        user_content = history_block + ANALYTICS_USER_TEMPLATE.format(
            query=query_ctx.raw_query,
            deviations_stats=deviations_stats,
            context=context,
        )
        messages = [
            {"role": "system", "content": ANALYTICS_SYSTEM + FOLLOWUPS_INSTRUCTION},
            {"role": "user", "content": user_content},
        ]
        return await generate_async(messages, model=model)
