"""Loader для кастомных tool'ов из ``workspace/tools/*.py``.

Заменяет ``RuntimePatcher.patch_project_tools`` (который жил в
``runtime_patcher.py`` до opencode change ``runtime-patcher-composition-cleanup``).
Это **stateless helper**, а не компонент:

  * нет lifecycle (``start``/``stop``);
  * нет state;
  * нет конфигурации (DI пробрасывается через kwargs);
  * единственный публичный контракт — ``register_project_tools(...)``
    + dataclass ``ProjectToolsLoadResult``.

Контракт не разрастается: discovery приватный (``_discover``),
DI-передача и регистрация идут внутри одной функции. Никакой
dependency на ``RuntimePatcher``: loader — **независимый stage**
composition root'а в ``ApplicationContext.create()``.

Поведение best-effort с частичным успехом (см. design.md Decision 3):

  * ошибка одного tool (``Tool.enabled`` / ``Tool.create`` /
    ``agent.tools.register``) **не отменяет** успешно зарегистрированные
    остальные;
  * результат фиксируется в структурных полях ``registered`` /
    ``disabled`` / ``duplicate`` / ``failed`` (для programmatic consumers);
  * ``detail: str`` — форматированная строка для баннера логов,
    совместимая с ``runtime_inventory.parse_project_tools_detail``.
"""
from __future__ import annotations

import importlib.util
import pkgutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ProjectToolsLoadResult:
    """Результат регистрации project tools.

    Attributes:
        registered: имена tool'ов, успешно зарегистрированных в
            ``agent.tools`` (порядок: как прошли discovery и DI).
        disabled: имена tool'ов, пропущенных по конфигурации
            (``Tool.enabled(ctx) == False``). Используется
            **каноническое** имя tool'а (``tool.name``) для
            совпадения с ``runtime_inventory.canonical_project_tools()``;
            fallback на ``cls.__name__`` — если ``cls().name`` падает.
        duplicate: имена tool'ов, не зарегистрированных из-за конфликта
            имён (в ``agent.tools`` уже есть tool с таким именем).
        failed: имена tool'ов, упавших на ``Tool.create()`` или
            ``agent.tools.register`` (с ``logger.exception`` трассой).
            Используется **каноническое** имя (``tool.name``), fallback
            на ``cls.__name__``. На **outer** failure loader-а
            (``_discover`` / импорт / ``ToolContext`` и т.п.)
            в ``failed`` пишется ``["register_project_tools"]``,
            чтобы banner diagnostics не терял loader-level ошибку.
        detail: presentation/diagnostic строка для баннера логов.
            Формат совместим с
            ``lib.services.runtime_inventory.parse_project_tools_detail``:
            ``[INTERNAL_FAILED] N project tools registered: a, b;
            M disabled by config: c; K already registered: d;
            J failed: e``. Маркер ``[INTERNAL_FAILED]`` ставится
            если ``failed`` непустой (включая outer-loader failure).
        error: ``str`` c repr внешнего исключения loader-а, если
            произошло в ``_discover`` / ``ToolContext`` / etc.
            ``None`` если все шаги прошли штатно. Используется
            programmatic consumers (banner'ы / ``diagnose_startup``)
            для различения «частичный success» vs «loader failure».
    """

    registered: list[str] = field(default_factory=list)
    disabled: list[str] = field(default_factory=list)
    duplicate: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    detail: str = ""
    error: str | None = None


