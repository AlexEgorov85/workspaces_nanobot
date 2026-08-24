"""DatabaseLoggingHook — AgentHook для логирования событий агента в БД.

Реализуется как ``AgentHook`` (async-методы из nanobot.agent.hook) для
tool-событий, и использует ``BusFactory`` (обёртки ``publish_inbound`` /
``publish_outbound``) для content-сообщений. НЕ использовать как обычный
sync-класс — он не подключается к циклу агента.

Подключение:
  * ``AgentFactory.create(..., db_logging_service=svc)`` — добавляет
    фабрику оборота ``make_db_logging_hook_factory`` в
    ``AgentLoop.from_config(hook_factories=[...])``. Фабрика создаёт
    СВЕЖИЙ ``DatabaseLoggingHook`` на каждый оборот (конкурентно-безопасно);
  * ``BusFactory(inbound_logger=..., outbound_logger=...)`` — обёртки шины.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from rich.console import Console

from .base_tool_tracking_hook import BaseToolTrackingHook

if TYPE_CHECKING:
    from nanobot.agent import AgentHookContext, AgentRunHookContext

logger = logging.getLogger(__name__)

console = Console()

# ---------------------------------------------------------------------------
# Мост per-iteration usage между хуком и патчами RuntimePatcher.
#
# Нужен для метрики занятости контекстного окна (``metadata.context_window``).
# Три участника:
#   * ``seed_context_window`` — патч ``agent._state_build`` (старт оборота)
#     кладёт лимит окна и модель в мост (канал сам лимит не знает);
#   * ``DatabaseLoggingHook.after_iteration`` пишет СВЕЖИЙ по-итерационный
#     ``context.usage`` (именно последняя итерация = то, что модель реально
#     видела в финальном запросе);
#   * ``RuntimePatcher.patch_assemble_outbound`` собирает готовый блок
#     ``context_window`` (usage посл. итерации ÷ лимит окна) и кладёт его
#     в metadata финального ответа;
#   * канал (postgres_channel) в фоновом цикле живого обновления читает
#     блок из моста (готовый либо собранный на лету) и пишет его в
#     processing-строку до финализации оборота.
#
# Ключ — session_key (например, ``postgres:chat_id``): разные сессии
# обрабатываются конкурентно как отдельные asyncio-задачи, без keying по
# сессии события разных чатов «перепутаются». Один проход оборота одной
# сессии выполняется последовательно (новая итерация не стартует, пока не
# окончена предыдущая), поэтому последняя запись перед ``_assemble_outbound``
# — это usage финальной итерации.
# ---------------------------------------------------------------------------
_CONTEXT_BRIDGE: dict[str, dict] = {}
_CONTEXT_BRIDGE_LOCK = threading.Lock()


def seed_context_window(
    session_key: str | None, *, limit: int = 0, model: str = ""
) -> None:
    """Засеять лимит окна/модель в мост на старте оборота.

    Вызывается из патча ``agent._state_build`` (RuntimePatcher.patch_
    context_bridge_seed): канал не знает лимит окна модели, поэтому на
    старте оборота кладём его в мост — дальше хук пишет usage каждой
    итерации, а канал собирает блок на лету (живое обновление).
    """
    if not session_key:
        return
    with _CONTEXT_BRIDGE_LOCK:
        entry = _CONTEXT_BRIDGE.setdefault(session_key, {})
        entry["limit"] = int(limit or 0)
        entry["model"] = model if isinstance(model, str) else ""


def _store_iteration_usage(session_key: str | None, usage: dict | None) -> None:
    """Записать по-итерационный usage оборота для сессии (неблокирующий)."""
    if not session_key:
        return
    with _CONTEXT_BRIDGE_LOCK:
        entry = _CONTEXT_BRIDGE.setdefault(session_key, {})
        entry["usage"] = dict(usage or {})
        entry["ts"] = time.time()


def _store_context_window(session_key: str | None, block: dict | None) -> None:
    """Записать готовый блок ``context_window`` для сессии (из патча)."""
    if not session_key or not block:
        return
    with _CONTEXT_BRIDGE_LOCK:
        entry = _CONTEXT_BRIDGE.setdefault(session_key, {})
        entry["block"] = dict(block)
        entry["ts"] = time.time()


def get_context_window(session_key: str | None) -> dict | None:
    """Вернуть блок ``context_window`` сессии (без удаления).

    Предпочитаем готовый блок, собранный патчем ``_assemble_outbound``
    (точный clamp с учётом реального лимита). До финализации оборота его
    ещё нет — собираем на лету из usage последней итерации и лимита,
    засеянного на старте оборота (``seed_context_window``).
    """
    if not session_key:
        return None
    with _CONTEXT_BRIDGE_LOCK:
        entry = dict(_CONTEXT_BRIDGE.get(session_key) or {})
    block = entry.get("block")
    if isinstance(block, dict):
        return dict(block)
    usage = entry.get("usage")
    limit = int(entry.get("limit") or 0)
    if not isinstance(usage, dict) or limit <= 0:
        return None
    raw_used = usage.get("prompt_tokens")
    try:
        used = int(raw_used or 0)
    except (TypeError, ValueError):
        return None
    if used <= 0:
        return None
    return {
        "used": used,
        "limit": limit,
        "pct": round(min(1.0, used / float(limit)), 4),
        "model": entry.get("model") or "",
    }


def get_iteration_usage(session_key: str | None) -> dict | None:
    """Прочитать по-итерационный usage сессии (без удаления)."""
    if not session_key:
        return None
    with _CONTEXT_BRIDGE_LOCK:
        entry = _CONTEXT_BRIDGE.get(session_key) or {}
        usage = entry.get("usage")
        return dict(usage) if isinstance(usage, dict) else None


def pop_context_bridge(session_key: str | None) -> None:
    """Снять с моста все данные сессии (финализация/ошибка оборота)."""
    if not session_key:
        return
    with _CONTEXT_BRIDGE_LOCK:
        _CONTEXT_BRIDGE.pop(session_key, None)


def make_db_logging_hook_factory(
    db_logging_service: Any,
    agent_id: str | None = None,
    print_llm_calls: bool = False,
) -> Callable[[Any], DatabaseLoggingHook]:
    """Фабрика: создать СВЕЖИЙ ``DatabaseLoggingHook`` на КАЖДЫЙ оборот.

    Передаётся в ``AgentLoop`` как ``hook_factories`` (``agent_factory.py``).
    Фреймворк вызывает её с ``AgentTurnHookContext``, в котором есть
    ``session_key`` текущего оборота. Фабрика резолвит ``request_id``
    вопроса из индекса сервиса и запекает оба поля в инстанс.

    Поскольку у каждого оборота СВОЙ инстанс, состояние вопроса
    (``_run_session_key``/``_request_id``) не разделяется между
    конкурентными сессиями — события не «путаются».

    Args:
        db_logging_service: ``DbLoggingService``.
        agent_id: id агента для колонки ``agent_id`` в логах.

    Returns:
        Фабрика ``def(turn_context) -> DatabaseLoggingHook``.
    """

    def _factory(turn_context: Any) -> DatabaseLoggingHook:
        session_key = getattr(turn_context, "session_key", None) or None
        request_id = None
        if session_key:
            request_id = db_logging_service.get_request_id(session_key)
        return DatabaseLoggingHook(
            db_logging_service,
            agent_id=agent_id,
            session_key=session_key,
            request_id=request_id,
            print_llm_calls=print_llm_calls,
        )

    return _factory


class DatabaseLoggingHook(BaseToolTrackingHook):
    """Агентский хук — пересылает tool- и run-события в DbLoggingService.

    Живёт в ``lib/hooks/``: это фреймворковый хук, а не плагин
    ``workspace/hooks/``. Он требует обязательный ``db_logging_service``
    в конструкторе, который ``hook_loader`` предоставить не может,
    поэтому в auto-scan ``workspace/hooks/`` не участвует (он и не
    сканируется — плагин-директория содержит только самодостаточные
    хуки с контрактом ``cls(workspace_dir=...)``).

    Создаётся per-turn через ``make_db_logging_hook_factory`` в
    ``AgentFactory`` или явно в ``RuntimePatcher.patch_subagent_logging``.

    Все методы НЕБЛОКИРУЮЩИЕ: ``DbLoggingService.log_*`` ставит события
    в очередь и возвращает ``True/False`` мгновенно.

    Конкурентность: фреймворковый ``AgentRunHookContext`` (для ``after_run``)
    не содержит ``session_key``, поэтому контекст вопроса раньше кэшировался
    в полях инстанса (``_run_session_key``/``_request_id``). При конкурентной
    обработке нескольких сессий одним общим инстансом поля перезаписывались
    чужим вопросом — события «путались».

    Теперь инстанс создаётся НА КАЖДЫЙ оборот через ``make_db_logging_hook_factory``
    и запекает свой ``session_key``/``request_id`` в конструкторе: состояние
    вопроса изолировано между сессиями, гонки нет.
    """

    def __init__(
        self,
        db_logging_service: Any,
        agent_id: str | None = None,
        *,
        session_key: str | None = None,
        request_id: str | None = None,
        print_llm_calls: bool = False,
    ) -> None:
        super().__init__()
        self._service = db_logging_service
        self._tool_start_times: dict[str, float] = {}
        self._agent_id = agent_id
        self._print_llm_calls = print_llm_calls
        # Контекст текущего оборота/вопроса. Запекается в фабрике на оборот,
        # чтобы ``after_run`` (у которого в контексте нет session_key) знал
        # свой вопрос. ``_capture_context`` дополнительно перечитывает
        # request_id по session_key из индекса сервиса — самокорректно.
        self._run_session_key = session_key
        self._request_id = request_id
        # Снимок промпта текущей итерации (полный messages), чтобы
        # ``after_iteration`` мог упаковать его вместе с ответом в llm_call.
        self._pending_prompt: list | None = None
        self._pending_iteration: int | None = None

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

    def _ctx(self, key: str) -> str | None:
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
        # Полный промпт (messages) этой итерации — снимок до ответа,
        # чтобы ``after_iteration`` упаковал его вместе с LLMResponse.
        self._pending_prompt = list(getattr(context, "messages", None) or [])
        self._pending_iteration = getattr(context, "iteration", None)

    async def after_iteration(self, context: Any) -> None:
        self._capture_context(context)
        # Свежий по-итерационный usage — мост для метрики занятости окна
        # (последняя запись перед финалом = usage финальной итерации).
        _store_iteration_usage(self._run_session_key, getattr(context, "usage", None))
        response = getattr(context, "response", None)
        if response is None:
            return
        try:
            from dataclasses import asdict

            self._service.log_llm_call(
                session_id=self._run_session_key or "",
                prompt=self._pending_prompt or [],
                response=asdict(response),
                iteration=self._pending_iteration or getattr(context, "iteration", None),
                model=getattr(response, "model", None),
                finish_reason=getattr(response, "finish_reason", None),
                usage=dict(getattr(context, "usage", None) or {}),
                request_id=self._request_id,
            )
        except Exception as exc:
            logger.warning("DbLoggingHook.after_iteration llm_call failed: %s", exc)
        if self._print_llm_calls:
            self._print_llm_tokens(context)

    def _print_llm_tokens(self, context: Any) -> None:
        """Вывести в терминал две строки о токенах итерации (CLI-режим)."""
        usage = dict(getattr(context, "usage", None) or {})
        if not usage:
            return
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        if prompt is None and completion is None:
            return
        if prompt is not None:
            console.print(f"[dim]→ LLM: отправлен промпт ({prompt} токенов)[/dim]")
        if completion is not None:
            console.print(f"[dim]← LLM: получен ответ ({completion} токенов)[/dim]")

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
    session_key: str | None = None,
    request_id: str | None = None,
):
    """Сформировать LogEvent из AgentRunHookContext (без жёсткой связки)."""
    from lib.services.db_logging_service import LogEvent

    final = context.final_content or ""
    tools = context.tools_used or []
    payload: dict[str, Any] = {
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
