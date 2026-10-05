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
import json
import os
import shutil
import sys
import traceback
from pathlib import Path


CLI_FIXED_PROFILE = "test"
CLI_REJECTED_FLAGS = frozenset({"--profile", "-profile", "-p"})


class CliStartupError(RuntimeError):
    """Отказ подъёма CLI-зависимости, который обязан быть виден сразу.

    Отдельный тип, а не ``ConfigurationError``: конфигурация тут ни при
    чём — ``enterprise_mcp`` объявлен и должен отвечать. Разные типы дают
    разные коды выхода (``2`` — ошибка конфигурации, ``1`` — зависимость
    не поднялась) и не дают смешивать два разных отказа в одном тексте.
    """


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

from loguru import logger
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
    _run_cli_repl(_create_cli_context(args), args)


def _run_patched(args: argparse.Namespace) -> None:
    """CLI-агент с холодным зеркалом сессий и workspace-хуками.

    Отличие от обычной ветки — только ``background_task_factory`` у REPL.
    Порядок рукопожатия, отказ запуска и коды выхода — общие намеренно:
    раньше ветка была отдельным телом и разошлась с обычной. В частности,
    ``asyncio.create_task(_run_patched_repl(...))`` вызывался ВНЕ работающего
    event loop и падал с ``RuntimeError: no running event loop``, из-за чего
    рукопожатие в этой ветке было недостижимо — а флаг ``--patched`` при
    этом оставался доступен из интерфейса.
    """
    _run_cli_repl(
        _create_cli_context(args),
        args,
        background_task_factory=lambda: asyncio.sleep(1),
    )


def _create_cli_context(args: argparse.Namespace):
    """Composition-контекст CLI. Одинаков для обеих ветвей запуска.

    Общий, а не продублированный: расхождение двух почти одинаковых тел
    и было источником дефекта ``--patched``.
    """
    from lib.core.application_context import ApplicationContext

    ctx = ApplicationContext.create(role='cli', 
        script_dir=script_dir_for_runtime(),
        workspace_dir=script_dir_for_runtime() / "workspace",
        session_override=args.session,
        storage_override=args.storage,
    )
    _configure_logging(ctx.settings)
    _migrate_cron_store(ctx.config)

    # ctx.agent уже содержит проектные хуки (SessionFileRedirectHook и др.) и
    # все runtime-patches (``assemble_outbound`` и пр.) уже применены через
    # ``ApplicationContext.create(role='cli', )`` → ``RuntimePatcher.apply_all()``.
    # Никаких дополнительных ``patch_*`` вызовов здесь быть не должно —
    # повторное применение приводит к double-wrap (см. openspec change
    # ``runtime-patcher-composition-cleanup``).
    return ctx


def _run_cli_repl(ctx, args: argparse.Namespace, *, background_task_factory=None) -> None:
    """Живой event loop: рукопожатие, транспорт журнала, затем REPL.

    ``asyncio.run`` открывает loop и блокирует до завершения ``body()``,
    поэтому всё, что обязано произойти ДО REPL, живёт внутри ``body()``, а
    не рядом с ним. Именно поэтому ``create_task`` перенесён внутрь
    ``asyncio.run``, а не вызывается из вызывающего кода: без работающего
    loop ``asyncio.create_task`` бросает ``RuntimeError: no running event
    loop``, а задача, созданная ДО ``asyncio.run``, не была бы выполнена
    вовсе — loop закрылся бы, не дождавшись её.

    Отказ рукопожатия поднимается наружу из ``body()``, поэтому REPL не
    поднимается ни в одной из ветвей, а ``ctx.stop()`` выполняется в
    ``finally`` любого исхода.
    """
    from lib.cli.console_loop import run_repl
    from lib.cli.display_config import DisplayConfig

    display = DisplayConfig.from_settings(
        ctx.config_service.settings_section("cli")
    )

    async def body() -> None:
        # Живой loop — единственное место, где можно поднять сессию MCP и
        # построить writer журнала. В ``ctx.start()`` его поднять нечем:
        # ``LoopCallRunner`` требует работающего event loop, и сессия MCP к
        # нему привязана. Без этого шага сервис остался бы в состоянии
        # «транспорт не выбран» и вёл бы локальный след вместо журнала
        # платформы. Рукопожатие идёт первым: без него отказ платформы
        # обнаружился бы посреди первого оборота (см.
        # ``_connect_enterprise_mcp``).
        await _connect_enterprise_mcp(ctx)
        # Второй процесс платформы — тот, из которого операции получает
        # МОДЕЛЬ. Шаг обязателен: без него семь объявленных операций в реестр
        # не попадут, и навык audit_analyzer будет обещать вызовы, которых
        # нет (см. _connect_mcp_provider).
        await _connect_mcp_provider(ctx)
        ctx.attach_log_transport()
        await run_repl(ctx.agent, ctx.config, session=args.session,
                       display=display,
                       background_task_factory=background_task_factory)

    ctx.start()
    try:
        asyncio.run(body())
    finally:
        ctx.stop()


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


