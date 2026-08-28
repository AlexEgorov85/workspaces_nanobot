"""AgentFactory — создание AgentLoop с хуками.

Подключает к ``AgentLoop`` обязательные и опциональные хуки:

  * ``ToolAuditHook`` (всегда) — собирает вызовы инструментов за один
    оборот агента. Данные читаются из ``hook.drain()`` и внедряются
    в ``OutboundMessage.metadata["_tool_audit"]`` (см. ``RuntimePatcher.
    patch_assemble_outbound``). Каналы и CLI рендерят их в UI.

  * ``TerminalToolPrintHook`` (если модуль импортируется) — живой
    терминальный вывод результатов ``tool_call`` (успех/ошибка +
    длительность). Регистрируется как обычный инстанс, потому что
    не хранит состояние, критичное к изоляции между сессиями
    (метрики по session_key используются только как bucket для
    ``_starts``). Отключается через ``gateway.print_tools=false``.

  * ``DatabaseLoggingHook`` (если передан ``db_logging_service``) —
    НЕ регистрируется как общий инстанс. Вместо этого в ``hook_factories``
    передаётся ``make_db_logging_hook_factory``: фреймворк создаёт СВЕЖИЙ
    ``DatabaseLoggingHook`` на КАЖДЫЙ оборот, запекая его session_key/
    request_id. Это делает логирование конкурентно-безопасным (разные
    вопросы не «путают» события) — см. ``lib/hooks/
    database_logging_hook.py``.

Семантический патч ``_assemble_outbound`` применяется ``RuntimePatcher``
после ``create()`` (т.е. на этапе ``ApplicationContext.create`` /
``start``). ``AgentFactory`` НЕ делает monkey-patch'ей — только
регистрирует хуки в ``AgentLoop.from_config(hooks=[...])`` и
фабрики оборота в ``hook_factories=[...]``.

Проектные хуки из ``workspace/hooks/*.py`` (например,
``SessionFileRedirectHook``) подмешивает сам ``ApplicationContext``: он
сканирует их через ``lib.cli.hook_loader.scan_and_register`` и передаёт
в ``AgentFactory.create(project_hooks=...)``. Фабрика складывает их
перед ``ToolAuditHook`` и создаёт ``AgentLoop`` ОДИН раз
(``AgentLoop.from_config(hooks=merged, hook_factories=factory_list)``) —
без повторной пересборки в ``ApplicationContext``.

Создаёт ли AgentFactory CronService? Нет — он приходит извне готовым.
Обычно ``ApplicationContext._make_cron_service()`` создаёт ``CronService``
с путём ``workspace/cron/jobs.json`` и передаёт в ``create(...)``.
"""

from __future__ import annotations

from typing import Any


