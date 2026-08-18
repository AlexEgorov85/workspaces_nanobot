"""AgentFactory — создание AgentLoop с хуками.

Подключает к ``AgentLoop`` обязательные и опциональные хуки:

  * ``ToolAuditHook`` (всегда) — собирает вызовы инструментов за один
    оборот агента. Данные читаются из ``hook.drain()`` и внедряются
    в ``OutboundMessage.metadata["_tool_audit"]`` (см. ``RuntimePatcher.
    patch_assemble_outbound``). Каналы и CLI рендерят их в UI.

  * ``DatabaseLoggingHook`` (если передан ``db_logging_service``) —
    НЕ регистрируется как общий инстанс. Вместо этого в ``hook_factories``
    передаётся ``make_db_logging_hook_factory``: фреймворк создаёт СВЕЖИЙ
    ``DatabaseLoggingHook`` на КАЖДЫЙ оборот, запекая его session_key/
    request_id. Это делает логирование конкурентно-безопасным (разные
    вопросы не «путают» события) — см. ``workspace/hooks/
    database_logging_hook.py``.

Семантический патч ``_assemble_outbound`` применяет ``RuntimePatcher``
после ``create()`` (т.е. на этапе ``ApplicationContext.create`` /
``start``). ``AgentFactory`` НЕ делает monkey-patch'ей — только
регистрирует хуки в ``AgentLoop.from_config(hooks=[...])`` и
фабрики оборота в ``hook_factories=[...]``.

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
                передан, ``AgentLoop`` получает фабрику оборота для
                ``DatabaseLoggingHook`` (per-turn инстансы, конкурентно-безопасно).
            agent_id: id агента для колонки ``agent_id`` в логах.

        Returns:
            ``(agent, hooks)`` — где ``hooks`` это СПИСОК переданных в
            ``AgentLoop`` хуков (для тестов/диагностики). ``DatabaseLoggingHook``
            и ``AutoAttachHook`` здесь НЕТ (они создаются per-turn через
            ``hook_factories``); ``hooks`` содержит только ``ToolAuditHook``.
        """
        from nanobot.agent.loop import AgentLoop

        hooks: List[Any] = []
        # ToolAuditHook — обязателен: каналы и CLI рендерят его записи
        # в UI ("✓ read(x.txt) → content" / "✗ exec: timeout").
        tool_audit_hook = self._import_tool_audit_hook()()
        hooks.append(tool_audit_hook)

        # Per-turn фабрики хуков (см. ``hook_factories``).
        hook_factories: List[Any] = []

        # DatabaseLoggingHook — опционален: НЕ общий инстанс, а фабрика
        # per-turn (конкурентная изоляция состояния вопроса).
        if db_logging_service is not None:
            factory = self._build_database_logging_factory(db_logging_service, agent_id)
            if factory is not None:
                hook_factories.append(factory)

        # AutoAttachHook — per-turn: если бот создал файл через
        # write/edit/exec, но забыл вызвать ``message(content, media=[path])``,
        # этот хук автоматически прикрепит файл к финальному ответу
        # через ``RuntimePatcher.patch_assemble_outbound``. Импорт через
        # try/except, чтобы ``AgentFactory`` не зависел жёстко от
        # ``workspace/hooks/auto_attach_hook`` (в тестах без полного
        # workspace hook просто не создаётся).
        auto_attach_factory = self._build_auto_attach_factory()
        if auto_attach_factory is not None:
            hook_factories.append(auto_attach_factory)

        kwargs: dict = {
            "session_manager": session_manager,
            "hooks": hooks,
            "hook_factories": hook_factories,
        }
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
    def _build_database_logging_factory(
        db_logging_service: Any, agent_id: Optional[str] = None
    ) -> Optional[Any]:
        """Создать фабрику оборота ``DatabaseLoggingHook``.

        Импорт через try/except, чтобы:
          * ``AgentFactory`` не зависел жёстко от
            ``workspace.hooks.database_logging_hook`` (этот модуль
            импортирует ``nanobot.agent.AgentHook``, который нужен
            не всегда);
          * в тестах без полного окружения фабрика просто не создавалась.

        Args:
            db_logging_service: ``DbLoggingService``.
            agent_id: идентификатор агента (для колонки ``agent_id`` в логах).

        Returns:
            ``make_db_logging_hook_factory(db_logging_service, agent_id)``
            или ``None``, если модуль недоступен.
        """
        try:
            from workspace.hooks.database_logging_hook import (
                make_db_logging_hook_factory,
            )
        except Exception:
            return None
        return make_db_logging_hook_factory(db_logging_service, agent_id)

    @staticmethod
    def _build_auto_attach_factory() -> Optional[Any]:
        """Создать фабрику оборота ``AutoAttachHook``.

        Без жёсткой зависимости от ``workspace/hooks/auto_attach_hook``:
        если модуль недоступен (тесты без полного workspace) — фабрика
        не создаётся, и бот просто не получает auto-attach. Это безопасно:
        старый путь (через ``message(content, media=[path])``) по-прежнему
        работает.

        Returns:
            ``make_auto_attach_hook_factory()`` или ``None``.
        """
        try:
            from workspace.hooks.auto_attach_hook import (
                make_auto_attach_hook_factory,
            )
        except Exception:
            return None
        return make_auto_attach_hook_factory()