async def _connect_mcp_provider(ctx) -> None:
    """Поднять MCP-серверы, объявленные для модели.

    Тот же контракт, что у рукопожатия клиента (``_connect_enterprise_mcp``),
    и та же нетерпимость к тихой деградации: без соединения семь операций
    ``mcp_enterprise_*`` не попадут в реестр, а ``audit_analyzer`` продолжит
    обещать их вызов — REPL поднялся бы, навыки были бы в промпте, вызовов
    не существовало бы. ``MCPProvider.connect()`` отказ не бросает, поэтому
    сверку объявленных серверов с соединёнными делает
    :func:`connect_mcp_provider`, а не сам провайдер.

    Вызывается до REPL, на живом loop, рядом с рукопожатием клиента.
    """
    from lib.services.mcp_provider import McpProviderUnavailable, connect_mcp_provider

    provider = getattr(ctx, "mcp_provider", None)
    if provider is None:
        console.print(
            "[yellow]○[/yellow] MCP-серверы для модели не объявлены "
            "(tools.mcpServers пуст) — mcp_enterprise_* модели не достаются"
        )
        return

    try:
        servers = await connect_mcp_provider(provider)
    except McpProviderUnavailable as exc:
        console.print(f"[red]✗ MCP-серверы для модели: НЕ ПОДНЯЛИСЬ[/red] — {exc}")
        console.print(
            "[red]  проверьте: config.json -> tools.mcpServers.enterprise, "
            "NANOBOT_ENTERPRISE_MCP_PROFILE, доступность платформы[/red]"
        )
        logger.error("MCP-серверы для модели не поднялись: %s", exc)
        raise CliStartupError(f"MCP-серверы для модели: {exc}") from exc
    console.print(
        f"[green]✓[/green] MCP-серверы для модели: подняты ({', '.join(servers)})"
    )


