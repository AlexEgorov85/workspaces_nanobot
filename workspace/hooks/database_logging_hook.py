"""DatabaseLoggingHook — AgentHook для логирования событий агента в БД.

Реализуется как ``AgentHook`` (async-методы из nanobot.agent.hook) для
tool-событий, и использует ``BusFactory`` (обёртки ``publish_inbound`` /
``publish_outbound``) для content-сообщений. НЕ использовать как обычный
sync-класс — он не подключается к циклу агента.

Подключение:
  * ``AgentFactory.create(..., db_logging_service=svc)`` — добавляет хук
    в ``AgentLoop.from_config(hooks=[...])``;
  * ``BusFactory(inbound_logger=..., outbound_logger=...)`` — обёртки шины.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from nanobot.agent import AgentHookContext, AgentRunHookContext

from .base_tool_tracking_hook import BaseToolTrackingHook

logger = logging.getLogger(__name__)


class DatabaseLoggingHook(BaseToolTrackingHook):
    """Агентский хук — пересылает tool- и run-события в DbLoggingService.

    Все методы НЕБЛОКИРУЮЩИЕ: ``DbLoggingService.log_*`` ставит события
    в очередь и возвращает ``True/False`` мгновенно.
    """

    def __init__(self, db_logging_service: Any, agent_id: Optional[str] = None) -> None:
        super().__init__()
        self._service = db_logging_service
        self._tool_start_times: Dict[str, float] = {}
        self._agent_id = agent_id
        # Контекст текущего оборота/вопроса: в AgentRunHookContext его нет,
        # поэтому ловим из AgentHookContext (before_iteration/before_execute_tool)
        # и прокидываем во все события вопроса.
        self._run_session_key: Optional[str] = None
        self._request_id: Optional[str] = None

    # ------------------------------------------------------------------
    # Tool-события
    # ------------------------------------------------------------------

    def _capture_context(self, context: Any) -> None:
        """Подхватить session_key и request_id текущего вопроса."""
        session_key = getattr(context, "session_key", None)
        if not session_key:
            return
        self._run_session_key = session_key
        self._request_id = self._service.get_request_id(session_key)

    def _ctx(self, key: str) -> Optional[str]:
        # Упрощено: контекст вопроса теперь живёт в question_runs,
        # в события gateway_logs идёт только request_id.
        if key == "request_id":
            return self._request_id
        if key == "agent_id":
            return self._agent_id
        return None

    async def before_execute_tool(
        self,
        context: AgentHookContext,
        tool_call: Any,
        tool: Any,
        params: Any,
    ) -> None:
        tool_call_id = self._tool_call_id(tool_call)
        self._tool_start_times[tool_call_id] = time.time()
        self._capture_context(context)
        try:
            self._service.log_tool_call(
                session_id=context.session_key or "",
                tool_name=self._tool_call_name(tool_call),
                args=params if isinstance(params, dict) else {},
                tool_call_id=tool_call_id,
                request_id=self._request_id,
            )
        except Exception as exc:
            logger.warning("DbLoggingHook.before_execute_tool failed: %s", exc)

    async def after_execute_tool(
        self,
        context: AgentHookContext,
        tool_call: Any,
        tool: Any,
        params: Any,
        result: Any,
    ) -> None:
        tool_call_id = self._tool_call_id(tool_call)
        start = self._tool_start_times.pop(tool_call_id, None)
        latency_ms = (time.time() - start) * 1000.0 if start is not None else 0.0
        try:
            self._service.log_tool_result(
                session_id=context.session_key or "",
                tool_name=self._tool_call_name(tool_call),
                result=result,
                latency_ms=latency_ms,
                tool_call_id=tool_call_id,
                status="ok",
                request_id=self._request_id,
            )
        except Exception as exc:
            logger.warning("DbLoggingHook.after_execute_tool failed: %s", exc)

    async def on_execute_tool_error(
        self,
        context: AgentHookContext,
        tool_call: Any,
        tool: Any,
        params: Any,
        error: Any,
    ) -> None:
        tool_call_id = self._tool_call_id(tool_call)
        start = self._tool_start_times.pop(tool_call_id, None)
        latency_ms = (time.time() - start) * 1000.0 if start is not None else 0.0
        try:
            self._service.log_tool_result(
                session_id=context.session_key or "",
                tool_name=self._tool_call_name(tool_call),
                result=None,
                latency_ms=latency_ms,
                tool_call_id=tool_call_id,
                status="error",
                error=str(error),
                level="ERROR",
                request_id=self._request_id,
            )
        except Exception as exc:
            logger.warning("DbLoggingHook.on_execute_tool_error failed: %s", exc)

    # ------------------------------------------------------------------
    # Run-level summary
    # ------------------------------------------------------------------

    async def before_iteration(self, context: Any) -> None:
        # AgentRunHookContext не содержит session_key, ловим его здесь
        # (передаётся AgentHookContext со spec.session_key)
        self._capture_context(context)

    async def after_run(self, context: AgentRunHookContext) -> None:
        try:
            self._service.log_event(
                _make_run_event(context, self._run_session_key, self._request_id)
            )
            if self._request_id:
                self._service.finish_request(
                    self._request_id,
                    status="error" if context.error else "finished",
                    summary=(context.final_content or "")[:200] or None,
                    response=context.final_content or None,
                )
        except Exception as exc:
            logger.warning("DbLoggingHook.after_run failed: %s", exc)
        finally:
            if self._run_session_key:
                self._service.clear_request(self._run_session_key)
                self._request_id = None


def _make_run_event(
    context: AgentRunHookContext,
    session_key: Optional[str] = None,
    request_id: Optional[str] = None,
):
    """Сформировать LogEvent из AgentRunHookContext (без жёсткой связки)."""
    from lib.services.db_logging_service import LogEvent

    final = context.final_content or ""
    tools = context.tools_used or []
    payload: Dict[str, Any] = {
        "final_content": final,
        "tools_used": tools,
        "stop_reason": context.stop_reason,
        "had_injections": context.had_injections,
    }
    if request_id:
        payload["request_id"] = request_id
    return LogEvent(
        event_type="run_finished",
        level="ERROR" if context.error else "INFO",
        actor="agent",
        session_id=session_key,
        request_id=request_id,
        summary=final[:200],
        payload=payload,
        metadata={
            "tokens_used": (context.usage or {}).get("total_tokens"),
            "had_error": bool(context.error),
        },
    )
