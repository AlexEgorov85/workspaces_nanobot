from __future__ import annotations

import asyncio
import json
import time
from copy import deepcopy
from typing import Any

from loguru import logger
from nanobot.agent import AgentHook, AgentHookContext
from nanobot.agent.loop import AgentLoop, TurnContext

from .reviewer import run_review
from hooks.tool_audit_hook import ToolAuditHook


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

    @property
    def triggered(self) -> bool:
        return self._injected

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
        enabled_checks: set[str] | None = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.max_review_retries = max_review_retries
        self._reviewer_model = reviewer_model
        self._enabled_checks = enabled_checks
        self._extra_hooks.append(RepeatGuardHook(max_repeats=max_tool_repeats))
        self._extra_hooks.append(ToolAuditHook())

    async def _state_run(self, ctx: TurnContext) -> str:
        if ctx.visible_run_started_at is None:
            ctx.visible_run_started_at = time.time()
        await self._webui_turns.publish_run_status(
            ctx.msg, "running",
            started_at=ctx.visible_run_started_at,
        )

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

        # locate RepeatGuardHook and ToolAuditHook for metadata
        guard = self._find_guard_hook()
        audit_hook = self._find_audit_hook()

        review_info: dict[str, Any] = {
            "quality": "skipped",
            "attempts": 0,
            "issues": [],
            "tool_repeat_stopped": False,
        }

        for attempt in range(self.max_review_retries + 1):
            result = await self._run_agent_loop(ctx.initial_messages, **base_kwargs)
            final_content, tools_used, all_msgs, stop_reason, had_injections = result

            ctx.final_content = final_content
            ctx.tools_used = tools_used
            ctx.all_messages = all_msgs
            ctx.stop_reason = stop_reason
            ctx.had_injections = had_injections

            if guard:
                review_info["tool_repeat_stopped"] = guard.triggered

            # Hard stop on fatal errors — replace response with honest message
            if stop_reason in ("error", "tool_error"):
                review_info["reason"] = f"stop_reason={stop_reason}"
                break

            if stop_reason == "max_iterations":
                ctx.final_content = (
                    "Я не смог получить данные — достигнут лимит итераций. "
                    "Попробуй уточнить запрос или проверить доступность данных вручную."
                )
                ctx.stop_reason = "max_iterations"
                review_info["quality"] = "blocked"
                review_info["reason"] = "max_iterations — replaced with honest message"
                break

            review = await run_review(
                self.provider,
                self._reviewer_model or self.model,
                final_content or "",
                all_msgs,
                enabled_checks=self._enabled_checks,
            )

            review_info["attempts"] = attempt + 1
            review_info["quality"] = review.get("quality", "bad")
            review_info["issues"] = review.get("issues", [])
            review_info["reason"] = review.get("reason", "")

            if review.get("quality") == "good":
                logger.info(
                    "Review passed (attempt {}/{})",
                    attempt + 1,
                    self.max_review_retries + 1,
                )
                break

            if attempt >= self.max_review_retries:
                ctx.final_content = (
                    "Не удалось получить ответ на ваш вопрос — "
                    "все попытки проверки не прошли. "
                    "Попробуйте переформулировать запрос."
                )
                review_info["quality"] = "blocked"
                review_info["reason"] = "all review retries exhausted"
                logger.warning(
                    "Review failed after {} attempts for session {}",
                    self.max_review_retries + 1,
                    ctx.session_key,
                )
                break

            # Build correction and retry
            correction_text = self._build_review_correction(review, attempt + 1)
            ctx.initial_messages = deepcopy(all_msgs)
            ctx.initial_messages.append({
                "role": "user",
                "content": correction_text,
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

        # Store review metadata on the outbound message
        ctx.msg.metadata["_review"] = review_info

        # Store tool audit trail (all tool calls with status, errors, args)
        if audit_hook:
            entries = audit_hook.drain()
            if entries:
                ctx.msg.metadata["_tool_audit"] = entries

        return "ok"

    def _find_guard_hook(self) -> RepeatGuardHook | None:
        for hook in self._extra_hooks:
            if isinstance(hook, RepeatGuardHook):
                return hook
        return None

    def _find_audit_hook(self) -> ToolAuditHook | None:
        for hook in self._extra_hooks:
            if isinstance(hook, ToolAuditHook):
                return hook
        return None

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
            "6. Не пиши пустые отписки вроде «не смог получить данные» или «попробуйте ещё раз» — "
            "обязательно опиши, КАКИЕ инструменты вызывал и ЧТО именно пошло не так "
            "(ошибка, пустой результат, неверный путь и т.п.).",
            "7. Отвечай на русском языке — язык ответа должен совпадать с языком запроса пользователя.",
            "8. Отвечай НА вопрос пользователя, а не описывай процесс. "
            "Первое предложение — прямой ответ (даже если ответ «не знаю» или «не могу найти»). "
            "Только после этого можно объяснять, что делал и почему не вышло.",
            "9. НЕ проси пользователя что-то делать (запускать скрипты, искать файлы, проверять пути). "
            "Если данных нет — объясни почему. Если нужно уточнение — задай короткий вопрос. "
            "Всю работу делаешь ТЫ, а не пользователь.",
        ])
        return "\n".join(lines)


def _drain_queue(q: asyncio.Queue) -> None:
    while True:
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            break
