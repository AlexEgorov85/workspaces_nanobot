"""Runtime-context providers для skill'а ``audit_analyzer``.

Skill публикует в system prompt:

* список predefined-скриптов из реестра ``public.agent_predefined_scripts``
  (тег ``source='audit_predefined_scripts'``);
* схему БД в формате LLM-промпта (тег ``source='audit_db_schema'``).

Регистрация: ``register_audit_runtime_providers(agent, settings)`` из
``lib/core/application_context.py`` (вызывается в ``start()`` если
skill включён через ``skills.audit_analyzer.*``).

Эти провайдеры — часть skill package'а и не зависят от runtime tools
(см. TARGET_ARCHITECTURE.md §22.2 — Skill не импортирует Tool).
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from workspace.skills.audit_analyzer.scripts.skill_config import (
    build_cache_provider,
    get_db_schema,
)


_scripts_cache: list[dict[str, Any]] | None = None
_schema_cache: str | None = None
_schema_cache_meta: tuple[str, Any] | None = None


def invalidate_scripts_cache() -> None:
    """Сбросить кеш списка скриптов (после reload реестра)."""
    global _scripts_cache
    _scripts_cache = None


def invalidate_schema_cache() -> None:
    """Сбросить кеш схемы (после миграций)."""
    global _schema_cache, _schema_cache_meta
    _schema_cache = None
    _schema_cache_meta = None


# ---------------------------------------------------------------------
# Provider 1: predefined scripts list
# ---------------------------------------------------------------------


def _load_scripts_list() -> list[dict[str, Any]]:
    """Загрузить список скриптов из реестра через skill-internal путь."""
    global _scripts_cache
    if _scripts_cache is not None:
        return _scripts_cache
    try:
        from workspace.skills.audit_analyzer.scripts import (
            db_loader,
            predefined,
        )
        provider = build_cache_provider()
        if not provider.open_cache():
            raise RuntimeError(
                "SQL-кэш не готов: не удалось открыть DuckDB-кэш "
                "(файл создаёт/обновляет gateway — AuditSyncService)."
            )
        db_loader.set_provider(provider)
        scripts = predefined.list_all_scripts()
    except Exception as exc:
        logger.warning(
            "audit_analyzer skill: failed to load scripts registry: {}", exc
        )
        return []
    _scripts_cache = list(scripts)
    return _scripts_cache


def _format_scripts_block(scripts: list[dict[str, Any]]) -> str:
    if not scripts:
        return ""
    lines = ["Доступные predefined SQL-скрипты (см. SKILL.md):"]
    for s in scripts:
        name = s.get("name", "?")
        desc = (s.get("description") or "").strip()
        params = s.get("parameters") or []
        params_str = ", ".join(params) if params else "(без параметров)"
        lines.append(f"- {name}: {desc} | параметры: {params_str}")
    return "\n".join(lines)


async def predefined_scripts_provider(request_ctx: Any) -> Any:
    """Runtime-context provider: список predefined-скриптов в system prompt.

    Контракт: ``async (RequestContext) -> RuntimeContextBlock | None``.
    """
    scripts = _load_scripts_list()
    if not scripts:
        return None
    try:
        from nanobot.runtime_context import wrap_runtime_context_lines
    except ImportError:
        return None
    body = _format_scripts_block(scripts)
    return RuntimeContextBlock(
        source="audit_predefined_scripts",
        content=wrap_runtime_context_lines(body.splitlines()),
    )


# ---------------------------------------------------------------------
# Provider 2: DB schema
# ---------------------------------------------------------------------


def _load_schema_block() -> str:
    """Загрузить схему БД (с кешем)."""
    global _schema_cache, _schema_cache_meta
    if _schema_cache is not None and _schema_cache_meta is not None:
        return _schema_cache
    try:
        provider = build_cache_provider()
        if not provider.open_cache():
            raise RuntimeError("SQL-кэш не готов")
        schema = provider.get_schema()
        from lib.utils.sql_safety import format_schema
        block = format_schema(schema)
    except Exception as exc:
        logger.warning(
            "audit_analyzer skill: failed to load DB schema: {}", exc
        )
        return ""
    _schema_cache = block
    _schema_cache_meta = (get_db_schema.__name__, None)
    return _schema_cache


async def db_schema_provider(request_ctx: Any) -> Any:
    """Runtime-context provider: схема БД в формате LLM-промпта.

    Контракт: ``async (RequestContext) -> RuntimeContextBlock | None``.
    """
    block = _load_schema_block()
    if not block:
        return None
    try:
        from nanobot.runtime_context import wrap_runtime_context_lines
    except ImportError:
        return None
    return RuntimeContextBlock(
        source="audit_db_schema",
        content=wrap_runtime_context_lines(block.splitlines()),
    )


# ---------------------------------------------------------------------
# Registration entry-point
# ---------------------------------------------------------------------


def register_audit_runtime_providers(agent: Any, settings: Any) -> None:
    """Зарегистрировать runtime-context providers на agent'е.

    Вызывается из ``ApplicationContext.start()`` если skill включён.
    """
    if agent is None:
        return
    try:
        rc_registry = getattr(agent, "runtime_context", None)
    except Exception:
        rc_registry = None
    if rc_registry is None:
        logger.debug(
            "audit_analyzer skill: agent has no runtime_context registry; "
            "skipping provider registration"
        )
        return
    try:
        rc_registry.register(predefined_scripts_provider)
        rc_registry.register(db_schema_provider)
    except AttributeError:
        try:
            rc_registry.add(predefined_scripts_provider)
            rc_registry.add(db_schema_provider)
        except Exception as exc:
            logger.warning(
                "audit_analyzer skill: cannot register runtime_context "
                "providers: {}", exc
            )


def unregister_audit_runtime_providers(agent: Any) -> None:
    """Снять регистрацию providers (для graceful shutdown)."""
    if agent is None:
        return
    rc_registry = getattr(agent, "runtime_context", None)
    if rc_registry is None:
        return
    for fn_name in ("unregister", "remove"):
        fn = getattr(rc_registry, fn_name, None)
        if fn is None:
            continue
        for provider in (predefined_scripts_provider, db_schema_provider):
            try:
                fn(provider)
            except Exception:
                pass


# Late import для типа RuntimeContextBlock — он доступен только если
# nanobot импортирован (а он импортируется всегда в рабочем runtime).
try:
    from nanobot.runtime_context import RuntimeContextBlock
except ImportError:
    class RuntimeContextBlock:  # type: ignore[no-redef]
        """Fallback для окружений, где nanobot не импортирован (тесты)."""
        def __init__(self, *, source: str, content: str) -> None:
            self.source = source
            self.content = content