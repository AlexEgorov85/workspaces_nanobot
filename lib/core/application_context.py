"""ApplicationContext — единая точка создания и связывания сервисов.

Создаёт все общие сервисы (конфиг, БД-логирование, аудит-сервисы,
шина сообщений, хранилище сессий, агент) и публикует их атрибутами.
Точки входа (gateway.py / cli_agent.py) — тонкие оркестраторы,
использующие ``ctx`` для запуска/остановки.

Все тяжёлые зависимости (nanobot, psycopg2) импортируются лениво —
модуль безопасно импортировать даже в тестовых средах.

Composition contract
====================

``ApplicationContext.create()`` принимает ТОЛЬКО typed-параметры:

  * обязательный ``role: Literal["gateway", "cli"]``;
  * явные override-ключи ``storage_override``, ``session_override``;
  * ``**kwargs`` — временная compatibility boundary для deprecated
    ``enable_db_logging / enable_audit / enable_cron / print_llm_calls``
    (см. AGENTS.md § «Working Conventions → Configuration»).
    Любой другой ключ (включая ``profile``) отвергается ``TypeError``.

``profile`` НЕ является параметром ``create()`` — профиль выбирается
на границе запуска приложения (argv application entrypoint) и
публикуется через ``config._initialize_settings(profile=...)``.
Composition root читает его только из ``SETTINGS["profile"]``.

``role`` определяет только composition инфраструктуры
(``PostgresChannel``, ``CronService``).

Снимком (``cache.duckdb``) агент **не владеет** (фаза 5, п. 5.8): файл
открывает capability ``data`` платформы, и режим доступа к нему задаётся
там. Прокси и фабрики провайдера в агенте сняты вместе с обвязкой.
"""

from __future__ import annotations

import inspect
import logging
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)


# Deprecated kwargs, принимаются ТОЛЬКО через **kwargs до раскрытия
# change ``remove-deprecated-enable-kwargs``. Production code MUST NOT
# их использовать. См. openspec/changes/unify-cli-gateway-architecture
# design D1 «Staged implementation» и Stage G.
DEPRECATED_ENABLE_KWARGS = frozenset({
    "enable_db_logging",
    "enable_audit",
    "enable_cron",
    "print_llm_calls",
})


