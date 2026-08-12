"""AgentFactory — создание AgentLoop с хуками.

Подключает к ``AgentLoop`` обязательные и опциональные хуки:

  * ``ToolAuditHook`` (всегда) — собирает вызовы инструментов за один
    оборот агента. Данные читаются из ``hook.drain()`` и внедряются
    в ``OutboundMessage.metadata["_tool_audit"]`` (см. ``RuntimePatcher.
    patch_assemble_outbound``). Каналы и CLI рендерят их в UI.

  * ``DatabaseLoggingHook`` (если передан ``db_logging_service``) —
    ``AgentHook`` из ``workspace.hooks.database_logging_hook``,
    логирует tool-события и run-level summary в БД через
    ``DbLoggingService``.

Семантический патч ``_assemble_outbound`` применяет ``RuntimePatcher``
после ``create()`` (т.е. на этапе ``ApplicationContext.create`` /
``start``). ``AgentFactory`` НЕ делает monkey-patch'ей — только
регистрирует хуки в ``AgentLoop.from_config(hooks=[...])``.

Создаёт ли AgentFactory CronService? Нет — он приходит извне готовым.
Обычно ``ApplicationContext._make_cron_service()`` создаёт ``CronService``
с путём ``workspace/cron/jobs.json`` и передаёт в ``create(...)``.
"""

from __future__ import annotations

from typing import Any, List, Optional, Tuple


class AgentFactory:
    """Фабрика AgentLoop с консистентно настроенными хуками.

    Управляет только составом ``hooks=`` в ``AgentLoop.from_config``.
    Дополнительные параметры (``session_manager``, ``cron_service``)
    пробрасываются как ``**kwargs`` в ``from_config``.

    Пример::

        agent, hooks = AgentFactory().create(
            config, bus,
            session_manager=pg_session_manager,
            cron_service=cron,
            db_logging_service=db_logging,
        )
    """

    def create(
        self,
        config: Any,
        bus: Any,
        session_manager: Optional[Any] = None,
        cron_service: Optional[Any] = None,
        db_logging_service: Optional[Any] = None,
        agent_id: Optional[str] = None,
    ) -> Tuple[Any, List[Any]]:
        """Создать AgentLoop с подключёнными хуками.

        Args:
            config: runtime-конфиг nanobot (объект с ``.agents.defaults``,
                ``.providers``, ``.channels``, ``.tools``, ``.workspace_path``).
            bus: ``MessageBus`` (см. ``nanobot.bus.queue``) — шина inbound/outbound.
            session_manager: ``PGSessionManager`` или ``SessionManager``.
                ``None`` — AgentLoop создаст дефолтный JSONL-менеджер.
            cron_service: ``CronService`` (опционально) — нужен CLI-режиму,
                в gateway не подключается.
            db_logging_service: ``DbLoggingService`` (опционально) — если
                передан, добавляется ``DatabaseLoggingHook``.

        Returns:
            ``(agent, hooks)`` — где ``hooks`` это СПИСОК переданных в
            ``AgentLoop`` хуков (для тестов/диагностики).
        """
        from nanobot.agent.loop import AgentLoop

        hooks: List[Any] = []
        # ToolAuditHook — обязателен: каналы и CLI рендерят его записи
        # в UI ("✓ read(x.txt) → content" / "✗ exec: timeout").
        tool_audit_hook = self._import_tool_audit_hook()()
        hooks.append(tool_audit_hook)

        # DatabaseLoggingHook — опционален, регистрируется только если
        # реально передан db_logging_service. Если workspace.hooks
        # недоступен (например, в тестах) — пропускаем без ошибки.
        if db_logging_service is not None:
            db_hook = self._build_database_logging_hook(db_logging_service, agent_id)
            if db_hook is not None:
                hooks.append(db_hook)

        kwargs: dict = {"session_manager": session_manager, "hooks": hooks}
        if cron_service is not None:
            kwargs["cron_service"] = cron_service

        agent = AgentLoop.from_config(config, bus, **kwargs)
        return agent, hooks

    @staticmethod
    def _import_tool_audit_hook():
        """Ленивый импорт ``ToolAuditHook`` из ``workspace/hooks/``.

        ``ToolAuditHook`` живёт в ``workspace/hooks/tool_audit_hook.py``
        (вне ``lib/``), потому что это проектный код, а не библиотечный.
        Импортируем лениво, чтобы ``lib.core.agent_factory`` не зависел
        от наличия workspace на ``sys.path`` во время старта Python.
        """
        from hooks.tool_audit_hook import ToolAuditHook

        return ToolAuditHook

    @staticmethod
    def _build_database_logging_hook(
        db_logging_service: Any, agent_id: Optional[str] = None
    ) -> Optional[Any]:
        """Ленивое создание ``DatabaseLoggingHook``.

        Импорт через try/except, чтобы:
          * ``AgentFactory`` не зависел жёстко от
            ``workspace.hooks.database_logging_hook`` (этот модуль
            импортирует ``nanobot.agent.AgentHook``, который нужен
            не всегда);
          * в тестах без полного окружения хук просто не подключался.

        Args:
            agent_id: идентификатор агента (для колонки ``agent_id`` в логах).

        Returns:
            ``DatabaseLoggingHook(db_logging_service, agent_id)`` или ``None``,
            если модуль недоступен.
        """
        try:
            from workspace.hooks.database_logging_hook import DatabaseLoggingHook
        except Exception:
            return None
        return DatabaseLoggingHook(db_logging_service, agent_id)