def _discover(workspace_dir: Path) -> list[type]:
    """Discover ``Tool``-подклассы в ``<workspace>/tools/*.py``.

    Приватная функция — наружу торчит только ``register_project_tools``.
    Возвращает список классов (не инстансов), найденных в
    ``workspace/tools/``. Импортирует каждый ``<name>.py`` (не начинающийся
    с ``_``) через ``importlib.util.spec_from_file_location`` под именем
    ``workspace.tools.<name>``. Затем собирает ``Tool``-подклассы
    (не абстрактные), дедуплицирует по ``id(cls)``.
    """
    from loguru import logger

    tools_dir = Path(workspace_dir) / "tools"
    if not tools_dir.is_dir():
        return []

    for _imp, mod_name, _ispkg in pkgutil.iter_modules([str(tools_dir)]):
        if mod_name.startswith("_"):
            continue
        full = f"workspace.tools.{mod_name}"
        if full in sys.modules:
            continue
        try:
            file_path = tools_dir / f"{mod_name}.py"
            spec = importlib.util.spec_from_file_location(full, str(file_path))
            if spec is None or spec.loader is None:
                logger.warning("Failed to build spec for {}", full)
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[full] = module
            try:
                spec.loader.exec_module(module)
            except Exception:
                sys.modules.pop(full, None)
                raise
        except Exception:
            logger.exception("Failed to import {}", full)

    from nanobot.agent.tools.base import Tool as _T

    candidates: list[type] = []
    seen_ids: set[int] = set()
    for mod_name in list(sys.modules):
        if not mod_name.startswith("workspace.tools."):
            continue
        module = sys.modules.get(mod_name)
        if module is None:
            continue
        for attr_name in dir(module):
            cls = getattr(module, attr_name, None)
            if not (isinstance(cls, type) and issubclass(cls, _T)):
                continue
            if cls is _T:
                continue
            if getattr(cls, "__abstractmethods__", None):
                continue
            if id(cls) in seen_ids:
                continue
            seen_ids.add(id(cls))
            candidates.append(cls)
    return candidates


def _build_tool_context(agent: Any, settings: Any,
                        db_logging_service: Any, enterprise_mcp: Any = None) -> Any:
    """Собрать ``ToolContext`` из атрибутов ``AgentLoop``.

    Тот же набор kwargs, что был в ``RuntimePatcher.patch_project_tools``
    (см. ``runtime_patcher.py:2392-2417`` в pre-change версии):
    ``config`` / ``workspace`` / ``bus`` / ``subagent_manager`` /
    ``cron_service`` / ``exec_session_manager`` / ``sessions`` /
    ``file_state_store`` / ``provider_snapshot_loader`` /
    ``image_generation_provider_configs`` / ``timezone`` /
    ``workspace_sandbox`` / ``runtime_control``.

    DI-расширения (``agent`` / ``settings`` / ``db_logging_service``)
    прокидываются через ``setattr`` — ``ToolContext`` остаётся frozen=False
    в nanobot 0.3.5.
    """
    from nanobot.agent.tools.context import ToolContext

    ctx = ToolContext(
        config=getattr(agent, "tools_config", None),
        workspace=str(getattr(agent, "workspace", "")),
        bus=getattr(agent, "bus", None),
        subagent_manager=getattr(agent, "subagents", None),
        cron_service=getattr(agent, "cron_service", None),
        exec_session_manager=getattr(agent, "_exec_session_manager", None),
        sessions=getattr(agent, "sessions", None),
        file_state_store=getattr(agent, "file_states", None),
        provider_snapshot_loader=getattr(
            agent, "provider_snapshot_loader", None,
        ),
        image_generation_provider_configs=getattr(
            agent, "_image_generation_provider_configs", None,
        ),
        timezone=getattr(
            getattr(agent, "context", None), "timezone", "UTC",
        ) or "UTC",
        workspace_sandbox=getattr(
            getattr(agent, "workspace_scopes", None),
            "sandbox_status", None,
        ),
        runtime_control=getattr(agent, "_runtime_control", None),
    )
    ctx._agent_ref = agent
    if settings is not None:
        ctx._settings_ref = settings
    if db_logging_service is not None:
        ctx._db_logging_service = db_logging_service
    if enterprise_mcp is not None:
        ctx._enterprise_mcp = enterprise_mcp
    return ctx