def _resolve_enable_kwargs(
    kwargs: dict[str, Any],
    *,
    gateway_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Извлечь deprecated ``enable_*``/``print_llm_calls`` из ``**kwargs``.

    Возвращает dict со всеми DEPRECATED_ENABLE_KWARGS (defaults из
    ``gateway.*`` settings). При передаче kwarg — поднимает
    ``DeprecationWarning`` (через ``warnings.warn`` с ``stacklevel=2``,
    чтобы указывать на caller'а, а не на эту функцию).

    ``DEPRECATED_ENABLE_KWARGS`` — allowlist: любой ключ, которого в нём
    нет, отвергается ``TypeError``. Это делает ``profile=`` (и опечатки
    вроде ``enable_aduit=``) явной ошибкой вместо молчаливого игнора.
    ``profile`` намеренно отсутствует: у него нет migration path в
    ``config.json`` — это не deprecated API, а состояние ``SETTINGS``,
    определённое ДО ``create()``.

    Production code MUST NOT передавать эти kwargs напрямую —
    использовать вместо этого ``gateway.enable_*`` в SETTINGS.
    """
    import warnings

    unknown = sorted(set(kwargs) - DEPRECATED_ENABLE_KWARGS)
    if unknown:
        raise TypeError(
            "ApplicationContext.create() got an unexpected keyword "
            f"argument(s): {', '.join(unknown)}. "
            f"Accepted deprecated kwargs: "
            f"{', '.join(sorted(DEPRECATED_ENABLE_KWARGS))}. "
            "Profile MUST be resolved before create() via "
            "config._initialize_settings(profile=...) and is read from "
            "SETTINGS['profile']."
        )

    gateways = gateway_settings or {}
    defaults = {
        "enable_db_logging": bool(gateways.get("enable_db_logging", True)),
        "enable_audit": bool(gateways.get("enable_audit", True)),
        "enable_cron": bool(gateways.get("enable_cron", False)),
        "print_llm_calls": bool(gateways.get("print_llm_calls", False)),
    }

    out = dict(defaults)
    for key, value in kwargs.items():
        if key in DEPRECATED_ENABLE_KWARGS:
            warnings.warn(
                f"ApplicationContext.create({key}={value!r}) is deprecated; "
                f"configure gateway.{key} in config.json instead. "
                "This compatibility boundary will be removed by change "
                "remove-deprecated-enable-kwargs.",
                DeprecationWarning,
                stacklevel=3,
            )
            out[key] = bool(value)
    return out


class ApplicationContext:
    """Контекст приложения: конфиг + все сервисы."""

    # Пути
    script_dir: Path
    workspace_dir: Path

    # Конфигурация
    config: Any
    settings: Any
    project_settings: Any = None

    # Шина
    bus: Any

    # Агент и его состояние
    agent: Any
    tool_audit_hook: Any
    hooks: list

    # Хранилище сессий
    session_manager: Any
    storage_mode: str

    # Сервисы (опциональные)
    db_logging_service: Any | None = None
    # Поля ``cache_loader`` / ``cache_provider`` / ``cache_store`` сняты в
    # фазе 5 (п. 5.8). Снимком владеет capability ``data`` платформы; второй
    # writer того же файла означал бы, что снимок читают не оттуда, откуда
    # его пишут.
    # Change ``drop-local-cache-read-from-pg``: поле ``ownership_coordinator``
    # удалено вместе со слоем владения. Навыки и CLI, присваивавшие
    # ``ctx.ownership_coordinator = None``, продолжают работать: dataclass
    # без ``__slots__`` допускает произвольные атрибуты экземпляра.
    # cache_store: Any | None = None  # DEPRECATED: слой владения удалён (drop-local-cache-read-from-pg)

    # Composition role (Stage A)
    role: str = ""  # "gateway" | "cli"
    enable_db_logging: bool = True
    enable_audit: bool = True
    enable_cron: bool = False
    print_llm_calls: bool = False

    # Storage-hybridization: cold-storage mirror для сессий + LLM usage.
    session_cold_sync_service: Any | None = None
    usage_store: Any | None = None

    # Клиент к MCP-серверу enterprise-mcp. Создаётся всегда, когда раздел
    # ``enterprise_mcp`` включён, но соединение ленивое: сервер не поднимается,
    # пока не понадобился, и не мешает старту агента, если платформа не собрана.
    enterprise_mcp: Any | None = None

    # Помощники
    config_service: Any = None
    runtime_patcher: Any = None
    runtime_health: Any = None
    runtime_readiness: Any = None
    session_storage_service: Any = None

    # Per-turn hook factories (для DatabaseLoggingHook и т.п.), которые
    # ``AgentFactory`` собрала из конфигурации и передала в
    # ``AgentLoop.from_config(hook_factories=...)``. Нужны для
    # пересборки ``AgentLoop`` после auto-scan проектных хуков.
    hook_factories: list = None  # type: ignore[assignment]

    # Lifecycle
    _started: bool = False
    _shutdown: Any | None = None  # ShutdownCoordinator
    runtime_events_subscriber: Any | None = None  # RuntimeEventsSubscriber

    @classmethod
    def create(
        cls,
        script_dir: Path,
        workspace_dir: Path,
        *,
        role: Literal["gateway", "cli"],
        storage_override: str | None = None,
        session_override: str | None = None,
        **kwargs: Any,
    ) -> ApplicationContext:
        """Собрать контекст приложения.

        Args:
            script_dir: корень проекта (где лежит config.json).
            workspace_dir: корень workspace.
            role: точка входа (``"gateway"`` или ``"cli"``). Определяет
                composition инфраструктуры (``PostgresChannel`` только в
                gateway, ``CronService`` только в gateway). НЕ определяет
                cache owner/reader — это ответственность
                ``CacheAccessMode``.
            storage_override: режим хранилища из CLI (auto/postgres/file).
            session_override: имя сессии (CLI).
            **kwargs: deprecated compatibility boundary для

                * ``enable_db_logging`` (bool);
                * ``enable_audit`` (bool);
                * ``enable_cron`` (bool);
                * ``print_llm_calls`` (bool).

                Принимаются с ``DeprecationWarning`` + применяются как
                override над ``SETTINGS["gateway"].*``. После раскрытия
                change ``remove-deprecated-enable-kwargs`` — ``TypeError``.

                ``profile`` НЕ принимается: профиль определён ДО вызова
                и читается из ``SETTINGS["profile"]``. Передача
                ``profile=`` приводит к ``TypeError``.

        Raises:
            ConfigurationError: если ``_initialize_settings(profile)`` ещё не
                выполнен (proxy остался uninitialized).
            TypeError: если в ``**kwargs`` передан ключ вне
                ``DEPRECATED_ENABLE_KWARGS`` (включая ``profile``).
        """
        # Делегируем ``**kwargs`` валидацию/применение (с DeprecationWarning).
        import config as _config
        ctx_settings = _config.SETTINGS
        # Touching ``["profile"]`` материализует ConfigurationError на
        # uninitialized proxy, но не делает duplicated work в happy-path.
        # Единственный канал получения профиля в composition root —
        # resolved SETTINGS; способ выбора профиля entrypoint'а здесь
        # неизвестен и не нужен.
        resolved_profile = ctx_settings["profile"]

        gateways = ctx_settings.get("gateway") or {}
        enable_kwargs = _resolve_enable_kwargs(
            kwargs, gateway_settings=gateways
        )

        ctx = cls()
        ctx.script_dir = Path(script_dir)
        ctx.workspace_dir = Path(workspace_dir)
        ctx.profile = resolved_profile
        ctx.role = role
        ctx.enable_db_logging = bool(enable_kwargs["enable_db_logging"])
        ctx.enable_audit = bool(enable_kwargs["enable_audit"])
        ctx.enable_cron = bool(enable_kwargs["enable_cron"])
        ctx.print_llm_calls = bool(enable_kwargs["print_llm_calls"])

        ctx.config_service = _make_config_service(
            ctx.script_dir, ctx.workspace_dir, settings_override=ctx_settings
        )
        ctx.config = ctx.config_service.load()
        ctx.settings = ctx_settings

        # 1a. Fail-fast валидация проектных настроек (типы/значения).
        from lib.core.project_settings import validate_project_settings

        ctx.project_settings = validate_project_settings(ctx.settings)

        # 2. Таймауты
        ctx.config_service.apply_timeouts(
            ctx.config,
            llm_timeout=ctx.config_service.get_int("gateway", "llm_timeout", default=300),
            exec_timeout=ctx.config_service.get_int("gateway", "exec_timeout", default=60),
            max_iterations=ctx.config_service.get_int("cli", "max_iterations", default=200),
        )

        # 3. SessionStorageService
        from lib.services.session_storage import SessionStorageService

        # Параметр session_manager_json удалён: теперь override из
        # session_manager.json применяется централизованно в
        # ConfigurationResolver (см. config.resolve_application_config,
        # шаг 2 порядка merge).
        ctx.session_storage_service = SessionStorageService()
        pg_section = ctx.config_service.settings_section("channels").get(
            "postgres", {}
        )

        # Конфигурация общего пула соединений (channels.postgres.pool) —
        # применяется ДО создания сервисов, чтобы воркеры пула использовали
        # заданные min_conn/max_conn/pool_timeout и т.п.
        if isinstance(pg_section, dict) and isinstance(pg_section.get("pool"), dict):
            _db_print = bool(
                ctx.config_service.settings_section("gateway").get(
                    "print_db_activity", False
                )
            )
            _configure_db_pool(
                pg_section.get("pool", {}), print_activity=_db_print
            )

        try:
            storage_mode, session_manager = ctx.session_storage_service.create(
                ctx.config,
                storage=storage_override
                or ctx.config_service.get_str("gateway", "storage", default="auto"),
                pg=pg_section,
                configure_db=True,
                return_file_manager=not ctx.enable_cron,
            )
        except Exception as exc:
            logger.warning("SessionStorageService failed: %s", exc)
            storage_mode, session_manager = "file", None

        ctx.storage_mode = storage_mode
        ctx.session_manager = session_manager

        # 4. DbLoggingService
        if ctx.enable_db_logging:
            ctx.db_logging_service = _make_db_logging(ctx)

        # 4a. LLMUsageStore (upstream observer storage).
        # См. спеку ``storage/usage-store``. Всегда создаётся —
        # фабрика вернёт ``None`` если конфиг отключён / nanobot
        # не предоставляет класс.
        ctx.usage_store = _make_usage_store(ctx)

        # 4b. SessionColdSyncService (cold-storage mirror).
        # Создаётся только при PG-конфиге. Sync стартует позже,
        # в ``start()`` lifecycle.
        ctx.session_cold_sync_service = _make_session_cold_sync_service(ctx)

        # 5. Реестр ресурсов удалён: писателей не осталось.
        #    Последним читателем был ``CacheLoadService``, ушедший на
        #    платформу 2026-10-01 вместе с кластером снимка. Состав снимка
        #    объявляет ``mcp-platform/platform.json`` — там же, где им
        #    владеют и читают.

        # 6. MessageBus + AgentFactory
        from lib.services.db_logging_bus import (
            make_inbound_logger,
            make_outbound_logger,
        )

        inbound_logger = None
        outbound_logger = None
        # Идентификатор агента — для колонки agent_id в логах
        # (подагенты получают parent_agent_id = этот id).
        agent_id = _resolve_agent_id(ctx.config)
        if ctx.db_logging_service is not None:
            inbound_logger = make_inbound_logger(ctx.db_logging_service, agent_id)
            outbound_logger = make_outbound_logger(ctx.db_logging_service, agent_id)

        ctx.bus = _create_bus(inbound_logger, outbound_logger)

        from lib.core.agent_factory import AgentFactory

        # CronService — ТОЛЬКО для role="gateway". При role="cli" значение
        # ``gateway.enable_cron`` MUST быть проигнорировано (см.
        # openspec/changes/unify-cli-gateway-architecture design D7 —
        # «Cron = gateway-only»). Решает проблему «два процесса выполняют
        # один jobs.json дважды».
        cron_service = None
        if ctx.enable_cron and ctx.role == "gateway":
            cron_service = _make_cron_service(ctx.config)

        # 6a. Auto-scan проектных хуков из ``workspace/hooks/*.py`` (ПЛАГИНЫ).
        # Фреймворковые хуки (``ToolAudit``, ``DatabaseLogging`` — живут
        # в ``lib/hooks/``) провязывает ``AgentFactory``; плагины
        # (например, ``SessionFileRedirectHook``, ``RecentFilesHook``)
        # сканируются здесь единым механизмом для всех точек входа
        # (gateway, cli_agent). Сканирование идёт ДО создания
        # ``AgentLoop``, чтобы агент создавался ровно один раз с полным
        # списком хуков (иначе был двойной лог ``Registered N tools``).
        # Если папки ``hooks/`` нет или она пуста (например, в юнит-тестах) —
        # пропускаем без ошибки.
        project_hooks: list = []
        try:
            from lib.cli.hook_loader import scan_and_register

            project_hooks = scan_and_register(
                ctx.workspace_dir / "hooks", ctx.workspace_dir
            )
        except Exception as exc:
            logger.warning("hook_loader.scan_and_register failed: %s", exc)

        agent_factory = AgentFactory()
        ctx.agent, ctx.hooks, ctx.hook_factories = agent_factory.create(
            ctx.config,
            ctx.bus,
            session_manager=ctx.session_manager,
            cron_service=cron_service,
            db_logging_service=ctx.db_logging_service,
            agent_id=agent_id,
            settings=ctx.settings,
            project_hooks=project_hooks or None,
            print_llm_calls=ctx.print_llm_calls,
            usage_store=ctx.usage_store,
        )

        # ToolAuditHook — фреймворковый, входит в ``ctx.hooks`` последним
        # (после плагинов). Нужен RuntimePatcher'у для внедрения аудита.
        ctx.tool_audit_hook = next(
            (h for h in ctx.hooks if type(h).__name__ == "ToolAuditHook"),
            None,
        )

        # Единственная точка вывода полного списка подключённых хуков:
        # плагины + фреймворковые (ToolAuditHook) + per-turn factories
        # (DatabaseLoggingHook). Печатается один раз — двойных сообщений
        # нет (сканер успех молчит).
        _log_connected_hooks(ctx)

        # 6b. RuntimeHealth / RuntimeReadiness — operational status.
        # Health: пульс процесса (liveness). Readiness: PG/duckdb/vector.
        # Регистрируется ПОСЛЕ хуков и сборки шины, потому что readiness
        # проверяет состояние уже созданных сервисов.
        from lib.services.runtime_health import (
            RuntimeHealth,
            RuntimeReadiness,
        )

        ctx.runtime_health = RuntimeHealth()
        ctx.runtime_readiness = RuntimeReadiness()
        _register_readiness_checks(ctx)

        # 7. RuntimePatcher
        from lib.services.runtime_patcher import RuntimePatcher

        # Найти RecentFilesHook среди зарегистрированных хуков (если
        # был подключён через auto-scan). Используется RuntimePatcher'ом
        # для auto-attach созданных файлов в ``OutboundMessage.media``.
        recent_files_hook = None
        for h in ctx.hooks:
            cls_name = type(h).__name__
            if cls_name == "RecentFilesHook":
                recent_files_hook = h
                break

        ctx.runtime_patcher = RuntimePatcher()
        patch_report = ctx.runtime_patcher.apply_all(
            ctx.config, ctx.settings, ctx.workspace_dir,
            ctx.agent, ctx.tool_audit_hook,
            recent_files_hook=recent_files_hook,
            db_logging_service=ctx.db_logging_service,
            session_manager=ctx.session_manager,
        )
        ctx.runtime_patch_report = patch_report
        # Баннер идёт в stdout тем же путём, что и «Hooks connected», а не
        # через ``logger.info``: эффективный уровень логгера
        # ``lib.core.application_context`` — WARNING, поэтому INFO там
        # отбрасывался молча, и ``tools/diagnose_startup.py`` сообщал
        # «CRITICAL MISSING REQUIRED» по патчам на здоровом старте.
        _print_startup_block(patch_report.render(specs=RuntimePatcher.patch_specs()))
        # Пустая строка отделяет блок от статусных строк старта: иначе
        # читатель лога (в том числе diagnose_startup) не может отличить
        # «конец блока» от «следующая строка вывода».
        _print_startup_block("")
        if patch_report.failed:
            logger.warning(
                "%d runtime patch(es) failed: %s",
                len(patch_report.failed),
                [name for name, _ in patch_report.failed],
            )
        _emit_patch_inventory_banner(patch_report)

        # 7a. Project tools registration — независимый stage composition
        # root'а (см. openspec/changes/runtime-patcher-composition-cleanup,
        # design Decision 3). ``register_project_tools`` НЕ вызывается из
        # ``RuntimePatcher.apply_all()`` — это отдельный вызов, source
        # of truth для ``_emit_project_tools_inventory_banner``.
        from lib.services.project_tool_loader import register_project_tools

        ctx.enterprise_mcp = _make_enterprise_mcp(ctx.settings, ctx)
        if ctx.enterprise_mcp is not None:
            logger.info(
                "enterprise-mcp: клиент создан (%s), соединение ленивое",
                ctx.enterprise_mcp.describe(),
            )

        project_tools_result = register_project_tools(
            agent=ctx.agent,
            workspace_dir=ctx.workspace_dir,
            settings=ctx.settings,
            db_logging_service=ctx.db_logging_service,
            enterprise_mcp=ctx.enterprise_mcp,
        )
        ctx.project_tools_result = project_tools_result
        _emit_project_tools_inventory_banner(project_tools_result)

        # 8. Помощники
        ctx.runtime_health.mark_started()
        return ctx

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def attach_log_transport(self) -> None:
        """Перевести запись журнала на ``enterprise-mcp`` (change
        ``enterprise-mcp-platform``, фаза 7).

        Зовётся **дважды**, и это не дублирование:

        1. Из ``start()`` — вне event loop. Живой loop и сессия MCP на этом
           шаге ещё не существуют, мост ``LoopCallRunner`` построить не на чем,
           поэтому подключается только локальный след, а сервис помечается
           ``transport_pending``: писать напрямую в этот промежуток нельзя,
           иначе возврат пула записи в руки агента случился бы молча.
        2. Из живого loop (``gateway._run``, ``cli_agent``) — сразу после
           подъёма сессии ``enterprise-mcp``. Вот здесь и появляется writer,
           и с этого момента журнал идёт операцией ``log_events``.

        Второй вызов отключает первое состояние, поэтому счётчики не
        учитываются дважды.

        Локальный fallback подключается всегда, независимо от клиента: он
        нужен именно тогда, когда писать некуда.
        """
        service = self.db_logging_service
        if service is None:
            return
        from lib.services.log_transport import LocalFallbackSink

        data_dir = getattr(self, "data_dir", None) or getattr(
            self, "workspace_dir", None
        )
        try:
            # Рядом с остальным состоянием оборота, а не рядом с workspace:
            # workspace читает человек, ``data_store`` - runtime.
            fallback = LocalFallbackSink(
                str(
                    Path(data_dir or ".")
                    / "data_store"
                    / "logs"
                    / "gateway-events-fallback.jsonl"
                )
            )
        except Exception as exc:  # noqa: BLE001 - лог не должен ронять старт
            logger.warning("local fallback для журнала не создан: %s", exc)
            fallback = None

        client = getattr(self, "enterprise_mcp", None)
        if client is None:
            # Раздел выключен по решению оператора: писать напрямую —
            # законное состояние, а не незавершённая миграция.
            if fallback is not None:
                service.attach_transport(
                    mcp_writer=None, fallback_sink=fallback,
                    transport_pending=False,
                )
            logger.info(
                "enterprise-mcp не объявлен: журнал пишется напрямую в БД, "
                "локальный fallback подключён"
            )
            return

        try:
            import asyncio

            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            # Живой loop ещё не поднят. Это НЕ «писать напрямую»: решение
            # о транспорте не принято, и принять его предстоит в том же
            # цикле, где поднимается сессия enterprise-mcp.
            if fallback is not None:
                service.attach_transport(
                    mcp_writer=None, fallback_sink=fallback,
                    transport_pending=True,
                )
            logger.warning(
                "нет живого event loop: транспорт журнала не выбран, запись "
                "ждёт подключения в цикле (log_events пока не используется)"
            )
            return

        from lib.services.log_transport import LoopCallRunner, McpLogWriter

        try:
            writer = McpLogWriter(call=client.call, run=LoopCallRunner(loop=loop))
            service.attach_transport(
                mcp_writer=writer, fallback_sink=fallback,
                transport_pending=False,
            )
        except Exception as exc:  # noqa: BLE001 - лог не должен ронять старт
            logger.warning("транспорт журнала через MCP не подключён: %s", exc)
            if fallback is not None:
                service.attach_transport(
                    mcp_writer=None, fallback_sink=fallback,
                    transport_pending=False,
                )
            return
        logger.info("журнал агента пишется через enterprise-mcp (log_events)")

    def start(self) -> None:
        """Запустить фоновые сервисы (БД-логирование, аудит)."""
        if self._started:
            return
        from lib.lifecycle.shutdown_coordinator import ShutdownCoordinator

        self._shutdown = ShutdownCoordinator()
        if self.runtime_health is not None:
            self.runtime_health.mark_started()

        # Переопределения системных шаблонов nanobot из workspace/overrides/
        # (например, русская инструкция Consolidator). Безопасно-идемпотентно;
        # при отсутствии каталога молча пропускается.
        try:
            from lib.services.consolidator_locale import apply_template_overrides

            if apply_template_overrides():
                logger.info("Template overrides active: workspace/overrides")
        except Exception as exc:
            logger.warning("Template overrides not applied: %s", exc)

        # Стартуем общий пул соединений (воркеры подключаются лениво при
        # первой задаче, но пул уже создан и подхватил pool-конфиг).
        _start_db_pool()

        # Pre-startup проверка наличия обязательных runtime-таблиц
        # (5 имён из SETTINGS["channels"]["postgres"] +
        # SETTINGS["logging"]["db"]). При отсутствии любой — выброс
        # SchemaValidationError (наследник ConfigurationError), который
        # ловится в gateway.main() / cli_agent.main() → exit 2 + stderr.
        # Без этой проверки gateway стартует, а сервисы падают уже
        # на первой INSERT/SELECT в несуществующие таблицы.
        # См. openspec/specs/runtime/startup-schema-validation.
        self._validate_runtime_schema()

        # Подписчик на runtime-события nanobot 0.3.5.
        # Регистрируется ПОСЛЕ apply_all (если он активен) и ДО старта каналов,
        # чтобы seed лимита окна/модели + метрики оборота были доступны
        # для первого inbound-сообщения. Lifecycle:
        # start() здесь → каналы стартуют → stop() в _stop_runtime_events_subscriber
        # ДО MessageBus.drain() в shutdown-последовательности.
        # См. openspec/changes/runtime-events-subscription.
        try:
            from lib.services.runtime_events_subscriber import (
                RuntimeEventsSubscriber,
            )
            self.runtime_events_subscriber = RuntimeEventsSubscriber(
                self.bus,
                db_logging_service=self.db_logging_service,
            )
            self.runtime_events_subscriber.start()
            if self._shutdown is not None:
                self._shutdown.register(
                    "runtime_events_subscriber",
                    self.runtime_events_subscriber,
                )
        except Exception as exc:
            logger.warning(
                "RuntimeEventsSubscriber not started: %s", exc
            )

        if self.db_logging_service is not None:
            self.db_logging_service.start()
            self._shutdown.register("db_logging_service", self.db_logging_service)
            # Первый из двух вызовов: живого loop на этом шаге ещё нет,
            # поэтому подключается только локальный след, а транспорт
            # помечается невыбранным. Окончательный writer ставится из
            # живого loop — см. ``attach_log_transport`` и его вызов
            # в ``gateway._run`` / ``cli_agent``.
            self.attach_log_transport()

        # Загрузка кэша уже выполнена в composition root
        # (``_init_cache_runtime``): это разовая синхронная операция, у неё
        # нет ни потока, ни ``start()``, ни записи в shutdown-координатор.
        # Фоновой синхронизации больше не существует.

        if self.session_cold_sync_service is not None:
            try:
                self.session_cold_sync_service.start()
                self._shutdown.register(
                    "session_cold_sync_service",
                    self.session_cold_sync_service,
                )
            except Exception as exc:
                logger.warning(
                    "SessionColdSyncService not started: %s", exc
                )

        self._started = True

        # Финальный readiness snapshot для startup-лога.
        if self.runtime_readiness is not None:
            report = self.runtime_readiness.check()
            # Этот модуль логирует через stdlib ``logging``, а gateway
            # настраивает только loguru — stdlib-INFO до потока не доходит.
            # Поэтому итог и разбор компонентов уходят в WARNING: оператор
            # обязан видеть, КАКОЙ компонент DOWN и ПОЧЕМУ, а не только
            # факт ``NOT_READY``.
            breakdown = "; ".join(
                "%s=%s%s" % (
                    c.name,
                    "UP" if c.status == "UP" else "DOWN",
                    (" (%s)" % c.detail) if c.detail else "",
                )
                for c in report.components
            ) or "no components"
            if report.status == "NOT_READY":
                logger.warning(
                    "Required dependencies are down; gateway starts in NOT_READY "
                    "state | components: %s",
                    breakdown,
                )
            else:
                logger.warning("Readiness: %s | components: %s", report.status, breakdown)

    def stop(self) -> None:
        """Корректно остановить все фоновые сервисы."""
        if not self._started:
            return
        if self._shutdown is not None:
            self._shutdown.shutdown_all()
        # Клиент enterprise-mcp: сессия stdio закрывается на том же loop,
        # которому принадлежит. В gateway loop уже закрыт ``asyncio.run'ом`` —
        # тогда сервер завершается сам по закрытию stdin (см. client.close()).
        if getattr(self, "enterprise_mcp", None) is not None:
            try:
                self.enterprise_mcp.close()
            except Exception as exc:
                logger.warning("enterprise_mcp.close failed: %s", exc)
        # MessageBus.drain() ожидает завершения in-flight handler'ов
        # (например, _handle_turn_completed ещё может писать в БД через
        # DbLoggingService с батчевым flush). Вызываем ПОСЛЕ остановки
        # сервисов (channels/sync) и ДО остановки RuntimeEventsSubscriber.
        # Если bus не имеет drain() (защита от nanobot < 0.3.5) — no-op.
        # См. openspec/changes/runtime-events-subscription/design.md D7.
        bus = getattr(self, "bus", None)
        if bus is not None and hasattr(bus, "drain"):
            try:
                drain = bus.drain
                if inspect.iscoroutinefunction(drain):
                    import asyncio
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            asyncio.ensure_future(drain())
                        else:
                            loop.run_until_complete(drain())
                    except RuntimeError:
                        pass
                else:
                    drain()
            except Exception as exc:
                logger.warning("MessageBus.drain failed: %s", exc)
        # RuntimeEventsSubscriber.stop() — после drain, чтобы in-flight
        # handler'ы гарантированно отработали.
        if getattr(self, "runtime_events_subscriber", None) is not None:
            try:
                self.runtime_events_subscriber.stop()
            except Exception as exc:
                logger.warning(
                    "RuntimeEventsSubscriber.stop failed: %s", exc
                )
        # Close LLM usage store (SQLite WAL).
        if self.usage_store is not None:
            try:
                self.usage_store.close()
            except Exception as exc:
                logger.warning("usage_store.close failed: %s", exc)
        # Close снимка убран вместе с обвязкой (фаза 5, п. 5.8): файл
        # ``cache.duckdb`` открывает и закрывает capability ``data``.
        # После остановки сервисов закрываем общий пул соединений.
        _stop_db_pool()
        if self.runtime_health is not None:
            self.runtime_health.mark_stopped()
        self._started = False

    def _validate_runtime_schema(self) -> None:
        """Pre-startup проверка наличия обязательных runtime-таблиц.

        Вызывается из ``start()`` сразу после ``_start_db_pool()`` и
        до подъёма каналов/``db_logging_service``/``sync_service``.
        Имена таблиц берутся из ``self.settings`` (5 ключей:
        ``channels.postgres.{table_name,messages_table,meta_table}``
        + ``logging.db.{table_name,question_runs_table}``)
        — никаких литералов в коде.

        При отсутствии любой таблицы — ``SchemaValidationError``
        (наследник ``ConfigurationError``). Покрывается
        ``gateway.main()`` / ``cli_agent.main()`` startup-boundary →
        ``exit 2`` + ``stderr``.

        Опциональный gate ``gateway.startup.schema_validation.enabled``
        (``True`` по умолчанию) позволяет временно пропустить
        проверку (например, при аварийном деплое).

        См. ``openspec/specs/runtime/startup-schema-validation``.
        """
        try:
            settings = self.settings or {}
        except Exception:
            settings = {}
        gateway_cfg = settings.get("gateway") or {}
        startup_cfg = gateway_cfg.get("startup") or {}
        schema_cfg = startup_cfg.get("schema_validation") or {}
        enabled = schema_cfg.get("enabled", True)
        timeout_sec = float(schema_cfg.get("timeout_sec", 5.0))
        if not enabled:
            logger.warning(
                "startup schema validation is disabled "
                "(gateway.startup.schema_validation.enabled=false)"
            )
            return
        try:
            from utils.db import fetch_with_timeout as _db_fetch
            from lib.services.schema_validation import SchemaValidationService
        except Exception as exc:
            # Если зависимости не загрузились — это серьёзная проблема,
            # но не блокируем startup (раньше без этой проверки gateway
            # всё равно бы упал позже). Логируем warning и пропускаем.
            logger.warning("startup schema validation skipped: %s", exc)
            return
        # ``SchemaValidationService.validate`` бросает ``SchemaValidationError``
        # (наследник ``ConfigurationError``) при missing — пусть поднимется
        # до ``gateway.main()`` / ``cli_agent.main()``. Предел SELECT'а
        # применяет адаптер на соединении пула, а не клиент: прерванное
        # ожидание оставило бы запрос работать в базе.
        def _bounded_fetch(sql: str, *params: Any) -> list[dict[str, Any]]:
            return _db_fetch(sql, *params, timeout_sec=timeout_sec)

        SchemaValidationService.validate(
            settings,
            fetch=_bounded_fetch,
            timeout_sec=timeout_sec,
        )


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _print_startup_block(text: str) -> None:
    """Вывести блок startup-инвентаря в stdout без переносов строк.

    Диагностический лог читает ``tools/diagnose_startup.py``, поэтому текст
    обязан оставаться машинно-читаемым: rich по умолчанию переносит длинные
    строки по ширине консоли, и парсер видит только начало блока.

    Fallback на ``print`` — для старых Windows-консолей без rich.
    """
    try:
        from rich.console import Console

        Console(soft_wrap=True).print(text, markup=False, highlight=False)
    except Exception:
        print(text)


def _log_connected_hooks(ctx: ApplicationContext) -> None:
    """Однократно вывести полный список подключённых хуков.

    Единая точка вывода: плагины ``workspace/hooks/`` + фреймворковые
    хуки (``ToolAuditHook`` — в ``ctx.hooks``) + per-turn hook factories
    (``DatabaseLoggingHook`` — в ``ctx.hook_factories``). Вызывается один
    раз после создания ``AgentLoop``, поэтому двойных сообщений нет.
    """
    names = [type(h).__name__ for h in ctx.hooks]
    if ctx.hook_factories:
        names.append(f"{len(ctx.hook_factories)} hook factory (per-turn)")
    label = ", ".join(names) or "(no hooks connected)"
    try:
        from rich.console import Console

        # soft_wrap=True обязателен: без него rich переносит список по ширине
        # консоли, и ``tools/diagnose_startup.py`` читает только первую
        # физическую строку. Инструмент докладывал «MISSING REQUIRED» по
        # подключённым хукам — на полностью здоровом старте.
        Console(soft_wrap=True).print(f"[green]\u2713[/green] Hooks connected: {label}")
    except Exception:
        # Старые Windows-консоли (cp1251) не умеют ✓ (U+2713) — выводим
        # тот же список обычным print, чтобы информация не пропадала.
        print(f"Hooks connected: {label}")
    _emit_hook_inventory_banner(ctx)


def _emit_hook_inventory_banner(ctx: ApplicationContext) -> None:
    """Промпт-сводка по хукам через ``runtime_inventory.diff_hooks``.

    Печатает:
      * (нет вывода) — все required хуки на месте, нет unexpected;
      * жёлтый блок — missing_optional / unexpected (не критично);
      * красный блок — missing_required / missing_factory (нужно внимание).

    Срабатывает ПОСЛЕ обычного ``Hooks connected: ...`` лога; использует
    ``rich.console.Console`` с явными цветами/рамочкой, чтобы в глаза
    бросалось даже если loguru/WARNING уровень подавлен.
    """
    from lib.services.runtime_inventory import collect_actual_hook_names, diff_hooks

    actual_names, factory_count = collect_actual_hook_names(ctx)
    diff = diff_hooks(actual_names, actual_factory_count=factory_count)

    has_issue = (
        diff["missing_required"] or diff["missing_factory"] or diff["unexpected"]
    )
    has_warn = diff["missing_optional"]
    if not has_issue and not has_warn:
        return

    try:
        from rich.console import Console
        from rich.panel import Panel

        console = Console(stderr=True)
        if has_issue:
            style = "bold red"
            header = "HOOK INVENTORY: critical drift detected"
        else:
            style = "bold yellow"
            header = "HOOK INVENTORY: optional drift"

        lines: list[str] = []
        if diff["missing_required"]:
            lines.append(
                f"[red]MISSING REQUIRED:[/red] {', '.join(diff['missing_required'])}"
            )
        if diff["missing_factory"]:
            lines.append(
                f"[red]MISSING FACTORY:[/red] {', '.join(diff['missing_factory'])}"
            )
        if diff["unexpected"]:
            lines.append(
                f"[yellow]UNEXPECTED:[/yellow] {', '.join(diff['unexpected'])}"
            )
        if diff["missing_optional"]:
            lines.append(
                f"[yellow]MISSING OPTIONAL:[/yellow] {', '.join(diff['missing_optional'])}"
            )

        console.print(
            Panel(
                "\n".join(lines),
                title=header,
                border_style=style.replace("bold ", ""),
                title_align="left",
            )
        )
    except Exception as exc:
        # Fallback на plain stderr, чтобы не потерять диагностику
        # в средах без rich (например, при cp1251 + minimal Python).
        import sys
        sys.stderr.write(
            f"HOOK INVENTORY: missing_required={diff['missing_required']} "
            f"unexpected={diff['unexpected']} "
            f"missing_factory={diff['missing_factory']} "
            f"missing_optional={diff['missing_optional']} ({exc})\n"
        )


def _emit_patch_inventory_banner(patch_report: Any) -> None:
    """Промпт-сводка по runtime-патчам через ``runtime_inventory.diff_runtime_patches``.

    Печатает красный блок, если required-патч fail'ит или не запустился;
    жёлтый — если есть failed optional (полезно видеть, но не ломает
    runtime). Срабатывает ПОСЛЕ обычного ``Runtime patches:`` лога.
    """
    from lib.services.runtime_inventory import diff_runtime_patches

    diff = diff_runtime_patches(
        applied=list(patch_report.applied),
        skipped=list(patch_report.skipped),
        failed=list(patch_report.failed),
    )

    has_critical = diff["missing_required"] or diff["failed_required"]
    has_warn = bool(patch_report.failed) and not has_critical
    if not has_critical and not has_warn:
        return

    try:
        from rich.console import Console
        from rich.panel import Panel

        console = Console(stderr=True)
        if has_critical:
            style = "bold red"
            header = "RUNTIME PATCH INVENTORY: critical drift"
        else:
            style = "bold yellow"
            header = "RUNTIME PATCH INVENTORY: optional patches failed"

        lines: list[str] = []
        if diff["missing_required"]:
            lines.append(
                f"[red]MISSING REQUIRED:[/red] {', '.join(diff['missing_required'])}"
            )
        if diff["failed_required"]:
            lines.append(
                f"[red]FAILED REQUIRED:[/red] {', '.join(diff['failed_required'])}"
            )
        if has_warn:
            failed_optional = [
                n for n, _ in patch_report.failed
                if n not in diff["failed_required"]
            ]
            if failed_optional:
                lines.append(
                    f"[yellow]FAILED OPTIONAL:[/yellow] {', '.join(failed_optional)}"
                )

        console.print(
            Panel(
                "\n".join(lines),
                title=header,
                border_style=style.replace("bold ", ""),
                title_align="left",
            )
        )
    except Exception as exc:
        import sys
        sys.stderr.write(
            f"RUNTIME PATCH INVENTORY: missing_required={diff['missing_required']} "
            f"failed_required={diff['failed_required']} "
            f"failed={list(patch_report.failed)} ({exc})\n"
        )


def _emit_project_tools_inventory_banner(project_tools_result: Any) -> None:
    """Промпт-сводка по project tools через ``runtime_inventory``.

    Использует **структурные поля** ``ProjectToolsLoadResult``
    (``registered`` / ``disabled`` / ``duplicate`` / ``failed`` /
    ``error``) **напрямую**, а не regex-парсинг ``detail``. Это:

      * даёт корректный баннер при outer-loader failure (раньше
        ``detail = "register_project_tools failed: RuntimeError: ..."``
        парсился как ``failed=["RuntimeError: ..."]`` — мусор);
      * отделяет ``loader-level error`` от ``per-tool failed``;
      * сохраняет совместимость с ``diagnose_startup.py``, который
        читает ``Custom (project) tools:`` из логов (там всё ещё
        ``detail`` — ``runtime_inventory.parse_project_tools_detail``).

    Печатает красный блок, если required tool не зарегистрировался
    (missing или failed) или loader вернул ``error``; жёлтый — если
    unexpected tool или failed optional.
    """
    if project_tools_result is None:
        return
    registered = list(getattr(project_tools_result, "registered", []) or [])
    disabled = list(getattr(project_tools_result, "disabled", []) or [])
    duplicate = list(getattr(project_tools_result, "duplicate", []) or [])
    failed = list(getattr(project_tools_result, "failed", []) or [])
    error = getattr(project_tools_result, "error", None)

    # Раньше banner анализировал detail через regex, что для outer
    # failure давало семантически неправильный результат
    # (failed = ["RuntimeError: ...]"). Сейчас diff вычисляется из
    # structured-полей напрямую.
    from lib.services.runtime_inventory import diff_project_tools

    diff = diff_project_tools(
        registered=registered,
        skipped_disabled=disabled,
        failed=failed,
    )

    has_critical = bool(diff["missing_required"] or diff["failed"] or error)
    has_warn = bool(diff["unexpected"] or diff["disabled_required"] or duplicate)
    if not has_critical and not has_warn:
        return

    try:
        from rich.console import Console
        from rich.panel import Panel

        console = Console(stderr=True)
        if has_critical:
            style = "bold red"
            header = "PROJECT TOOLS INVENTORY: critical drift"
        else:
            style = "bold yellow"
            header = "PROJECT TOOLS INVENTORY: drift"

        lines: list[str] = []
        if error:
            # Outer-loader failure (``_discover`` / ``ToolContext`` / etc.)
            # — отдельная категория, не путать с per-tool failed.
            lines.append(f"[red]LOADER ERROR:[/red] {error}")
        if diff["missing_required"]:
            lines.append(
                f"[red]MISSING REQUIRED:[/red] {', '.join(diff['missing_required'])}"
            )
        if diff["failed"]:
            lines.append(
                f"[red]FAILED:[/red] {', '.join(diff['failed'])}"
            )
        if diff["disabled_required"]:
            lines.append(
                f"[red]DISABLED REQUIRED:[/red] {', '.join(diff['disabled_required'])}"
            )
        if diff["unexpected"]:
            lines.append(
                f"[yellow]UNEXPECTED:[/yellow] {', '.join(diff['unexpected'])}"
            )
        if duplicate:
            # Два файла с одинаковым ToolSpec: зарегистрирован первый,
            # второй молча выпал. Без этой строки потеря была бы невидима.
            lines.append(
                f"[yellow]DUPLICATE (не зарегистрирован):[/yellow] {', '.join(duplicate)}"
            )

        console.print(
            Panel(
                "\n".join(lines),
                title=header,
                border_style=style.replace("bold ", ""),
                title_align="left",
            )
        )
    except Exception as exc:
        import sys
        sys.stderr.write(
            f"PROJECT TOOLS INVENTORY: error={error} "
            f"missing_required={diff['missing_required']} "
            f"failed={diff['failed']} unexpected={diff['unexpected']} ({exc})\n"
        )


def _register_readiness_checks(ctx: ApplicationContext) -> None:
    """Зарегистрировать проверки зависимостей для RuntimeReadiness.

    Профили:
      * ``postgres`` — **required только когда БД реально участвует в
        работе**: включён postgres-канал ИЛИ storage работает в режиме
        ``postgres``. Если канал выключен и storage в file-режиме, БД не
        нужна, и её недоступность — DEGRADED, а не NOT_READY.

    Здоровье определяется **реальным ping'ом по пулу**, а не именем класса
    менеджера сессий. Имя класса как признак непригодно принципиально:
    ``lib.session.pg_session_manager.build_session_manager`` возвращает
    библиотечный ``SessionManager`` (не подкласс), а ``install_async_save``
    возвращает тот же объект. Поэтому гейт ``"PG" in cls or "Postgres" in
    cls`` не срабатывал НИКОГДА, и при полностью рабочей системе readiness
    оставался NOT_READY, а код пинга ниже гейта был недостижим.

    Проверки ``duckdb_cache`` и ``vector_search`` сняты в фазе 5 (п. 5.8):
    они читали состояние снимка, которого в агенте больше нет. Оставить их
    было бы хуже, чем убрать — компонент, которого нет, всегда DOWN, то есть
    readiness не смог бы стать READY никогда. Здоровье снимка и индексов
    теперь отвечает платформа, у которой свои capability ``data`` и
    ``vectors``.

    Все проверки идемпотентны и быстрые (< 1 сек каждая).
    """
    from lib.services.runtime_health import (
        ComponentStatus,
    )

    # Required-ness — следствие конфигурации, а не константа: ``register``
    # фиксирует флаг навсегда, сменить его на каждый ``check()`` нельзя.
    _pg_cfg = (getattr(ctx, "settings", None) or {}).get("channels", {}).get("postgres", {})
    _pg_channel_on = bool(_pg_cfg.get("enabled", False))
    _pg_dsn = str(_pg_cfg.get("dsn") or "").strip()
    _pg_required = _pg_channel_on or getattr(ctx, "storage_mode", "") == "postgres"

    def _detail(extra: str = "") -> str:
        parts = ["storage_mode=%s" % getattr(ctx, "storage_mode", "?")]
        if extra:
            parts.append(extra)
        return ", ".join(parts)

    def check_postgres() -> ComponentStatus | None:
        sm = getattr(ctx, "session_manager", None)
        if sm is None:
            return ComponentStatus(
                name="postgres", required=_pg_required, status="DOWN",
                detail="no session_manager",
            )
        # БД по конфигурации не нужна и DSN не задан — пинговать нечего,
        # и ждать 2 с таймаута на заведомо отсутствующем пуле незачем.
        if not _pg_required and not _pg_dsn:
            return ComponentStatus(
                name="postgres", required=False, status="UP",
                detail=_detail("pg not required (channel disabled, storage=file)"),
            )
        # Реальный ping через пул соединений — единственный честный признак
        # здоровья. Используем прямой submit с таймаутом, чтобы при
        # недоступном PG readiness-чек не зависал на внутренних backoff-ретраях
        # воркера (psycopg2.connect + connect_max_retries могут занять
        # десятки секунд, а ``fetch().get()`` блокирует навсегда).
        try:
            import sys
            from pathlib import Path

            _ws = Path(__file__).resolve().parents[2] / "workspace"
            if str(_ws) not in sys.path:
                sys.path.insert(0, str(_ws))
            from utils.db import _get_manager, _Job

            def _ping(conn: Any) -> Any:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    return cur.fetchone()

            # ``_submit`` возвращает сам ``_JobResult`` (у него и есть
            # ``.get``); ``_Job.result`` — это тот же объект, так что
            # ``_submit(...).result`` даёт AttributeError. ``get`` бросает
            # ошибку воркера и возвращает ``None`` по таймауту.
            pending = _get_manager()._submit(_Job(_ping, tag="readiness.postgres"))
            result = pending.get(timeout=2.0)
            if result is None:
                return ComponentStatus(
                    name="postgres", required=_pg_required, status="DOWN",
                    detail=_detail("pg ping timeout (2s)"),
                )
            return ComponentStatus(
                name="postgres", required=_pg_required, status="UP",
                detail=_detail(),
            )
        except Exception as exc:
            return ComponentStatus(
                name="postgres", required=_pg_required, status="DOWN",
                detail=_detail(f"pg ping failed: {type(exc).__name__}: {exc}"),
            )

    ctx.runtime_readiness.register(
        "postgres", check_postgres, required=_pg_required
    )


def _make_config_service(
    script_dir: Path,
    workspace_dir: Path,
    *,
    settings_override: Any | None = None,
) -> Any:
    """Создать ``ConfigService``, привязанный к корню проекта.

    Использует lazy-import, чтобы не зависеть от ``config.py`` на
    старте (если config битый, ошибка проявится в ``.load()``).

    ``settings_override`` — готовый resolved SETTINGS (от Resolver).
    Если не передан — ConfigService возвращает глобальный
    ``config.SETTINGS``. Оба пути Resolver-разрешённые.
    """
    from lib.services.config_service import ConfigService

    return ConfigService(
        script_dir=script_dir,
        workspace_dir=workspace_dir,
        settings_override=settings_override,
    )


def _resolve_agent_id(config: Any) -> str:
    """Получить идентификатор агента из конфигурации (или ``"main"``)."""
    try:
        agents = getattr(config, "agents", None)
        if agents is not None:
            defaults = getattr(agents, "defaults", None) or {}
            name = defaults.get("name") if isinstance(defaults, dict) else getattr(defaults, "name", None)
            if name:
                return str(name)
    except Exception:
        pass
    return "main"


#: Дефолт лимита длины результата tool'а, если runtime-конфиг его не задал.
#: Совпадает с дефолтом nanobot ``AgentLoop.max_tool_result_chars``.
_DEFAULT_MAX_TOOL_RESULT_CHARS = 16_000


def _make_db_logging(ctx: ApplicationContext) -> Any | None:
    """Собрать ``DbLoggingService`` из секции ``logging.db`` в settings.

    Возвращает ``None`` если:
      * ``logging.db.enabled != True`` (явно отключено);
      * нет DSN в ``channels.postgres`` (некуда писать);
      * psycopg2 не импортируется (битое окружение).

    DSN берётся из ``channels.postgres.dsn`` (тот же, что для
    ``SessionColdSyncService`` и PostgresChannel). Резервной записи в JSONL-файл
    нет: при недоступности БД события выбрасываются.
    """
    try:
        from lib.services.config_service import ConfigService  # noqa: F401
        from lib.services.db_logging_service import DbLoggingService
    except Exception as exc:
        logger.warning("DbLoggingService unavailable: %s", exc)
        return None

    log_cfg = ctx.config_service.settings_section("logging")
    db_cfg = log_cfg.get("db", {}) if isinstance(log_cfg, dict) else {}
    if not db_cfg.get("enabled", False):
        return None

    pg = ctx.config_service.settings_section("channels").get("postgres", {})
    dsn = ""
    if isinstance(pg, dict):
        dsn = pg.get("dsn", "") or ""
    if not dsn:
        return None

    table_name = db_cfg.get("table_name", "")
    question_runs_table = db_cfg.get("question_runs_table", "")
    if not table_name or not question_runs_table:
        from config import ConfigurationError

        raise ConfigurationError(
            "конфиг logging.db (table_name и question_runs_table) "
            "обязательны для DbLoggingService (нет авто-дефолтов в коде). "
            f"table_name={table_name!r}, question_runs_table={question_runs_table!r}"
        )

    # ``logging.db.flush_interval_sec`` (см. ``LoggingDbSettings``):
    # диапазон ``0.5 ≤ value ≤ 60.0`` сек, дефолт ``5.0``. Значение
    # уже валидировано pydantic на старте ``ApplicationContext.create``
    # через ``validate_project_settings`` (шаг 1a), и ``LoggingDbSettings.
    # _default_flush_interval_sec`` подменяет ``None`` на ``5.0``.
    # Здесь читаем уже валидный ``float`` из типизированной проекции —
    # единственный путь разрешения конфигурации (см. ``docs/PROFILES.md``
    # § «Configuration resolver chain»); ``project_settings`` всегда
    # инициализирован к моменту этого шага (fail-fast на шаге 1a).
    flush_interval_sec = (
        ctx.project_settings.logging.db.flush_interval_sec
    )

    return DbLoggingService(
        dsn=dsn,
        table_name=table_name,
        question_runs_table=question_runs_table,
        schema=db_cfg.get("schema", "public"),
        dialect=db_cfg.get("dialect", "postgres"),
        flush_interval_sec=flush_interval_sec,
        batch_size=int(db_cfg.get("batch_size", 100)),
        queue_maxsize=int(db_cfg.get("queue_maxsize", 10000)),
        min_level=db_cfg.get("min_level", "INFO"),
        connect_backoff_sec=float(db_cfg.get("connect_backoff_sec", 1.0)),
        connect_backoff_max_sec=float(db_cfg.get("connect_backoff_max_sec", 60.0)),
        summary_max_chars=int(db_cfg.get("summary_max_chars", 200)),
        retention_days=int(db_cfg.get("retention_days", 90)),
        purge_interval_sec=float(db_cfg.get("purge_interval_sec", 3600.0)),
    )


def _make_enterprise_mcp(settings: Any, ctx: Any = None) -> Any:
    """Создать клиента к MCP-серверу ``enterprise-mcp``.

    ``None`` — раздел ``enterprise_mcp`` выключен или не задан. Это не
    ошибка: без него агент работает, но потребители, которым нужен
    сервер, отвечают структурной ошибкой вместо падения на старте.

    Ничего сверх ``config.json → gateway.agent.enterprise_mcp`` здесь не передаётся.
    Путь к снимку и объявления индексов раньше уходили в процесс сервера
    переменными окружения; они живут в ``mcp-platform/platform.json``, и
    экспорт не просто дублировал их, а молча затирал файловое значение —
    окружение приоритетнее.
    """
    from lib.services.enterprise_mcp_client import client_from_settings

    # Журнал нужен клиенту, чтобы достроить ``request_id`` оборота, когда
    # вызывающий tool' личность не передал. Пробуем оба имени: на момент
    # сборки сервис может быть ещё не создан.
    return client_from_settings(
        settings,
        db_logging_service=getattr(ctx, "db_logging_service", None)
        or getattr(ctx, "_db_logging_service", None),
    )


def _make_cron_service(config: Any) -> Any:
    """Создать ``CronService`` для CLI-режима (только там он нужен).

    ``CronService`` хранит задачи в ``workspace/cron/jobs.json`` —
    путь относительно ``config.workspace_path``. Если директории нет,
    CronService создаст её при первом сохранении задачи.
    """
    from nanobot.cron.service import CronService

    return CronService(config.workspace_path / "cron" / "jobs.json")


def _make_session_cold_sync_service(ctx: ApplicationContext) -> Any | None:
    """Создать ``SessionColdSyncService`` (cold-storage mirror).

    Сервис создаётся только если:

    - есть ``session_manager`` (upstream JSONL);
    - в PG-конфиге указан DSN (cold-storage нужен только при
      PG-деплое).

    Если условия не выполнены — возвращает ``None``.

    См. спеку ``openspec/specs/storage/session-hybridization/spec.md``
    requirement «Cold-storage mirror в PostgreSQL».
    """
    if ctx.session_manager is None:
        return None
    try:
        from config import get_setting, require_setting

        pg_dsn = get_setting("channels", "postgres", "dsn", default="")
    except Exception:
        pg_dsn = ""
    if not pg_dsn:
        return None

    try:
        sync_cfg = ctx.config_service.settings_section("gateway").get(
            "session_cold_sync", {}
        )
    except Exception:
        sync_cfg = {}
    if not isinstance(sync_cfg, dict):
        sync_cfg = {}

    enabled = bool(sync_cfg.get("enabled", True))
    sync_interval_sec = float(sync_cfg.get("sync_interval_sec", 30.0))
    batch_size = int(sync_cfg.get("batch_size", 50))
    stale_tolerance_seconds = int(sync_cfg.get("stale_tolerance_seconds", 120))
    sync_lag_threshold_seconds = int(
        sync_cfg.get("sync_lag_threshold_seconds", 3600)
    )

    # Имена таблиц — обязательные ключи конфигурации: литерал в коде означал бы
    # вторую копию объявления, которая молча разойдётся с config.json/профилем.
    schema = get_setting("channels", "postgres", "schema", default="public")
    meta_table = require_setting("channels", "postgres", "meta_table")
    messages_table = require_setting("channels", "postgres", "messages_table")

    from lib.services.session_cold_sync_service import SessionColdSyncService

    logger.info(
        'session_cold_sync: stale_tolerance=%ss, '
        'sync_lag_threshold=%ss, sync_interval=%ss, batch=%d, enabled=%s',
        stale_tolerance_seconds,
        sync_lag_threshold_seconds,
        sync_interval_sec,
        batch_size,
        enabled,
    )

    return SessionColdSyncService(
        session_manager=ctx.session_manager,
        pg_dsn=pg_dsn,
        schema=schema,
        meta_table=meta_table,
        messages_table=messages_table,
        sync_interval_sec=sync_interval_sec,
        batch_size=batch_size,
        enabled=enabled,
        db_logging_service=ctx.db_logging_service,
        stale_tolerance_seconds=stale_tolerance_seconds,
        sync_lag_threshold_seconds=sync_lag_threshold_seconds,
    )


def _wrap_bus_publish(bus: Any, method: str, log: Any) -> None:
    """Подменить ``bus.<method>`` на async-обёртку ``await log(); await original()``.

    Оригинальный метод сохраняется в замыкании. Если ``log`` бросит
    исключение — оно глотается, оригинальный метод всё равно зовётся.
    Это критично: иначе один сломанный логгер положил бы шину сообщений.
    """
    original = getattr(bus, method)

    async def _wrapper(msg: Any) -> None:
        try:
            await log(msg)
        except Exception:
            pass
        await original(msg)

    setattr(bus, method, _wrapper)


def _create_bus(inbound_logger: Any, outbound_logger: Any) -> Any:
    """Создать ``MessageBus`` и обернуть публикации логгерами.

    ``MessageBus`` — класс библиотеки (``nanobot.bus.queue``): через неё
    каналы публикуют входящие сообщения, а ``AgentLoop`` — исходящие.
    Хуков на публикацию у неё нет, поэтому логирование входящих/исходящих
    подключается подменой двух методов на async-обёртки.

    Отдельный модуль-фабрика для этого не нужен: вся сборка — создать шину
    и, если логгеры заданы, подменить два метода.
    """
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    if inbound_logger is not None:
        _wrap_bus_publish(bus, "publish_inbound", inbound_logger)
    if outbound_logger is not None:
        _wrap_bus_publish(bus, "publish_outbound", outbound_logger)
    return bus


def _make_usage_store(ctx: ApplicationContext) -> Any | None:
    """Создать ``LLMUsageStore`` (upstream nanobot) по конфигу.

    Конфиг — ``gateway.usage_store.*`` (``sqlite_path``,
    ``enabled``). Возвращает ``None`` если отключено.

    Хранилище — штатный синглтон библиотеки
    (``nanobot.llm_usage.get_llm_usage_store``): путь по умолчанию,
    потокобезопасный кеш и создание объекта — его забота. Наш код передаёт
    путь, только если он задан явно, и тогда сам создаёт каталог: в отличие
    от синглтона, мы обязаны учесть ``sqlite_path`` из конфига.

    См. спеку ``openspec/specs/storage/usage-store/spec.md``.
    """
    try:
        usage_cfg = ctx.config_service.settings_section("gateway").get(
            "usage_store", None
        )
    except Exception:
        usage_cfg = None
    if not usage_cfg or not usage_cfg.get("enabled", True):
        return None

    raw_path = usage_cfg.get("sqlite_path")
    try:
        from nanobot.llm_usage import get_llm_usage_store

        if not raw_path:
            return get_llm_usage_store()
        from pathlib import Path

        sqlite_path = Path(str(raw_path)).expanduser()
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        return get_llm_usage_store(sqlite_path)
    except Exception as exc:
        logger.warning(
            "LLMUsageStore unavailable ({}); observer will not be attached",
            exc,
        )
        return None


# ----------------------------------------------------------------------
# Общий пул соединений utils.db
# ----------------------------------------------------------------------


def _configure_db_pool(pool_cfg: dict, print_activity: bool = False) -> None:
    """Применить ``channels.postgres.pool`` к общему пулу ``utils.db``.

    ``pool_cfg`` — словарь с ключами ``min_conn/max_conn/pool_timeout/
    queue_maxsize/reconnect_backoff_sec/reconnect_backoff_max_sec/
    connect_max_retries/idle_timeout_sec/job_max_retries``. Неизвестные
    ключи игнорируются (``set_pool_config`` принимает только известные).

    ``print_activity`` — вывод активности db-worker'ов (гейт
    ``gateway.print_db_activity``), кладётся в конфиг пула как
    ``print_activity``.
    """
    try:
        from utils.db import set_pool_config

        merged = dict(pool_cfg)
        merged["print_activity"] = bool(print_activity)
        set_pool_config(merged)
    except Exception as exc:
        logger.warning("utils.db pool config ignored: %s", exc)


def _start_db_pool() -> None:
    """Запустить общий пул ``utils.db`` (воркеры подключаются лениво)."""
    try:
        from utils.db import start

        start()
    except Exception as exc:
        logger.warning("utils.db pool start failed: %s", exc)


def _stop_db_pool() -> None:
    """Остановить общий пул ``utils.db`` и закрыть все соединения."""
    try:
        from utils.db import shutdown

        shutdown()
    except Exception as exc:
        logger.warning("utils.db pool shutdown failed: %s", exc)
