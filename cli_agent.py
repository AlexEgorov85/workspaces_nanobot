"""cli_agent.py — терминальный режим работы агента (REPL).

Тонкий оркестратор: загрузка конфига и сервисов — в ``ApplicationContext``
(включая auto-scan проектных хуков из ``workspace/hooks/``),
REPL/typewriter — в ``lib.cli.console_loop``. Этот файл — CLI-аргументы,
миграция cron, preload аудит-кеша навыка, vanilla/patched-режимы.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
from pathlib import Path

# Кросс-платформенная UTF-8 кодировка для ВСЕХ exec-подпроцессов (см. gateway.py).
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from rich.console import Console

from lib.cli.console_loop import run_repl
from lib.cli.display_config import DisplayConfig
from lib.core.application_context import ApplicationContext

_SCRIPT_DIR = Path(__file__).parent
_WORKSPACE_DIR = _SCRIPT_DIR / "workspace"

# Добавляем корень проекта и workspace в sys.path (для lib.* / workspace.utils.*).
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
        print_llm_calls=True,
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
        print_llm_calls=True,
    )
    _configure_logging(ctx.settings)
    _migrate_cron_store(ctx.config)

    # ctx.agent уже содержит проектные хуки (SessionFileRedirectHook и др.) —
    # ApplicationContext.create() сделал auto-scan и пересобрал AgentLoop.
    # Здесь только финальный семантический патч _assemble_outbound.
    ctx.runtime_patcher.patch_assemble_outbound(ctx.agent, ctx.tool_audit_hook)

    asyncio.create_task(_run_patched_repl(ctx, args))


def _run_patched_repl(ctx: ApplicationContext, args: argparse.Namespace) -> None:
    """REPL для patched-режима."""
    import asyncio

    async def bg():
        await run_repl(ctx.agent, ctx.config, session=args.session,
                       display=DisplayConfig.from_settings(
                           ctx.config_service.settings_section("cli")),
                       background_task_factory=lambda: asyncio.sleep(1))

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
    from lib.utils.logging_utils import configure_loguru

    configure_loguru(level, env_var="NANOBOT_LOG_LEVEL")


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