def register_project_tools(
    agent: Any,
    workspace_dir: Any,
    *,
    settings: Any = None,
    db_logging_service: Any = None,
    enterprise_mcp: Any = None,
) -> ProjectToolsLoadResult:
    """Discover + DI + register project tools из ``<workspace>/tools/``.

    Best-effort с частичным успехом: ошибка одного tool (``enabled`` /
    ``create`` / ``register``) не отменяет уже зарегистрированные.

    Args:
        agent: ``AgentLoop`` (target ``agent.tools.register``).
        workspace_dir: ``Path`` — корень workspace, в нём лежит ``tools/``.
        settings: ``SETTINGS`` (опционально) — для ``ctx._settings_ref``.
        db_logging_service: ``DbLoggingService`` (опционально) — для
            ``ctx._db_logging_service``.
        enterprise_mcp: ``EnterpriseMcpClient`` (опционально) — для
            ``ctx._enterprise_mcp``; ``None``, если раздел
            ``enterprise_mcp`` выключен или не задан.

    Returns:
        ``ProjectToolsLoadResult`` со структурными полями ``registered`` /
        ``disabled`` / ``duplicate`` / ``failed`` и ``detail``-строкой
        для баннера логов (формат совместим с
        ``runtime_inventory.parse_project_tools_detail``).

        Имена в ``registered``/``disabled``/``duplicate``/``failed``
        — **канонические** (``tool.name``), что позволяет
        ``runtime_inventory.diff_project_tools()`` матчить их с
        ``canonical_project_tools()``. Fallback на ``cls.__name__``
        если ``cls().name`` падает.

        При **outer-failure** (сбой в ``_discover`` / ``_build_tool_context``
        / etc., пойманный внешним ``except``) — ``failed`` заполняется
        маркером ``"register_project_tools"`` и ``error`` получает
        repr исключения, чтобы banner diagnostics (``diff_project_tools``)
        видел loader-level ошибку, а не только частичный success.
    """
    from loguru import logger

    def _canonical_name(cls: type) -> str:
        """Получить каноническое имя tool'а (``tool.name``).

        На момент ``enabled() == False`` инстанса ещё нет, поэтому
        пытаемся создать его без side-effects (``Tool.__init__``
        no-op в nanobot 0.3.5). Если не получилось — fallback
        на ``cls.__name__`` (старое поведение для диагностики).
        """
        try:
            return cls().name
        except Exception:
            return cls.__name__

    if agent is None:
        return ProjectToolsLoadResult(
            failed=["register_project_tools"],
            detail="agent is None",
            error="agent is None",
        )

    try:
        tools_dir = Path(workspace_dir) / "tools"
        if not tools_dir.is_dir():
            return ProjectToolsLoadResult(detail="workspace/tools not found — skip")

        candidates = _discover(workspace_dir)
        if not candidates:
            return ProjectToolsLoadResult(detail="no project tools found")

        ctx = _build_tool_context(agent, settings, db_logging_service, enterprise_mcp)

        registered: list[str] = []
        skipped_disabled: list[str] = []
        skipped_duplicate: list[str] = []
        failed: list[str] = []

        for cls in candidates:
            canonical = _canonical_name(cls)
            try:
                if not cls.enabled(ctx):
                    skipped_disabled.append(canonical)
                    continue
                tool = cls.create(ctx)
                if agent.tools.get(tool.name) is not None:
                    skipped_duplicate.append(tool.name)
                    continue
                agent.tools.register(tool)
                registered.append(tool.name)
            except Exception:
                logger.exception("Failed to register {}", canonical)
                failed.append(canonical)

        detail = f"{len(registered)} project tools registered"
        if registered:
            detail += f": {', '.join(registered)}"
        if skipped_disabled:
            detail += (
                f"; {len(skipped_disabled)} disabled by config: "
                f"{', '.join(skipped_disabled)}"
            )
        if skipped_duplicate:
            detail += (
                f"; {len(skipped_duplicate)} already registered: "
                f"{', '.join(skipped_duplicate)}"
            )
        if failed:
            detail += f"; {len(failed)} failed: {', '.join(failed)}"
            detail = "[INTERNAL_FAILED] " + detail

        logger.info(
            "Custom (project) tools: {} — workspace={}",
            detail, workspace_dir,
        )
        return ProjectToolsLoadResult(
            registered=registered,
            disabled=skipped_disabled,
            duplicate=skipped_duplicate,
            failed=failed,
            detail=detail,
        )
    except Exception as exc:
        logger.exception("register_project_tools failed: {}", exc)
        # Outer-failure: ``failed=["register_project_tools"]`` —
        # маркер для banner diagnostics; ``error`` — repr для
        # programmatic consumers. Detail остаётся совместимым
        # с ``parse_project_tools_detail`` (префикс ``[INTERNAL_FAILED]``
        # парсером НЕ распознаётся — но banner смотрит на ``failed``
        # через ``ProjectToolsLoadResult.failed``, а не на detail).
        return ProjectToolsLoadResult(
            failed=["register_project_tools"],
            detail=f"register_project_tools failed: {exc}",
            error=f"{type(exc).__name__}: {exc}",
        )
