from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from typing import Any

from loguru import logger
from nanobot.agent import AgentHook, AgentHookContext
from nanobot.agent.loop import AgentLoop, TurnContext, publish_turn_run_status

from .reviewer import run_review


class RepeatGuardHook(AgentHook):
    """Прерывает циклические повторения одного и того же инструмента.

    Если агент вызывает один инструмент с одинаковыми параметрами
    N раз подряд без полезного результата — подменяет содержимое
    последнего tool result на принудительную остановку.
    """

    def __init__(self, max_repeats: int = 3):
        super().__init__()
        self._signatures: list[tuple[str, str]] = []
        self._max_repeats = max_repeats
        self._injected = False

    async def after_iteration(self, ctx: AgentHookContext) -> None:
        if self._injected:
            return

        for tc in ctx.tool_calls:
            args_str = (
                json.dumps(tc.arguments, sort_keys=True)
                if isinstance(tc.arguments, dict)
                else str(tc.arguments or "")
            )
            self._signatures.append((tc.name, args_str))

        if len(self._signatures) < self._max_repeats:
            return

        recent = self._signatures[-self._max_repeats:]
        if len(set(recent)) > 1:
            return

        name = recent[0][0]
        self._injected = True
        for msg in reversed(ctx.messages):
            if msg.get("role") == "tool" and msg.get("name") == name:
                msg["content"] = (
                    f"[SYSTEM: REPEATED CALL BLOCKED] "
                    f"Ты вызвал '{name}' с одинаковыми параметрами "
                    f"{self._max_repeats} раз подряд без полезного результата. "
                    f"НЕ ПОВТОРЯЙ этот вызов. "
                    f"Сообщи пользователю, что не можешь получить данные, "
                    f"и опиши причину (ошибка, пустой результат, недоступность)."
                )
                break


class ReviewAgentLoop(AgentLoop):
    """AgentLoop с самопроверкой финального ответа через LLM-ревьюер.

    После каждого прогона агента проверяет, что финальный ответ основан
    на реальных tool results. Если нет — отправляет ответ на доработку.
    """

    def __init__(
        self,
        *args,
        max_review_retries: int = 2,
        reviewer_model: str | None = None,
        max_tool_repeats: int = 3,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.max_review_retries = max_review_retries
        self._reviewer_model = reviewer_model
        self._extra_hooks.append(RepeatGuardHook(max_repeats=max_tool_repeats))

    async def _state_run(self, ctx: TurnContext) -> str:
        await publish_turn_run_status(self.bus, ctx.msg, "running")

        base_kwargs: dict[str, Any] = {
            "on_progress": ctx.on_progress,
            "on_stream": ctx.on_stream,
            "on_stream_end": ctx.on_stream_end,
            "on_retry_wait": ctx.on_retry_wait,
            "session": ctx.session,
            "channel": ctx.msg.channel,
            "chat_id": ctx.msg.chat_id,
            "message_id": ctx.msg.metadata.get("message_id"),
            "metadata": ctx.msg.metadata,
            "session_key": ctx.session_key,
            "pending_queue": ctx.pending_queue,
        }

        for attempt in range(self.max_review_retries + 1):
            result = await self._run_agent_loop(ctx.initial_messages, **base_kwargs)
            final_content, tools_used, all_msgs, stop_reason, had_injections = result

            ctx.final_content = final_content
            ctx.tools_used = tools_used
            ctx.all_messages = all_msgs
            ctx.stop_reason = stop_reason
            ctx.had_injections = had_injections

            # Skip review if nothing to verify
            if not tools_used or stop_reason in ("error", "tool_error", "max_iterations"):
                return "ok"

            review = await run_review(
                self.provider,
                self._reviewer_model or self.model,
                final_content or "",
                all_msgs,
            )

            if review.get("quality") == "good":
                logger.info(
                    "Review passed (attempt {}/{})",
                    attempt + 1,
                    self.max_review_retries + 1,
                )
                return "ok"

            if attempt >= self.max_review_retries:
                warning = (
                    f"\n\n[Self-review: ответ не прошёл проверку после "
                    f"{self.max_review_retries + 1} попыток. "
                    f"Проверьте факты самостоятельно.]"
                )
                ctx.final_content = (final_content or "") + warning
                logger.warning(
                    "Review failed after {} attempts for session {}",
                    self.max_review_retries + 1,
                    ctx.session_key,
                )
                return "ok"

            # Build correction and retry
            correction = self._build_review_correction(review, attempt + 1)
            ctx.initial_messages = deepcopy(all_msgs)
            ctx.initial_messages.append({
                "role": "user",
                "content": correction,
            })

            # Disable streaming/progress for retry — only final result matters
            base_kwargs.update({
                "on_progress": None,
                "on_stream": None,
                "on_stream_end": None,
            })
            # Use a fresh pending queue or None
            if ctx.pending_queue is not None:
                _drain_queue(ctx.pending_queue)
                base_kwargs["pending_queue"] = None

            logger.info(
                "Review failed (attempt {}/{}), retrying... issues: {}",
                attempt + 1,
                self.max_review_retries + 1,
                review.get("issues", []),
            )

        return "ok"

    @staticmethod
    def _build_review_correction(review: dict, attempt: int) -> str:
        issues = review.get("issues", [])
        reason = review.get("reason", "Обнаружены расхождения с tool results")
        lines = [
            f"[Self-review, попытка {attempt}]",
            f"Проблема: {reason}",
            "",
            "Детали:",
        ]
        for issue in issues:
            lines.append(f"- {issue}")
        lines.extend([
            "",
            "Требования к исправлению:",
            "1. Убери ВСЕ данные, которых нет в tool results.",
            "2. Если данные в ответе не совпадают с tool results — исправь.",
            "3. Если выводы/расчёты ошибочны — пересчитай на основе tool results.",
            "4. Не придумывай новые данные — используй ТОЛЬКО то, что вернули инструменты.",
            "5. Части ответа, где всё верно, — оставь без изменений.",
        ])
        return "\n".join(lines)


def _drain_queue(q: asyncio.Queue) -> None:
    while True:
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            break
