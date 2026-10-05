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
    ``_starts``). Флага отключения нет: ключ ``gateway.print_tools`` в
    ``config.json`` не читается нигде, см. докстринг
    ``lib/hooks/terminal_tool_print_hook.py``.

  * ``DatabaseLoggingHook`` (если передан ``db_logging_service``) —
    НЕ регистрируется как общий инстанс. Вместо этого в ``hook_factories``
    передаётся ``make_db_logging_hook_factory``: фреймворк создаёт СВЕЖИЙ
    ``DatabaseLoggingHook`` на КАЖДЫЙ оборот, запекая его session_key/
    request_id. Это делает логирование конкурентно-безопасным (разные
    вопросы не «путают» события) — см. ``lib/hooks/
    database_logging_hook.py``.

  * ``McpIdentityHook`` — подставляет личность оборота (session_id,
    user_id, request_id) в аргументы вызовов операций платформы
    ``mcp_enterprise_*``, объявленных в ``config.json →
    tools.mcpServers``. Регистрируется общим инстансом: состояния не
    хранит, личность читается из контекста конкретного вызова.

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

from loguru import logger


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
        settings: Any = None,
        project_hooks: list[Any] | None = None,
        framework_hooks: list[Any] | None = None,
        print_llm_calls: bool = False,
        usage_store: Any | None = None,
        tool_registry: Any | None = None,
    ) -> tuple[Any, list[Any], list[Any]]:
        """Создать AgentLoop с подключёнными хуками.

        Args:
            config: runtime-конфиг nanobot (объект с ``.agents.defaults``,
                ``.providers``, ``.channels``, ``.tools``, ``.workspace_path``).
            bus: ``MessageBus`` (см. ``nanobot.bus.queue``) — шина inbound/outbound.
            session_manager: менеджер сессий — класс библиотеки
                ``nanobot.session.manager.SessionManager`` (у нас поверх
                ``SanitizingSessionStore``). ``None`` — AgentLoop создаст
                дефолтный JSONL-менеджер.
            cron_service: ``CronService`` (опционально) — нужен CLI-режиму,
                в gateway не подключается.
            db_logging_service: ``DbLoggingService`` (опционально) — если
                передан, ``AgentLoop`` получает фабрику оборота для
                ``DatabaseLoggingHook`` (per-turn инстансы, конкурентно-безопасно).
            agent_id: id агента для колонки ``agent_id`` в логах.
            settings: merged ``SETTINGS`` — читается
                ``gateway.error_messages.*`` для fallback'а на internal-ошибку.
                ``None`` — дефолтный текст и ``log_to_db=True``.
            project_hooks: плагины из ``workspace/hooks/`` (после auto-scan).
                ``None``/``[]`` — только фреймворковые хуки.
            framework_hooks: готовые инстансы дополнительных фреймворковых
                хуков, которые ``ApplicationContext`` уже собрал, потому что
                для них нужна конфигурация. ``None``/``[]`` — ничего не
                добавлять. Класс НЕ импортируется здесь: фабрика управляет
                только составом списка, а не тем, откуда хук пришёл.
                Сейчас список пуст: ``ToolResultArchiveHook`` убран в пользу
                upstream ``maybe_persist_tool_result`` (change
                ``use-upstream-tool-result-persist``), а остальные
                фреймворковые хуки собирает сама фабрика.

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
        from nanobot.agent.tools.registry import ToolRegistry

        from lib.services.turn_delivery_factory import build_turn_delivery_factory

        hooks: list[Any] = []
        # ToolAuditHook — обязателен: каналы и CLI рендерят его записи
        # в UI ("✓ read(x.txt) → content" / "✗ exec: timeout"). Ему же
        # передаётся служба журнала: отказ, возникший на проводе ДО входа в
        # конвейер платформы, не оставляет в журнале ни одной строки, и
        # записать его может только сторона, которая этот отказ видит, — агент.
        tool_audit_hook = self._import_tool_audit_hook()(
            db_logging_service=db_logging_service
        )
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

        # RepeatGuardHook — защитник от повторных tool-вызовов
        # (``gateway.repeat_guard.*``). Регистрируется ВСЕГДА, даже при
        # ``mode="off"``: тогда его каноническая запись в
        # ``canonical_framework_hooks()`` совпадает с фактом, и
        # ``tools/diagnose_startup.py`` не сообщает о ложном drift'е.
        # В режиме ``off`` хук делает один флаг-чек и выходит.
        repeat_guard_settings = self._read_repeat_guard_settings(settings)
        repeat_guard_cls = self._import_repeat_guard_hook()
        if repeat_guard_cls is not None:
            hooks.append(
                repeat_guard_cls(
                    settings=repeat_guard_settings,
                    db_logging_service=db_logging_service,
                )
            )

        # McpIdentityHook — подставляет личность оборота в аргументы вызовов
        # операций платформы (``mcp_enterprise_*``). Идёт последним из
        # фреймворковых: ``ToolAuditHook`` читает аргументы раньше, и в UI
        # аудита видны Intent'ы модели, а не инфраструктурные ключи, которые
        # хук добавил под них. Состояния не хранит (личность читается из
        # контекста вызова), поэтому общий инстанс обслуживает все обороты.
        mcp_identity_cls = self._import_mcp_identity_hook()
        if mcp_identity_cls is not None:
            hooks.append(mcp_identity_cls(db_logging_service=db_logging_service))

        # Плагины workspace/hooks/ идут ПЕРЕД ToolAuditHook, чтобы их
        # правки ``params["path"]`` уже были видны в аудите.
        if project_hooks:
            hooks = list(project_hooks) + hooks

        # Дополнительные фреймворковые хуки, собранные вызывающим кодом
        # (инстансы уже созданы). Идут последними: хук, который читает
        # результат tool'а, держит ``after_execute_tool`` — ему важно
        # увидеть то, что вернул runner, а не то, что подготовили плагины.
        if framework_hooks:
            hooks.extend(framework_hooks)

        # DatabaseLoggingHook — опционален: регистрируется НЕ как общий
        # инстанс, а как фабрика оборота (per-turn инстансы). Это
        # изолирует состояние вопроса между конкурентными сессиями.
        # Если workspace.hooks недоступен (например, в тестах) —
        # пропускаем без ошибки.
        hook_factories: list[Any] = []
        if db_logging_service is not None:
            # ``get_model`` — closure для резолва текущего имени модели
            # в nanobot 0.3.5+ (где ``LLMResponse.model`` удалён). На
            # момент регистрации фабрики ``agent`` ещё не существует;
            # кидаем изменяемый контейнер ``_agent_box``, который
            # ``AgentLoop.from_config`` заполнит ссылкой. ``get_model``
            # читается лениво на каждой итерации — после ``from_config``
            # ``_agent_box[0]`` уже содержит ``agent``, свойство
            # ``AgentLoop.model`` (``nanobot/agent/loop.py:218``) отдаёт
            # текущее значение runtime_resolver.runtime.model.
            _agent_box: list[Any] = []

            def get_model() -> str | None:
                if not _agent_box:
                    return None
                try:
                    return getattr(_agent_box[0], "model", None)
                except Exception:
                    return None

            factory = self._build_database_logging_factory(
                db_logging_service, agent_id,
                print_llm_calls=print_llm_calls,
                get_model=get_model,
            )
            if factory is not None:
                hook_factories.append(factory)

            # ``_populate_box`` вызывается ПОСЛЕ ``from_config``, чтобы
            # closure увидел agent. Если фабрика не собралась — бокс
            # заполнять незачем: ``get_model`` тогда никто не читает
            # (прежняя ветка с no-op лямбдой того же эффекта не давала).
            # Ни ``_agent_box``, ни ``factory`` ниже не переприсваиваются,
            # поэтому closure читает актуальное значение — def здесь
            # эквивалентен прежним лямбдам.
            def _populate_box(built: Any) -> None:
                if factory is not None:
                    _agent_box.append(built)

        kwargs: dict = {
            "session_manager": session_manager,
            "hooks": hooks,
            "hook_factories": hook_factories,
            "tool_registry": (
                tool_registry if tool_registry is not None else ToolRegistry()
            ),
        }
        # Fallback на internal-ошибку — через публичную точку nanobot, а не
        # патчем (ADR turn-delivery-public-extension). Путь один: иначе путь,
        # забытый при сборке, снова покажет пользователю upstream-литерал.
        # ``None`` — upstream-модуль недоступен, тогда AgentLoop возьмёт
        # фабрику сам (build_turn_delivery_factory уже записал причину).
        turn_delivery_factory = build_turn_delivery_factory(
            bus,
            settings=settings,
            db_logging_service=db_logging_service,
            agent_id=agent_id,
        )
        if turn_delivery_factory is not None:
            kwargs["turn_delivery_factory"] = turn_delivery_factory
        if cron_service is not None:
            kwargs["cron_service"] = cron_service
        if usage_store is not None:
            kwargs["provider_snapshot_loader"] = self._wrap_provider_snapshot_loader(
                config, usage_store, bus
            )

        agent = AgentLoop.from_config(config, bus, **kwargs)
        # Backfill: теперь ``agent`` существует — закрыть closure.
        if db_logging_service is not None:
            _populate_box(agent)
        return agent, hooks, hook_factories

    @staticmethod
    def _wrap_provider_snapshot_loader(
        config: Any,
        usage_store: Any,
        bus: Any | None,
    ) -> Any:
        """Build a ``provider_snapshot_loader`` that attaches the LLM observer.

        ``AgentLoop`` создаёт провайдер внутри ``from_config(...)`` и наружу
        его не отдаёт, поэтому единственная точка подключения observer'а —
        обёртка над загрузчиком snapshot'а. На каждом вызове она просит
        snapshot и подписывает провайдера двумя observer'ами:

          * ``set_llm_call_observer(store.record)`` — учёт вызовов LLM;
          * ``set_fallback_model_observer(bus)`` — семантика смены модели,
            только для ``FallbackProvider``.

        Это те же строки, что делает библиотека при штатном запуске
        gateway (``nanobot/cli/gateway_runtime.py::_observe_provider``), но
        у нас observer инъецируется, а store опционален.

        Fail-soft: ошибка подписки логируется и не мешает агенту — учёт
        usage это observability, а не бизнес-критичный путь.

        Falls back to ``config.build_provider_snapshot`` when available;
        otherwise returns ``None`` and the upstream default is used.
        """
        base_loader = getattr(config, "build_provider_snapshot", None)
        if base_loader is None:
            return None

        def _wrapped(*, preset_name: str | None = None, **kwargs: Any) -> Any:
            snapshot = base_loader(preset_name=preset_name, **kwargs)
            if snapshot is None:
                return snapshot
            provider = getattr(snapshot, "provider", None)
            if provider is None:
                return snapshot
            if usage_store is not None:
                try:
                    provider.set_llm_call_observer(usage_store.record)
                except Exception as exc:
                    logger.warning(
                        "LLMUsageStore observer failed to attach: {}", exc
                    )
            if bus is not None:
                try:
                    from nanobot.providers.fallback_provider import FallbackProvider

                    if isinstance(provider, FallbackProvider):
                        provider.set_fallback_model_observer(bus)
                except Exception as exc:
                    logger.warning(
                        "Fallback model observer failed to attach: {}", exc
                    )
            return snapshot

        return _wrapped

    @staticmethod
    def _read_repeat_guard_settings(settings: Any) -> Any:
        """Достать ``gateway.repeat_guard`` из settings, терпимо к мусорам.

        Возвращает ``None``, когда секции нет (дефолт ``mode="off"``) или
        settings недоступны. Ошибки чтения проглатываются: отсутствие
        настройки не повод не стартовать.
        """
        try:
            gateway = getattr(settings, "gateway", None)
            if gateway is None:
                return None
            return getattr(gateway, "repeat_guard", None)
        except Exception:
            return None

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
    def _import_repeat_guard_hook():
        """Ленивый импорт ``RepeatGuardHook`` из ``lib/hooks/``.

        Опционален, как и предыдущий: отсутствие модуля не должно ломать
        старт — защитник от повторов удобен, но не обязателен.
        """
        try:
            from lib.hooks.repeat_guard_hook import RepeatGuardHook
        except Exception:
            return None
        return RepeatGuardHook

    @staticmethod
    def _import_mcp_identity_hook():
        """Ленивый импорт ``McpIdentityHook`` из ``lib/hooks/``.

        Опционален по той же причине, что и предыдущие: без него вызовы
        операций платформы просто не получат личность и будут отвергнуты
        сервером с ``identity_missing``, но старт агента не ломается.
        """
        try:
            from lib.hooks.mcp_identity_hook import McpIdentityHook
        except Exception:
            return None
        return McpIdentityHook

    @staticmethod
    def _build_database_logging_factory(
        db_logging_service: Any,
        agent_id: str | None = None,
        print_llm_calls: bool = False,
        get_model: Any = None,
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
            print_llm_calls: печатать в CLI токены каждой итерации.
            get_model: опциональный callable ``() -> str | None`` для
                получения текущего имени модели (nanobot 0.3.5+ —
                ``LLMResponse.model`` удалён, модель только на
                ``AgentLoop.model``). Передаётся в ``DatabaseLoggingHook``
                и вызывается в ``after_iteration``.

        Returns:
            ``make_db_logging_hook_factory(db_logging_service, agent_id, ...)``
            или ``None``, если модуль недоступен.
        """
        try:
            from lib.hooks.database_logging_hook import (
                make_db_logging_hook_factory,
            )
        except Exception:
            return None
        return make_db_logging_hook_factory(
            db_logging_service, agent_id,
            print_llm_calls=print_llm_calls,
            get_model=get_model,
        )