async def _connect_enterprise_mcp(ctx) -> None:
    """Поднять сессию ``enterprise-mcp`` и убедиться, что сервер отвечает.

    Тот же контракт, что и в gateway (``gateway._connect_enterprise_mcp``):
    сервер нужен всегда — три входа к данным живут в нём, — поэтому
    «платформа не отвечает» обязано обнаруживаться на старте, а не посреди
    оборота пользователя. Разница только в способе подъёма и в подаче отказа
    (см. ниже).

    Раньше подъём был ленивым: сессия создавалась при первом вызове tool'а,
    и первый же реальный вопрос падал вместо того, чтобы честно упасть
    на старте. Пользователь видел тогда ошибку посреди диалога и не мог
    отличить «платформа лежит» от «агент сломался».

    Проверка — ``list_operations()``: он поднимает сессию и делает
    discovery, второго механизма рукопожатия нет намеренно.

    Успех рукопожатия — не конец проверки: за ним идёт сверка профильных
    имён таблиц (``_verify_platform_tables``). CLI прибит к профилю
    ``test``, поэтому расхождение оверлея на нём — самая вероятная
    конфигурационная ошибка, и ловить её обязана именно startup-проверка.

    Отказ не проглатывается: поднимается ``CliStartupError``, который
    ``main()`` превращает в ``stderr`` + ненулевой код выхода. Тихая
    деградация была бы единственным неправильным вариантом — REPL поднялся
    бы, первый вопрос дошёл бы до агента, и tool'ы отвечали бы ошибкой.

    Подача отказа рассчитана на интерактивную консоль (в отличие от
    gateway, где тот же отказ уходит в лог ``GatewayRunner``): причина
    печатается одной читаемой строкой без стек-трейса, а подсказка — что
    проверять — идёт отдельной строкой. Полный трейс доступен по
    ``NANOBOT_CLI_TRACEBACK=1``, но по умолчанию пользователю не сыпется.

    Вызывается внутри живого event loop: сессия stdio привязана к loop, и
    поднять её синхронно до ``asyncio.run`` нельзя.

    ``None`` — раздел ``enterprise_mcp`` выключен: сервера нет по решению
    оператора, и это не повод падать (потребители сообщат структурной
    ошибкой). Так же, как в gateway.
    """
    client = getattr(ctx, "enterprise_mcp", None)
    if client is None:
        console.print(
            "[yellow]○[/yellow] enterprise-mcp: не объявлен "
            "(раздел gateway.agent.enterprise_mcp выключен) — "
            "инструменты данных ответят структурной ошибкой"
        )
        return

    # Импорт рядом с ``_verify_platform_tables``, который берёт оттуда же
    # ``CallIdentity``: клиент платформы нашим входом не поднимается, и
    # его импорт на уровне модуля стал бы платой за каждый запуск.
    from lib.services.enterprise_mcp_client import EnterpriseMcpPortBusy

    try:
        operations = await client.list_operations()
        console.print(f"[green]✓[/green] {client.presence_line(len(operations))}")
        await _verify_platform_tables(ctx, client)
    except ConfigurationError as exc:
        # Расхождение профиля — это ошибка конфигурации, а не «сервер не
        # отвечает»: повтор и рестарт её не исправят. Тип сохраняется, чтобы
        # ``main()`` дал тот же код выхода ``2``, что и gateway.
        console.print(f"[red]✗ enterprise-mcp: КОНФИГУРАЦИЯ[/red] — {exc}")
        logger.error("enterprise-mcp: ошибка конфигурации: %s", exc)
        raise
    except EnterpriseMcpPortBusy as exc:
        # Порт платформы занят — решение о подъёме не принято, и ни
        # повтор, ни рестарт его не исправят. Отдельная ветка, потому
        # что подсказка здесь другая: устранять надо держателя порта,
        # а не «platform.json и .secrets.env».
        console.print(f"[red]✗ enterprise-mcp: ПОРТ ЗАНЯТ[/red] — {exc}")
        if exc.holder_pid:
            console.print(
                f"[red]  держатель: pid {exc.holder_pid}. "
                f"Остановите его (Ctrl+C в его окне) и запустите снова.[/red]"
            )
        logger.error("enterprise-mcp: занятый порт: %s", exc)
        raise CliStartupError(f"enterprise-mcp: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 — причина важнее типа
        # Одна строка причины + подсказка, без стек-трейса: CLI интерактивен,
        # и пользователю нужен вердикт «не поднялась платформа», а не дамп.
        console.print(
            f"[red]✗ enterprise-mcp: НЕ ПОДНЯЛСЯ[/red] — {type(exc).__name__}: {exc}"
        )
        console.print(
            "[red]  проверьте: mcp-platform/platform.json, "
            "mcp-platform/.secrets.env, доступность python и БД[/red]"
        )
        # Причина уходит и в лог: stderr-строка в консоли не заменяет
        # запись в журнал запуска, откуда инцидент потом и разбирают.
        logger.error("enterprise-mcp не поднялся на старте CLI: %s: %s",
                     type(exc).__name__, exc)
        raise CliStartupError(f"enterprise-mcp: {type(exc).__name__}: {exc}") from exc


async def _verify_platform_tables(ctx, client) -> None:
    """Сверить профильные имена таблиц агента и платформы на старте CLI.

    Решение — ПАРИТЕТ с gateway, а не сознательный отказ от сверки. CLI
    жёстко прибит к профилю ``test`` (``CLI_FIXED_PROFILE``), а оверлей
    профиля объявлен в ДВУХ файлах (``profiles/test.jsonc`` агента и
    ``mcp-platform/platform.json → profiles.test``): правка одного без
    другого даёт ровно тот дефект, который сверка ловит, — агент опрашивает
    ``agent_conversation_messages_test``, а платформа пишет в боевые
    таблицы. Заметить это можно только по содержимому журнала, поэтому
    проверка обязана быть на старте. Отказ — ``ConfigurationError``, а не
    ``CliStartupError``: повтор его не исправит, и код выхода обязан остаться
    ``2``, чтобы расхождение оверлея не выглядело как «платформа не
    отвечает».

    Отличие от gateway — одна проба ``data.schema_check`` вместо трёх по
    capability. Сводка «индексы/скрипты» перед REPL в интерактивной консоли
    — шум, а список таблиц отдаёт ровно ``data.schema_check``, то есть ровно та
    проба, ради которой она и делается.

    Правило сверки НЕ копируется, а импортируется у владельца контракта
    (``gateway``): второе определение разошлось бы с первым при первой же
    правке, а расхождение двух копий и было причиной, по которой CLI от
    сверки отставал.
    """
    from gateway import _schema_line, _verify_platform_table_alignment
    from lib.services.enterprise_mcp_client import CallIdentity

    # Идентичность обязательна для платформы (require_call_meta), но это не
    # пользовательский оборот: служебная, чтобы health-проба не создавала
    # запись в agent_question_runs от имени живого запроса.
    identity = CallIdentity(
        session_id="startup:cli", user_id="startup:health"
    ).with_request_id("startup-enterprise-mcp-health")

    platform_tables: list[str] | None = None
    try:
        raw = await asyncio.wait_for(
            client.call("data.schema_check", arguments={}, identity=identity),
            timeout=20.0,
        )
        payload = json.loads(raw)
        platform_tables = list(payload.get("tables") or [])
        line = _schema_line(payload)
    except asyncio.TimeoutError:
        # Проба не ответила — это неполнота capability, а не отказ запуска:
        # платформа-то ответила. Сверка ниже получит ``None`` и промолчит.
        line = "[red]проба не ответила за 20 с[/red]"
    except ConfigurationError:
        # Отказ конфигурации от самой пробы — тоже отказ конфигурации.
        # Превращать его в строку сводки значило бы потерять и код выхода 2,
        # и саму причину.
        raise
    except Exception as exc:  # noqa: BLE001 — причина важнее типа
        line = f"[red]{type(exc).__name__}: {exc}[/red]"
    console.print(f"    [dim]·[/dim] data     {line}")

    _verify_platform_table_alignment(ctx, platform_tables)


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

    Вторая ветка — ``CliStartupError`` (не поднялась зависимость, например
    ``enterprise-mcp``). Это НЕ ошибка конфигурации, поэтому свой код
    выхода ``1``; консоль к этому моменту уже получила и вердикт, и
    подсказку из ``_connect_enterprise_mcp``, здесь дописывается только
    строка для ``stderr``. Стек-трейс по умолчанию НЕ печатается: CLI
    интерактивен, и пользователю нужен читаемый отказ, а не дамп. Полный
    трейс — по ``NANOBOT_CLI_TRACEBACK=1`` (тот же env-флаг, которым
    поднимается уровень логов).
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
    except CliStartupError as exc:
        sys.stderr.write(f"FATAL: {exc}\n")
        if os.environ.get("NANOBOT_CLI_TRACEBACK"):
            traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
