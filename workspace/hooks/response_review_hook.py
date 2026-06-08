import json
import re
from pathlib import Path
from typing import Any

from nanobot.agent import AgentHook, AgentHookContext, AgentRunHookContext


class ResponseReviewHook(AgentHook):
    """Проверяет финальный ответ на соответствие tool results.

    Если ответ содержит факты, не подтверждённые результатами инструментов,
    добавляет предупреждение в content и пишет отчёт в workspace/reviews/.
    """

    def __init__(self, workspace_dir: str | None = None):
        super().__init__()
        self._workspace = Path(workspace_dir) if workspace_dir else None
        self._session_key: str | None = None
        self._issues: list[str] = []

    def finalize_content(self, ctx: AgentHookContext, content: str | None) -> str | None:
        """Проверяет content перед финальной выдачей."""
        if not content or not ctx.tool_results:
            return content

        issues = self._check_claims_vs_tool_results(content, ctx.tool_results, ctx.tool_calls)
        if not issues:
            return content

        warning = "\n\n---\n⚠ **Self-review: в ответе есть утверждения, не подтверждённые tool results:**\n"
        for issue in issues:
            warning += f"- {issue}\n"
        warning += "Перепроверьте факты и при необходимости уточните.\n---"

        self._issues = issues
        return content + warning

    async def after_run(self, ctx: AgentRunHookContext) -> None:
        """Сохраняет отчёт о проверке после завершения."""
        if not self._issues:
            return
        if self._workspace:
            reviews_dir = self._workspace / "reviews"
            reviews_dir.mkdir(exist_ok=True)
            report = {
                "tool_events": ctx.tool_events,
                "issues": self._issues,
                "final_content_preview": (ctx.final_content or "")[:500],
            }
            review_path = reviews_dir / f"review_{ctx.session_key or 'unknown'}.json"
            review_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def _check_claims_vs_tool_results(
        self,
        content: str,
        tool_results: list[Any],
        tool_calls: list[Any],
    ) -> list[str]:
        """Ищет в content числовые/фактологические утверждения, не встречающиеся в tool_results."""
        issues: list[str] = []

        all_results_text = " ".join(
            str(r) for r in tool_results if r is not None
        ).lower()

        numbers = re.findall(r'\b\d{2,}(?:[.,]\d+)?\b', content)
        tool_names = [tc.name for tc in tool_calls] if tool_calls else []

        for num in numbers:
            if num not in all_results_text:
                issues.append(f"Число «{num}» не найдено в tool results")

        if tool_names and not any(name in content.lower() for name in tool_names):
            if len(tool_names) <= 3:
                issues.append(
                    f"В ответе не упоминаются вызванные инструменты: {', '.join(tool_names)}"
                )

        return issues
