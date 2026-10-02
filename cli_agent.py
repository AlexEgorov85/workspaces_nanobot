"""cli_agent.py — терминальный режим работы агента (REPL).

Тонкий оркестратор: загрузка конфига и сервисов — в ``ApplicationContext``
(включая auto-scan проектных хуков из ``workspace/hooks/``),
REPL/typewriter — в ``lib.cli.console_loop``. Этот файл — CLI-аргументы,
миграция cron, preload аудит-кеша навыка, vanilla/patched-режимы.

CLI = фиксированный профиль ``test`` (Stage F из
``unify-cli-gateway-architecture``). ``--profile`` больше НЕ принимается;
передача → ``ConfigurationError``. CLI MUST NOT поднимать env-based
override (см. design D8). CLI — локальный test/dev entrypoint, не
production deployment interface; для production-dep используется
``gateway.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
from pathlib import Path


CLI_FIXED_PROFILE = "test"
CLI_REJECTED_FLAGS = frozenset({"--profile", "-profile", "-p"})


from config import ConfigurationError  # noqa: E402 — module-level import is safe

from lib.utils.windows_terminal import ensure_console_colors

# Legacy Windows-консоль без ENABLE_VIRTUAL_TERMINAL_PROCESSING печатает
# ANSI как мусор "?[2m→ LLM: ...?[0m". ensure_console_colors() включает VT,
# а если хост его не поддерживает — отключает цвета (NO_COLOR + ANSI-фильтр
# на sys.stdout) и возвращает текст предупреждения. Вызывается на импорте
# ДО создания первого Console(), т.к. no_color читается в Console.__init__.
_WINDOWS_COLOR_WARNING = ensure_console_colors()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Парсинг argv. ``--profile`` НЕ принимается (CLI = фиксированный
    profile ``test``, см. design D8). Если передан — ``ConfigurationError``
    с понятным сообщением.

    Whitelist и required-валидация делаются здесь, а не делегируются
    ``argparse.error``/``choices=`` — иначе ``SystemExit(2)`` от argparse
    минует ``ConfigurationError`` boundary, нарушая Error Lifecycle
    Contract (см. docs/PROFILES.md и openspec/specs/configuration/profiles).
    """
    parser = argparse.ArgumentParser(description="nanobot CLI agent", add_help=False)
    parser.add_argument("--patched", "-P", action="store_true", default=False)
    parser.add_argument("--storage", "-S", type=str, default="auto",
                        choices=("auto", "file", "postgres"))
    parser.add_argument("--session", "-s", type=str, default=None)
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Smoke-режим: инициализирует SETTINGS, "
             "печатает баннер + runtime-таблицу, выходит 0. "
             "Только для D.2 integration-тестов.",
    )
    parser.add_argument("--help", "-h", action="store_true")
    if argv is None:
        argv = sys.argv[1:]
    if "--help" in argv or "-h" in argv:
        parser.print_help()
        sys.exit(0)

    # Stage F: явный reject ``--profile`` до argparse (после будет
    # путать с hidden args). Учитываем и form с ``=`` (``--profile=test``,
    # ``-p=test``) — argv-элемент может начинаться с rejected.
    for arg in argv:
        for rejected in CLI_REJECTED_FLAGS:
            if arg == rejected or arg.startswith(rejected + "="):
                raise ConfigurationError(
                    f"cli_agent.py: {rejected} is not supported "
                    f"(CLI uses fixed profile={CLI_FIXED_PROFILE!r}; "
                    "use gateway.py for prod deployment)"
                )

    args, _unknown = parser.parse_known_args(argv)

    # ``profile`` фиксирован — НЕ передаётся в lifecycle-gate.
    args.profile = CLI_FIXED_PROFILE
    return args


# Кросс-платформенная UTF-8 кодировка для ВСЕХ exec-подпроцессов (см. gateway.py).
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from rich.console import Console


def _entrypoint_main(args: argparse.Namespace) -> None:
    """Startup + application body, поднимает ``ConfigurationError`` на ошибках.

    Граница ``ConfigurationError → exit 2`` живёт в ``main()`` — здесь
    нет ``sys.exit(2)`` (см. design.md Decision 2 unification).
    """
    import config as _cfg
    # CLI = фиксированный test-профиль (Stage F, design D8). НЕ
    # читаем из окружения и НЕ принимаем --profile.
    _cfg._initialize_settings(profile=CLI_FIXED_PROFILE)

    from lib.cli.console_loop import run_repl
    from lib.cli.display_config import DisplayConfig
    from lib.core.application_context import ApplicationContext

    if _WINDOWS_COLOR_WARNING:
        console.print(f"[yellow]{_WINDOWS_COLOR_WARNING}[/yellow]")

    console.print(
        f"[bold]Starting nanobot cli[/bold] · profile={CLI_FIXED_PROFILE}"
    )

    # Smoke-режим: печатает баннер + runtime-таблицу, выходит 0
    # без открытия REPL/миграции cron/auto-scan хуков.
    if args.smoke:
        from lib.utils.project_version import project_version
        from nanobot.cli.commands import __version__

        cfg = ApplicationContext.create(role='cli', 
            script_dir=script_dir_for_runtime(),
            workspace_dir=script_dir_for_runtime() / "workspace",
        )
        runtime_table = cfg.settings["logging"]["db"]["table_name"]
        console.print(
            f"nanobot cli smoke · project v{project_version()} · "
            f"nanobot {__version__} · profile={CLI_FIXED_PROFILE} · "
            f"logging.db.table_name={runtime_table}"
        )
        console.print("OK_SMOKE_COMPLETE")
        cfg.stop()
        return

    if args.patched:
        _run_patched(args)
    else:
        _run_vanilla(args)


