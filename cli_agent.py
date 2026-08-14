"""cli_agent.py — терминальный режим работы агента (REPL).

Тонкий оркестратор: загрузка конфига и сервисов — в ``ApplicationContext``,
REPL/typewriter — в ``lib.cli.console_loop``, авто-сканирование хуков — в
``lib.cli.hook_loader``. Этот файл — CLI-аргументы, миграция cron,
preload аудит-кеша навыка, vanilla/patched-режимы.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
from pathlib import Path

from rich.console import Console

from lib.cli.console_loop import run_repl
from lib.cli.display_config import DisplayConfig
from lib.cli.hook_loader import scan_and_register
from lib.core.application_context import ApplicationContext

_SCRIPT_DIR = Path(__file__).parent
_WORKSPACE_DIR = _SCRIPT_DIR / "workspace"
_HOOKS_DIR = _WORKSPACE_DIR / "hooks"

# Добавляем корень проекта и workspace в sys.path (для hooks.* импортов).
sys.path.insert(0, str(_SCRIPT_DIR))
sys.path.insert(0, str(_WORKSPACE_DIR))

console = Console()


def main() -> None:
    args = _parse_args()
    if args.patched:
        _run_patched(args)
    else:
        _run_vanilla(args)


def _run_vanilla(args: argparse.Namespace) -> None:
    """Стандартный CLI-агент (как ``nanobot agent``). Без доработок."""
    ctx = ApplicationContext.create(
        script_dir=_SCRIPT_DIR,
        workspace_dir=_WORKSPACE_DIR,
        enable_db_logging=True,
        enable_audit=False,
        enable_cron=True,
        session_override=args.session,
    )
    _configure_logging(ctx.settings)
    _migrate_cron_store(ctx.config)
    ctx.start()
    try:
        display = DisplayConfig.from_settings(
            ctx.config_service.settings_section("cli")
        )
        asyncio.run(run_repl(ctx.agent, ctx.config, session=args.session, display=display))
    finally:
        ctx.stop()


def _run_patched(args: argparse.Namespace) -> None:
    """CLI-агент с PGSessionManager и workspace-хуками."""
    ctx = ApplicationContext.create(
        script_dir=_SCRIPT_DIR,
        workspace_dir=_WORKSPACE_DIR,
        enable_db_logging=True,
        enable_audit=False,
        enable_cron=True,
        storage_override=args.storage,
        session_override=args.session,
    )
    _configure_logging(ctx.settings)
    _migrate_cron_store(ctx.config)

    hooks, _ = scan_and_register(_HOOKS_DIR, _WORKSPACE_DIR)
    if ctx.tool_audit_hook not in hooks:
        hooks.append(ctx.tool_audit_hook)
    ctx.agent = ctx.agent.__class__.from_config(
        ctx.config,
        ctx.bus,
        session_manager=ctx.session_manager,
        cron_service=__get_cron(ctx),
        hooks=hooks,
    )
    ctx.runtime_patcher.patch_assemble_outbound(ctx.agent, ctx.tool_audit_hook)

    asyncio.create_task(_run_patched_repl(ctx, args))


def _run_patched_repl(ctx: ApplicationContext, args: argparse.Namespace) -> None:
    """REPL для patched-режима (фоновая подгрузка кеша + REPL)."""
    import asyncio
    from lib.services.preload_service import PreloadService

    preload = PreloadService(settings=ctx.settings)

    async def bg():
        reload_task = await preload.start_audit_cache_tasks(ctx.config)
        try:
            await run_repl(ctx.agent, ctx.config, session=args.session,
                           display=DisplayConfig.from_settings(
                               ctx.config_service.settings_section("cli")),
                           background_task_factory=lambda: asyncio.sleep(1))
        finally:
            await preload.stop_tasks(reload_task)

    ctx.start()
    try:
        asyncio.run(bg())
    finally:
        ctx.stop()


def __get_cron(_ctx: ApplicationContext):
    """CronService уже создан в ApplicationContext — возвращаем None,
    потому что AgentFactory уже подключила его из hooks."""
    return None


def _configure_logging(settings) -> None:
    """loguru из cli.log_level."""
    cli = settings.get("cli") if isinstance(settings, dict) else getattr(settings, "cli", None)
    level = "WARNING"
    if cli is not None:
        if isinstance(cli, dict):
            level = cli.get("log_level", "WARNING")
        else:
            level = getattr(cli, "log_level", "WARNING")
    os.environ.setdefault("NANOBOT_LOG_LEVEL", str(level))
    try:
        from loguru import logger
        logger.remove()
        logger.add(sys.stderr, level=level)
    except Exception:
        pass


def _migrate_cron_store(config) -> None:
    """Перенос cron-задач из глобальной cron-директории nanobot в workspace."""
    try:
        from nanobot.config.paths import get_cron_dir  # type: ignore
        legacy = get_cron_dir() / "jobs.json"
        new = config.workspace_path / "cron" / "jobs.json"
        if legacy.is_file() and not new.exists():
            new.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy), str(new))
    except Exception:
        pass


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="nanobot CLI agent")
    parser.add_argument("--patched", "-P", action="store_true", default=False)
    parser.add_argument("--storage", "-S", type=str, default="auto",
                        choices=("auto", "file", "postgres"))
    parser.add_argument("--session", "-s", type=str, default=None)
    return parser.parse_args()


if __name__ == "__main__":
    main()
