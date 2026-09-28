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
            (``Tool.enabled(ctx) == False``).
        duplicate: имена tool'ов, не зарегистрированных из-за конфликта
            имён (в ``agent.tools`` уже есть tool с таким именем).
        failed: имена tool'ов, упавших на ``Tool.create()`` или
            ``agent.tools.register`` (с ``logger.exception`` трассой).
        detail: presentation/diagnostic строка для баннера логов.
            Формат совместим с
            ``lib.services.runtime_inventory.parse_project_tools_detail``:
            ``[INTERNAL_FAILED] N project tools registered: a, b;
            M disabled by config: c; K already registered: d;
            J failed: e``. Маркер ``[INTERNAL_FAILED]`` ставится
            только если ``failed`` непустой.
    """

    registered: list[str] = field(default_factory=list)
    disabled: list[str] = field(default_factory=list)
    duplicate: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    detail: str = ""


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


def _build_tool_context(agent: Any, settings: Any, cache_store: Any,
                        db_logging_service: Any) -> Any:
    """Собрать ``ToolContext`` из атрибутов ``AgentLoop``.

    Тот же набор kwargs, что был в ``RuntimePatcher.patch_project_tools``
    (см. ``runtime_patcher.py:2392-2417`` в pre-change версии):
    ``config`` / ``workspace`` / ``bus`` / ``subagent_manager`` /
    ``cron_service`` / ``exec_session_manager`` / ``sessions`` /
    ``file_state_store`` / ``provider_snapshot_loader`` /
    ``image_generation_provider_configs`` / ``timezone`` /
    ``workspace_sandbox`` / ``runtime_control``.

    DI-расширения (``agent`` / ``settings`` / ``cache_store`` /
    ``db_logging_service``) прокидываются через ``setattr`` —
    ``ToolContext`` остаётся frozen=False в nanobot 0.3.5.
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
    if cache_store is not None:
        ctx._cache_store_ref = cache_store
    if db_logging_service is not None:
        ctx._db_logging_service = db_logging_service
    return ctx


def register_project_tools(
    agent: Any,
    workspace_dir: Any,
    *,
    settings: Any = None,
    cache_store: Any = None,
    db_logging_service: Any = None,
) -> ProjectToolsLoadResult:
    """Discover + DI + register project tools из ``<workspace>/tools/``.

    Best-effort с частичным успехом: ошибка одного tool (``enabled`` /
    ``create`` / ``register``) не отменяет уже зарегистрированные.

    Args:
        agent: ``AgentLoop`` (target ``agent.tools.register``).
        workspace_dir: ``Path`` — корень workspace, в нём лежит ``tools/``.
        settings: ``SETTINGS`` (опционально) — для ``ctx._settings_ref``.
        cache_store: ``CacheProvider`` (опционально) — DI в tool'ы с
            ``set_provider`` / ``set_connection_factory``.
        db_logging_service: ``DbLoggingService`` (опционально) — для
            ``ctx._db_logging_service``.

    Returns:
        ``ProjectToolsLoadResult`` со структурными полями ``registered`` /
        ``disabled`` / ``duplicate`` / ``failed`` и ``detail``-строкой
        для баннера логов (формат совместим с
        ``runtime_inventory.parse_project_tools_detail``).
    """
    from loguru import logger

    if agent is None:
        return ProjectToolsLoadResult(detail="agent is None")

    try:
        tools_dir = Path(workspace_dir) / "tools"
        if not tools_dir.is_dir():
            return ProjectToolsLoadResult(detail="workspace/tools not found — skip")

        candidates = _discover(workspace_dir)
        if not candidates:
            return ProjectToolsLoadResult(detail="no project tools found")

        ctx = _build_tool_context(agent, settings, cache_store, db_logging_service)

        registered: list[str] = []
        skipped_disabled: list[str] = []
        skipped_duplicate: list[str] = []
        failed: list[str] = []

        for cls in candidates:
            try:
                if not cls.enabled(ctx):
                    skipped_disabled.append(cls.__name__)
                    continue
                tool = cls.create(ctx)
                if agent.tools.get(tool.name) is not None:
                    skipped_duplicate.append(tool.name)
                    continue
                if cache_store is not None:
                    if hasattr(tool, "set_provider"):
                        try:
                            tool.set_provider(cache_store)
                        except Exception:
                            logger.exception(
                                "set_provider failed for {}", cls.__name__,
                            )
                    elif hasattr(tool, "set_connection_factory"):
                        try:
                            tool.set_connection_factory(
                                getattr(cache_store, "get_duckdb_connection", None)
                                or getattr(cache_store, "connect", None),
                            )
                        except Exception:
                            logger.exception(
                                "set_connection_factory failed for {}",
                                cls.__name__,
                            )
                agent.tools.register(tool)
                registered.append(tool.name)
            except Exception:
                logger.exception("Failed to register {}", cls.__name__)
                failed.append(cls.__name__)

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
        return ProjectToolsLoadResult(detail=f"patch failed: {exc}")