def _run_vanilla(args: argparse.Namespace) -> None:
    """Стандартный CLI-агент (как ``nanobot agent``). Без доработок."""
    from lib.cli.console_loop import run_repl
    from lib.cli.display_config import DisplayConfig
    from lib.core.application_context import ApplicationContext

    ctx = ApplicationContext.create(role='cli', 
        script_dir=script_dir_for_runtime(),
        workspace_dir=script_dir_for_runtime() / "workspace",
        session_override=args.session,
        storage_override=args.storage,
    )
    _configure_logging(ctx.settings)
    _migrate_cron_store(ctx.config)
    ctx.start()
    try:
        display = DisplayConfig.from_settings(
            ctx.config_service.settings_section("cli")
        )

        async def repl() -> None:
            # Транспорт журнала подключается внутри живого loop. В
            # ``ctx.start()`` его поднять нечем: ``LoopCallRunner`` требует
            # работающего event loop, и сессия MCP к нему привязана. Без
            # этого шага сервис остался бы в состоянии «транспорт не выбран»
            # и вёл бы локальный след вместо журнала платформы.
            ctx.attach_log_transport()
            await run_repl(ctx.agent, ctx.config, session=args.session, display=display)

        asyncio.run(repl())
    finally:
        ctx.stop()


def _run_patched(args: argparse.Namespace) -> None:
    """CLI-агент с PGSessionManager и workspace-хуками."""
    from lib.cli.console_loop import run_repl
    from lib.cli.display_config import DisplayConfig
    from lib.core.application_context import ApplicationContext

    ctx = ApplicationContext.create(role='cli', 
        script_dir=script_dir_for_runtime(),
        workspace_dir=script_dir_for_runtime() / "workspace",
        storage_override=args.storage,
        session_override=args.session,
    )
    _configure_logging(ctx.settings)
    _migrate_cron_store(ctx.config)

    # ctx.agent уже содержит проектные хуки (SessionFileRedirectHook и др.) и
    # все runtime-patches (``assemble_outbound`` и пр.) уже применены через
    # ``ApplicationContext.create(role='cli', )`` → ``RuntimePatcher.apply_all()``.
    # Никаких дополнительных ``patch_*`` вызовов здесь быть не должно —
    # повторное применение приводит к double-wrap (см. openspec change
    # ``runtime-patcher-composition-cleanup``).

    asyncio.create_task(_run_patched_repl(ctx, args))


def _run_patched_repl(ctx, args: argparse.Namespace) -> None:
    """REPL для patched-режима."""
    from lib.cli.console_loop import run_repl
    from lib.cli.display_config import DisplayConfig

    async def bg():
        # Живой loop — единственное место, где можно построить writer
        # журнала; см. комментарий в ``_run_vanilla``.
        ctx.attach_log_transport()
        await run_repl(ctx.agent, ctx.config, session=args.session,
                       display=DisplayConfig.from_settings(
                           ctx.config_service.settings_section("cli")),
                       background_task_factory=lambda: asyncio.sleep(1))

    ctx.start()
    try:
        asyncio.run(bg())
    finally:
        ctx.stop()


def __get_cron(_ctx):
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


_SCRIPT_DIR: Path | None = None


def script_dir_for_runtime() -> Path:
    """Абсолютный путь к каталогу ``cli_agent.py``.

    Ленивая инициализация, чтобы ``import cli_agent`` оставался
    чистым от side-effects (контракт ``application entrypoint``).
    """
    global _SCRIPT_DIR
    if _SCRIPT_DIR is None:
        _SCRIPT_DIR = Path(__file__).resolve().parent
    return _SCRIPT_DIR


console = Console()


def main(argv: list[str] | None = None) -> int:
    """Точка входа cli_agent с единым error-lifecycle boundary.

    Аналогично ``gateway.main`` — ловит ``ConfigurationError`` и
    превращает в ``sys.stderr.write + return 2``. Один contract для
    всех трёх entrypoint'ов (см. design.md Decision 2 unification).
    """
    try:
        args = _parse_args(argv)
    except ConfigurationError as exc:
        sys.stderr.write(f"FATAL: {exc}\n")
        return 2

    script_dir = script_dir_for_runtime()
    workspace_dir = script_dir / "workspace"

    # Добавляем корень проекта и workspace в sys.path (для lib.* / workspace.utils.*).
    if str(script_dir) not in sys.path:
        sys.path.insert(0, str(script_dir))
    if str(workspace_dir) not in sys.path:
        sys.path.insert(0, str(workspace_dir))

    try:
        _entrypoint_main(args)
    except ConfigurationError as exc:
        sys.stderr.write(f"FATAL: {exc}\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