class AgentFactory:
    """Фабрика AgentLoop с консистентно настроенными хуками.

    Управляет только составом ``hooks=`` и ``hook_factories=`` в
    ``AgentLoop.from_config``. Дополнительные параметры (``session_manager``,
    ``cron_service``) пробрасываются как ``**kwargs`` в ``from_config``.

    Пример::

        agent, hooks, hook_factories = AgentFactory().create(
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
        session_manager: Any | None = None,
        cron_service: Any | None = None,
        db_logging_service: Any | None = None,
        agent_id: str | None = None,
        project_hooks: list[Any] | None = None,
        print_llm_calls: bool = False,
    ) -> tuple[Any, list[Any], list[Any]]:
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
            project_hooks: плагины из ``workspace/hooks/`` (после auto-scan).
                ``None``/``[]`` — только фреймворковые хуки.

        Returns:
            ``(agent, hooks, hook_factories)``:

              * ``agent`` — созданный ``AgentLoop``;
              * ``hooks`` — общие хуки, переданные в ``AgentLoop.hooks=``.
                Порядок: ``project_hooks`` (если есть) → ``ToolAuditHook``
                (чтобы правки плагинов ``params["path"]`` были видны в аудите);
              * ``hook_factories`` — список per-turn фабрик, переданный в
                ``AgentLoop.hook_factories=`` (для ``DatabaseLoggingHook``
                или ``None``).

            ``DatabaseLoggingHook`` в ``hooks`` НЕ попадает — он создаётся
            per-turn через ``hook_factories``.

            ``AgentLoop`` создаётся РОВНО ОДИН раз (полные хуки известны
            заранее): старый двушаговый ``AgentFactory.create`` → пересборка
            в ``ApplicationContext`` создавал агента дважды (двойной лог
            ``Registered N tools`` при старте).
        """
        from nanobot.agent.loop import AgentLoop

        hooks: list[Any] = []
        # ToolAuditHook — обязателен: каналы и CLI рендерят его записи
        # в UI ("✓ read(x.txt) → content" / "✗ exec: timeout").
        tool_audit_hook = self._import_tool_audit_hook()()
        hooks.append(tool_audit_hook)

        # TerminalToolPrintHook — опционален: живой вывод результатов
        # tool-вызовов в терминал (отдельный канал ``tools`` в loguru).
        # Подключается ПОСЛЕ ToolAuditHook, потому что использует
        # те же ``tool_events``/``tool_results``/``tool_calls``,
        # которые ToolAuditHook заполняет в ``before_execute_tools`` /
        # ``after_iteration``. Импорт через try/except — модуль
        # может отсутствовать в форке nanobot.
        terminal_print_hook = self._import_terminal_tool_print_hook()
        if terminal_print_hook is not None:
            hooks.append(terminal_print_hook())

        # Плагины workspace/hooks/ идут ПЕРЕД ToolAuditHook, чтобы их
        # правки ``params["path"]`` уже были видны в аудите.
        if project_hooks:
            hooks = list(project_hooks) + hooks

        # DatabaseLoggingHook — опционален: регистрируется НЕ как общий
        # инстанс, а как фабрика оборота (per-turn инстансы). Это
        # изолирует состояние вопроса между конкурентными сессиями.
        # Если workspace.hooks недоступен (например, в тестах) —
        # пропускаем без ошибки.
        hook_factories: list[Any] = []
        if db_logging_service is not None:
            factory = self._build_database_logging_factory(
                db_logging_service, agent_id, print_llm_calls=print_llm_calls
            )
            if factory is not None:
                hook_factories.append(factory)

        kwargs: dict = {
            "session_manager": session_manager,
            "hooks": hooks,
            "hook_factories": hook_factories,
        }
        if cron_service is not None:
            kwargs["cron_service"] = cron_service

        agent = AgentLoop.from_config(config, bus, **kwargs)
        return agent, hooks, hook_factories

    @staticmethod
    def _import_tool_audit_hook():
        """Ленивый импорт ``ToolAuditHook`` из ``lib/hooks/``.

        ``ToolAuditHook`` — фреймворковый хук (живёт в ``lib/hooks/``,
        а не в плагин-директории ``workspace/hooks/``). Импортируем
        лениво, чтобы ``lib.core.agent_factory`` не зависел от наличия
        nanobot на ``sys.path`` во время старта Python.
        """
        from lib.hooks.tool_audit_hook import ToolAuditHook

        return ToolAuditHook

    @staticmethod
    def _import_terminal_tool_print_hook():
        """Ленивый импорт ``TerminalToolPrintHook`` из ``lib/hooks/``.

        Возвращает класс хука или ``None``, если модуль отсутствует
        или не импортируется (например, в минимальном окружении
        тестов, где ``loguru.bind`` ещё не настроен, или если хук
        не зарегистрирован в ``lib/hooks/``).

        В отличие от ``_import_tool_audit_hook``, этот хук **опционален** —
        если он не подключён, терминал просто не печатает tool-результаты,
        но всё остальное работает.
        """
        try:
            from lib.hooks.terminal_tool_print_hook import TerminalToolPrintHook
        except Exception:
            return None
        return TerminalToolPrintHook

    @staticmethod
    def _build_database_logging_factory(
        db_logging_service: Any,
        agent_id: str | None = None,
        print_llm_calls: bool = False,
    ) -> Any | None:
        """Создать фабрику оборота ``DatabaseLoggingHook``.

        Импорт через try/except, чтобы:
          * ``AgentFactory`` не зависел жёстко от
            ``lib.hooks.database_logging_hook`` (этот модуль
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
            from lib.hooks.database_logging_hook import (
                make_db_logging_hook_factory,
            )
        except Exception:
            return None
        return make_db_logging_hook_factory(
            db_logging_service, agent_id, print_llm_calls=print_llm_calls
        )
