"""gateway.py — серверный режим работы агента.

Тонкий оркестратор: вся инициализация сервисов — в ``ApplicationContext``,
каналы — в ``ChannelFactory``, lifecycle — в ``GatewayRunner``.
Файл отвечает ТОЛЬКО за gateway-специфику: preload FAISS-индексов,
вывод Rich-баннера.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import contextlib
import os
import sys
import traceback
from pathlib import Path


_SUPPORTED_PROFILES = ("prod", "test")


# ``ConfigurationError`` импортируется на module-level до ``_parse_args`` —
# единственное место, где boundary-исключения могут всплыть из
# validation-кода в argv-парсинге (missing --profile, неподдерживаемый
# профиль). Сам импорт ``config`` чистый (никаких side-effects на
# module-level — Phase A).
from config import ConfigurationError  # noqa: E402

from lib.utils.windows_terminal import ensure_console_colors

# Legacy Windows-консоль без VT печатает ANSI как "?[2m...". Включаем VT,
# а если хост не поддерживает — глушим цвета (NO_COLOR + ANSI-фильтр).
# До первого Console(), т.к. no_color читается в Console.__init__.
_WINDOWS_COLOR_WARNING = ensure_console_colors()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Парсинг argv без делегирования валидации ``--profile`` в argparse.

    Ошибки argparse (``--help``, missing flag) НЕ минуют boundary
    ``ConfigurationError → exit 2``. Внутри startup-блока выполняется
    явная whitelist-валидация (а не делегируется ``argparse.error``) —
    иначе ``SystemExit(2)`` от argparse минует ``ConfigurationError``
    boundary, нарушая Error Lifecycle Contract (см. design.md Decision 2).
    """
    parser = argparse.ArgumentParser(
        description="nanobot gateway", add_help=False
    )
    parser.add_argument(
        "--profile",
        type=str,
        default=None,
        help="Профиль конфигурации: prod | test.",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Smoke-режим: парсит --profile, инициализирует SETTINGS, "
             "печатает баннер и имя runtime-таблицы, выходит 0. "
             "Только для Phase F integration-тестов; production не использует.",
    )
    if argv is None:
        argv = sys.argv[1:]
    if "--help" in argv or "-h" in argv:
        parser.print_help()
        sys.exit(0)
    args, _unknown = parser.parse_known_args(argv)

    # Whitelist и required-валидация — внутри startup-блока,
    # НЕ через ``argparse.error``. Это даёт нам ``ConfigurationError``
    # boundary вместо ``SystemExit(2)`` от argparse.
    if not args.profile:
        raise ConfigurationError("--profile is required")
    if args.profile not in _SUPPORTED_PROFILES:
        raise ConfigurationError(
            f"--profile={args.profile!r} is not supported "
            f"(allowed: prod, test)"
        )
    return args


# Кросс-платформенная кодировка для ВСЕХ exec-подпроцессов (Windows + Linux).
# На Windows PowerShell по умолчанию cp1251/OEM, и Python-подпроцессы
# получают эту кодировку в stdout/stderr — кириллица в путях/выводе
# ломается (C:\Users\Алексей\… → C:\Users\\…). PYTHONUTF8=1 (PEP 540,
# Python 3.7+) переключает дочерний Python в UTF-8; PYTHONIOENCODING=utf-8
# фиксит stdout/stderr encoding. На Linux обе переменные обычно уже
# соответствуют (no-op), но задаём их явно — детерминированно.
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
# На Linux задаём C.UTF-8 locale для подпроцессов, чтобы Python читал
# кириллицу из argv/env в кодировке UTF-8, а не C/POSIX (ASCII-only).
# На Windows не трогаем LANG/LC_ALL — там переменная игнорируется Python'ом
# и оставление её не выставленной безопаснее.
if sys.platform != "win32":
    os.environ.setdefault("LC_ALL", "C.UTF-8")
    os.environ.setdefault("LANG", "C.UTF-8")

from loguru import logger
from rich.console import Console


def _entrypoint_main(args: argparse.Namespace, script_dir: Path, workspace_dir: Path) -> None:
    """Startup + application body.

    Raises ``ConfigurationError`` on startup errors. Никакого
    ``sys.exit(2)`` изнутри — это ответственность boundary
    ``_run`` (см. design.md Decision 2 unification).
    """
    import config as _cfg

    # Импорт нанобота — ПЕРЕД настройкой вывода, и это не стилистика.
    # ``nanobot/cli/commands.py:29-41`` при импорте делает
    # ``logger.remove()`` и ставит свой sink с форматом
    # ``{extra[channel]}``. Настройка вывода, поставленная до этого
    # импорта, оказывалась перебитой первой же последующей строкой,
    # и весь запуск печатал чужой формат.
    from nanobot.cli.commands import __logo__, __version__

    # 1. Lifecycle-gate: публикация SETTINGS на основе argv --profile.
    #    ``_SUPPORTED_PROFILES`` в argparse уже гарантирует whitelist,
    #    но ``_initialize_settings`` повторяет проверку (defensive —
    #    если кто-то вызовет lifecycle-gate напрямую минуя CLI).
    _cfg._initialize_settings(profile=args.profile)

    # Настройка вывода — до сборки контекста, и это не «для галочки»:
    # ``ApplicationContext.create()`` сам логирует хуки, инструменты
    # и project tools, и стоял он ниже. Эти строки уходили в формат
    # нанобота, а всё после — в формат консоли оператора, то есть
    # один и тот же запуск печатал два формата. Смоук это не
    # показывал: страж проверял порядок только относительно ветки
    # ``--smoke``, а не относительно первого же логирования.
    _configure_logging(_cfg.SETTINGS)

    from lib.core.application_context import ApplicationContext
    from lib.lifecycle.gateway_runner import GatewayRunner

    ctx = ApplicationContext.create(role='gateway', 
        script_dir=script_dir,
        workspace_dir=workspace_dir,
    )

    # 3. Smoke-режим: печатает баннер и runtime-таблицу, выходит сразу.
    #    Позволяет integration-тестам проверить конфигурацию без подъёма
    #    postgres channel/websocket listener/full event loop.
    # Импорты — выше ``if args.smoke:`` чтобы избежать
    # UnboundLocalError (Python видит имя в теле функции и считает
    # его локальным; ветка else не имеет своего импорта).
    from lib.utils.project_version import project_version

    if args.smoke:
        runtime_table = ctx.settings["logging"]["db"]["table_name"]
        # Баннер смоука остаётся в stdout: это блок, а не построчный факт.
        # Машинный маркер ``OK_SMOKE_COMPLETE`` читают пять проверок
        # (``tests/test_profile_lifecycle.py`` и ещё четыре) — соглашение о
        # результате запуска, а не факт для человека. Перенос построчных
        # фактов в stderr его не затрагивает.
        console.print(
            f"{__logo__} nanobot gateway smoke · "
            f"project v{project_version()} · nanobot {__version__} · "
            f"profile={args.profile} · logging.db.table_name={runtime_table}"
        )
        console.print("OK_SMOKE_COMPLETE")
        return

    console.print(
        f"{__logo__} Starting nanobot gateway · project v{_project_version()} "
        f"(nanobot {__version__}) · profile={args.profile}..."
    )

    # Локальный кэш агента снят вместе с ``_init_cache_runtime``, поэтому
    # ждать «первый sync» нечего: ни колбэков записи, ни фонового потока
    # в агенте не осталось. Снимком владеет capability ``data`` платформы,
    # и её ленивое подключение — отдельная сессия, поднимаемая рукопожатием
    # в ``_connect_enterprise_mcp`` ниже.

    ctx.start()

    _report_db_pool_startup()

    _check_websocket_port_available(ctx)

    try:
        GatewayRunner().run_forever(
            lambda: asyncio.run(_run(ctx))
        )
    finally:
        # Шага «опубликовать финальный снимок» больше нет: данные уже в
        # файле кэша, отдельной публикации не существует.
        # Останавливаем фоновые сервисы, которые создал ApplicationContext,
        # но channels — отдельно (живут в shutdown(ctx))
        ctx.stop()


def _project_version() -> str:
    """Ленивая обёртка над ``lib.utils.project_version.project_version``.

    Module-level импорт lib.* был отложен до первого обращения,
    потому что ``import lib.utils.project_version`` транзитивно
    читает ``config.SETTINGS`` (через event_log / log-формат) —
    и эта функция вызывается только при штатном старте, когда
    ``_initialize_settings`` уже отработал.
    """
    from lib.utils.project_version import project_version
    return project_version()


async def _connect_enterprise_mcp(ctx) -> None:
    """Поднять сессию ``enterprise-mcp`` и убедиться, что сервер отвечает.

    Инвариант: сервер поднимается **до** агента. Он всегда нужен — три входа
    к данным и пул соединений живут в нём, — поэтому «платформа не
    отвечает» обязано обнаруживаться на старте, а не посреди оборота.

    Проверка — это ``list_operations()``: он поднимает сессию и делает
    discovery. Второго механизма рукопожатия здесь нет намеренно: если
    сервер поднялся, но не отдал операции, подниматься он нечего.

    Отказ не проглатывается. ``EnterpriseMcpUnavailable`` уходит наверх, в
    ``GatewayRunner``, который перезапускает gateway с backoff и пишет
    причину в лог на каждой попытке. Тихая деградация была бы здесь
    единственным неправильным вариантом: каналы поднялись бы, задачи
    начали бы забираться, а tool'ы отвечали бы ошибкой.

    ``None`` — раздел ``enterprise_mcp`` выключен: тогда сервера нет по
    решению оператора, и это не повод падать. Потребители об этом сообщают
    структурной ошибкой (см. ``history_search_tool``).
    """
    client = getattr(ctx, "enterprise_mcp", None)
    if client is None:
        _verdict(
            "enterprise-mcp: не объявлен "
            "(раздел gateway.agent.enterprise_mcp выключен) — "
            "инструменты данных ответят структурной ошибкой",
            level="WARN",
        )
        return

    try:
        operations = await client.list_operations()
        _verdict(client.presence_line(len(operations)))
        # Куда ушёл stderr платформы. Без этой строки режим наблюдения
        # молчал бы, и «окно не открылось» читалось бы как «смотреть
        # не на что» — тем более что по умолчанию stderr уходит в
        # stderr агента вперемешку с его журналом.
        _verdict(client.stderr_report())
        await _report_enterprise_mcp_health(ctx, client)
    except ConfigurationError as exc:
        # Расхождение профиля: повтор не поможет, перезапуск лишь повторит
        # ту же ошибку через backoff. Сообщение печатается целиком, потому
        # что в логе иначе остаётся только «Gateway exited unexpectedly».
        _verdict(f"enterprise-mcp: КОНФИГУРАЦИЯ — {exc}", level="ERROR")
        raise
    except Exception as exc:
        # Именно этот вывод спасает при разборе инцидента: без него
        # ``GatewayRunner`` сообщает только «Gateway exited unexpectedly,
        # restarting in 1.0s», и причина — не поднявшаяся платформа — не
        # читается ни в одном логе. Строка уходит ДО ``raise``
        # (``runtime/entrypoints``), и уровнем ERROR, а не INFO: отказ рукопо-
        # жатия не должен быть отсеян отбором глубины.
        _verdict(
            f"enterprise-mcp: НЕ ПОДНЯЛСЯ — {type(exc).__name__}: {exc}",
            level="ERROR",
        )
        _verdict(
            "проверьте: mcp-platform/platform.json, "
            "mcp-platform/.secrets.env, доступность python и БД",
            level="ERROR",
        )
        raise


def _verdict(text: str, *, level: str = "INFO", who: str = "gateway") -> None:
    """Вердитная строка баннера в общий построчный поток консоли.

    Раньше такие строки печатались ``rich.console.print`` в stdout, то есть
    ВТОРЫМ потоком со своим синтаксисом и своими правилами кодировки. Теперь
    это тот же объявленный формат и тот же поток, что и факты оборота
    (``lib/services/operator_console.py``). Блочный инвентарный баннер в
    ``ApplicationContext`` остаётся rich — его читает
    ``tools/diagnose_startup.py`` регулярками, привязанными к началу строки,
    и это исключение объявлено намеренно.
    """
    from lib.services.operator_console import emit, startup_fact

    emit(startup_fact(text, who=who, level=level))


async def _report_enterprise_mcp_health(ctx, client) -> None:
    """Сводка по capability платформы сразу после рукопожатия.

    Процесс может подняться и при этом быть частично нерабочим: индексы
    не собрались, снимок недоступен, реестр скриптов пуст. Такое состояние
    раньше не было видно нигде — в логе есть только сырой stderr дочернего
    процесса, а вердикт «MCP поднят» ничего не говорил про данные.

    Проверки дешёвые и локальные (без обращений к внешним API): по одной
    операции на ``vectors``, ``data`` и ``audit``. Отказ проверки НЕ роняет
    старт — платформа отвечает, а неполнота одного capability разбирается
    отдельно и не должна выглядеть как «шлюз не поднялся».
    """
    from lib.services.enterprise_mcp_client import CallIdentity

    # Идентичность обязательна для платформы (require_call_meta), но это не
    # пользовательский оборот: подставляем служебную, чтобы health-проба
    # не создавала запись в agent_question_runs от имени живого запроса.
    identity = CallIdentity(
        session_id="startup:gateway", user_id="startup:health"
    ).with_request_id("startup-enterprise-mcp-health")

    probes = (
        ("vectors", "list_indexes", lambda d: _indexes_line(d)),
        ("data", "schema_check", lambda d: _schema_line(d)),
        ("audit", "list_scripts", lambda d: _scripts_line(d)),
    )

    platform_tables: list[str] | None = None
    for capability, operation, render_line in probes:
        healthy = True
        try:
            raw = await asyncio.wait_for(
                client.call(operation, arguments={}, identity=identity),
                timeout=20.0,
            )
            payload = json.loads(raw)
            line = render_line(payload)
            if operation == "schema_check":
                platform_tables = list(payload.get("tables") or [])
                healthy = bool(payload.get("ok"))
            elif operation == "list_indexes":
                indexes = payload.get("indexes") or []
                healthy = bool(indexes) and all(
                    i.get("state") == "ready" for i in indexes
                )
            elif operation == "list_scripts":
                count = payload.get("count")
                if count is None:
                    count = len(payload.get("scripts") or [])
                healthy = bool(count)
        except asyncio.TimeoutError:
            line = "проба не ответила за 20 с"
            healthy = False
        except Exception as exc:  # noqa: BLE001
            line = f"{type(exc).__name__}: {exc}"
            healthy = False
        _verdict(
            f"{capability}: {line}",
            level="INFO" if healthy else "WARN",
            who="platform",
        )

    _verify_platform_table_alignment(ctx, platform_tables)


def _verify_platform_table_alignment(ctx, platform_tables: list[str] | None) -> None:
    """Сверить имена таблиц агента и платформы и упасть при расхождении.

    Оверлей профиля объявлен в ДВУХ файлах: ``profiles/<mode>.jsonc`` агента и
    ``mcp-platform/platform.json → profiles.<имя>``. Правка одного без другого
    даёт ровно тот дефект, который профиль и чинит: агент опрашивает
    ``agent_conversation_messages_test``, а платформа пишет в
    ``agent_gateway_logs``, — и заметить это можно только по содержимому
    боевого журнала.

    Поэтому сверка обязана быть на старте, а не «когда-нибудь заметим».
    Расхождение — ``ConfigurationError``: подниматься с профилем, который
    пишет не туда, опаснее, чем не подняться.

    Сверяются три таблицы, которыми владеет платформа. Таблицы сессий
    (``messages_table``/``meta_table``) платформе не нужны — она ими не
    пользуется, и в её списке их нет.

    ``platform_tables is None`` — проба ``schema_check`` не ответила. Это уже
    показано в сводке строкой выше, и добивать старт второй ошибкой из-за
    той же причины незачем.
    """
    if platform_tables is None:
        return

    settings = getattr(ctx, "settings", None) or {}
    pg = (settings.get("channels", {}) or {}).get("postgres", {}) or {}

    # Имена журнальных таблиц берутся у ВЛАДЕЛЬЦА (``db_logging_service``),
    # а не из SETTINGS: доступ к конфигурации журнальных таблиц вне owner'а
    # запрещён (design D6.4, страж ``test_no_lookup_logging_db_table_name``) —
    # и по существу, а не только по регламенту. Ведьмачий журнал пишет
    # платформа, и схема проектируется так, чтобы агент её не знал; сверка не
    # должна возвращать это знание в составной root. Таблица очереди — другое
    # дело: ею владеет канал, и её раздел читать можно.
    logging_service = getattr(ctx, "db_logging_service", None)

    expected = {
        "log_table": getattr(logging_service, "_table_name", None),
        "question_runs_table": getattr(
            logging_service, "_question_runs_table", None
        ),
        "task_table": pg.get("table_name"),
    }

    def _bare(name: object) -> str:
        return str(name or "").split(".")[-1].strip()

    platform_bare = {_bare(name) for name in platform_tables if _bare(name)}
    missing = {
        key: value
        for key, value in expected.items()
        if value and _bare(value) not in platform_bare
    }
    if not missing:
        _verdict(
            f"tables: профиль согласован "
            f"({len(expected)} таблиц, profile={settings.get('profile', 'prod')})"
        )
        return

    detail = "; ".join(f"{key}={value}" for key, value in sorted(missing.items()))
    profile = settings.get("profile", "prod")
    raise ConfigurationError(
        f"профиль {profile!r}: имена таблиц агента и платформы расходятся — "
        f"{detail}. Платформа пишет в {sorted(platform_bare)}, агент ждёт эти. "
        f"Синхронизируйте profiles/{profile}.jsonc и "
        f"mcp-platform/platform.json → profiles.{profile}: оверлей объявлен "
        f"в двух файлах, и подниматься с расхождением нельзя — иначе журнал "
        f"тестового контура окажется в боевых таблицах."
    )


def _indexes_line(data: dict) -> str:
    """``3/3 индекса ready (10, 100, 10 векторов)``.

    Возвращается ГОЛЫЙ текст, без rich-разметки: строка уходит в общий
    построчный поток loguru, и ``[red]`` в нём был бы виден как мусор.
    Тяжесть строки задаёт вызывающий (см. ``_report_enterprise_mcp_health``).
    """
    indexes = data.get("indexes") or []
    if not indexes:
        return "индексы не объявлены"
    ready = [i for i in indexes if i.get("state") == "ready"]
    counts = ", ".join(str(i.get("vector_count", "?")) for i in indexes)
    if len(ready) != len(indexes):
        bad = ", ".join(
            "%s=%s" % (i.get("index_name"), i.get("state") or "unknown")
            for i in indexes
            if i.get("state") != "ready"
        )
        return f"{len(ready)}/{len(indexes)} ready ({bad})"
    return f"{len(ready)}/{len(indexes)} ready (векторов: {counts})"


def _schema_line(data: dict) -> str:
    """``7/7 таблиц на месте`` либо список отсутствующих."""
    if data.get("ok"):
        return f"{data.get('found')}/{data.get('expected')} таблиц на месте"
    missing = ", ".join(str(t) for t in (data.get("missing") or [])) or "неизвестно"
    return "не хватает таблиц: %s (найдено %s/%s)" % (
        missing, data.get("found"), data.get("expected"),
    )


def _scripts_line(data: dict) -> str:
    """Сколько предопределённых скриптов доступно capability ``audit``."""
    count = data.get("count")
    if count is None:
        scripts = data.get("scripts")
        count = len(scripts) if isinstance(scripts, list) else 0
    if not count:
        return "реестр скриптов пуст — будет только generate_sql"
    return f"{count} скриптов в реестре"


async def _run(ctx) -> None:
    """Основной рабочий цикл gateway: каналы + агент."""
    from lib.services.channel_factory import ChannelFactory

    channel_factory = ChannelFactory(
        print_worker_activity=_gateway_print_worker_activity(),
        db_logging_service=ctx.db_logging_service,
        enterprise_mcp=ctx.enterprise_mcp,
        compaction_event_subscriber=ctx.compaction_event_subscriber,
    )
    channels, messages = channel_factory.create_all(
        ctx.config, ctx.settings, ctx.bus, ctx.session_manager,
    )
    for msg in messages:
        console.print(msg)

    # Блок «audit_analyzer кэш загружен / vector indexes» снят в фазе 5
    # (п. 5.8): снимком и FAISS-индексами владеет платформа, у которой свои
    # capability ``data`` и ``vectors`` и своя точка их подготовки.

    # enterprise-mcp поднимается ДО каналов и ДО работы агента: его процесс — единственный
    # владелец пула PostgreSQL и единственный, кто даёт модели три входа к данным. Проверка
    # не декоративная — подъём ленивый, а отказ тогда обнаруживался бы посреди оборота, и
    # «платформа лежит» выглядел бы как «агент работает».
    await _connect_enterprise_mcp(ctx)

    # Транспорт журнала подключается ЗДЕСЬ, а не в ``ctx.start()``: ``start()`` выполняется
    # вне event loop, где мост ``LoopCallRunner`` построить не на чем, и сервис молча ушёл бы
    # писать ``INSERT`` сам — пул записи журнала остался бы в руках агента. Второй вызов
    # снимает отметку «транспорт не выбран», установленную в ``start()``.
    ctx.attach_log_transport()

    # Зеркало сессий стартует здесь, а не в ``ctx.start()``: это подсистема
    # шлюза, она работает задачей этого loop'а и ходит к данным через тот же
    # клиент платформы, чья сессия только что поднялась рукопожатием. Раньше —
    # раньше бессмысленно (loop ещё не существует), позже — позже сессии уже
    # могли бы перестать доходить до холодного хранилища незамеченными.
    mirror = getattr(ctx, "session_mirror", None)
    if mirror is not None:
        try:
            await mirror.start()
            if not mirror.enabled:
                console.print(
                    f"[yellow]session_mirror: выключено ({mirror.disabled_reason})[/yellow]"
                )
        except Exception as exc:
            logger.warning("SessionMirror not started: %s", exc)

    # Наблюдение за живостью платформы стартует сразу после рукопожатия и до
    # каналов: очередь, журнал и зеркало уходят в тот же процесс, и остановить
    # его можно в любой момент. Без наблюдения обрыв замечает только тот, кто
    # обратится первым, — а до обращения платформа может лежать сутки.
    mcp_health = getattr(ctx, "mcp_health_monitor", None)
    if mcp_health is not None:
        await mcp_health.start()

    try:
        channels_task = asyncio.create_task(channels.start_all())
        await ctx.agent.run()
    except (asyncio.CancelledError, KeyboardInterrupt):
        console.print("\nShutting down...")
    except Exception:
        console.print("\n[red]Gateway crashed[/red]")
        console.print(traceback.format_exc())
    finally:
        channels_task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await channels_task

        await ctx.agent.aclose()
        ctx.agent.stop()
        await channels.stop_all()

        flushed = ctx.agent.sessions.flush_all()
        if flushed:
            logger.info("Flushed {} session(s) to disk", flushed)

        # Финальный проход зеркала — ПОСЛЕ сброса сессий на диск и ДО закрытия
        # сессии платформы. Порядок не переставлен ради красоты: сброс делает
        # JSONL окончательным, и только после этого имеет смысл зеркалить;
        # закрытие сессии платформы до прохода погасило бы последний шанс внести
        # изменения текущего оборота. ``ctx.stop()`` вызывается уже после
        # ``asyncio.run``, когда loop мёртв, поэтому ждать его здесь нельзя
        # (D21).
        if mirror is not None:
            with contextlib.suppress(Exception):
                await mirror.stop()

        # Наблюдение останавливается до закрытия сессии платформы: иначе проба
        # успела бы разбудить процесс, который сейчас закрывают.
        if mcp_health is not None:
            with contextlib.suppress(Exception):
                await mcp_health.stop()

        # Сессия enterprise-mcp закрывается здесь, пока жив loop: после
        # выхода из asyncio.run() закрыть её уже нечем, и сервер завершился
        # бы только вслед за stdin агента.
        if getattr(ctx, "enterprise_mcp", None) is not None:
            with contextlib.suppress(Exception):
                await ctx.enterprise_mcp.aclose()


_SCRIPT_DIR: Path | None = None


def script_dir_for_runtime() -> Path:
    """Абсолютный путь к каталогу gateway.py.

    Module-level ``Path(__file__).parent`` лениво: чтобы ``import gateway``
    оставался чистым от side-effects (контракт ``application entrypoint``
    из design.md Decision 2).
    """
    global _SCRIPT_DIR
    if _SCRIPT_DIR is None:
        _SCRIPT_DIR = Path(__file__).resolve().parent
    return _SCRIPT_DIR


def _configure_logging(settings) -> None:
    """Настроить логирование из конфига (``gateway.log_level``).

    Через общую шву ``lib.utils.logging_utils.configure_loguru``: она же
    ставит мост stdlib ``logging`` → loguru, поэтому модули вроде
    ``application_context`` и ``gateway_runner``, пишущие через
    ``logging.getLogger``, подчиняются тому же уровню, что и loguru.
    Раньше ``gateway.log_level`` управлял только loguru, и ``INFO`` из
    stdlib-модулей не доходил до консоли вовсе.

    Глубина вывода консоли объявляется ОДНИМ ключом ``gateway.console_level``
    и не выводится повышением ``log_level`` — иначе факт простоя (сегодня
    DEBUG) пришлось бы поднимать до DEBUG целиком.

    Старые булевы ключи дают предупреждение с уровнем, который из них
    следует, и печатаются в баннер: молчаливый игнор изменил бы вывод у того,
    кто их выставил, без единого слова.
    """
    try:
        from lib.services.config_service import ConfigService

        gateway_settings = ConfigService().settings_section("gateway") or {}
        log_level = gateway_settings.get("log_level", "INFO")
    except Exception:
        gateway_settings = {}
        log_level = "INFO"
    from lib.utils.logging_utils import configure_loguru

    warnings = configure_loguru(log_level)
    return warnings


def _gateway_print_worker_activity() -> bool:
    """Видна ли активность пула воркеров при объявленной глубине вывода.

    Глубину объявляет ОДИН ключ ``gateway.console_level``
    (``operator_console.depth_visible("turn")``), а не этот флаг: прежний
    дефолт ``false`` означал, что за оборотом при ``log_level=INFO`` не было
    видно ничего.
    """
    try:
        from lib.services.operator_console import (
            CONSOLE_LEVEL_TURN,
            depth_visible,
        )
    except Exception:
        return False
    return depth_visible(CONSOLE_LEVEL_TURN)


def _report_db_pool_startup() -> None:
    """Прогреть пул соединений БД и вывести отчёт о его воркерах.

    Воркеры ``utils.db`` подключаются лениво, поэтому перед отчётом
    заставляем их реально подключиться (``probe_connections``), чтобы
    на старте gateway было видно: сколько воркеров должно быть, сколько
    запустилось и сколько не смогли подключиться к БД.

    ``timeout=None`` — ждём реального исхода подключения каждого воркера
    (при недоступной БД это честно выявляет ошибку вместо «0 connected»).
    """
    try:
        from utils.db import probe_connections, get_stats

        probe_connections()
        s = get_stats()
        expected = int(s.get("min_conn", 1))
        max_conn = int(s.get("max_conn", 4))
        started = int(s.get("workers", 0))
        connected = int(s.get("connected_workers", 0))
        failed = int(s.get("failed_workers", 0))
        if failed:
            errors = int(s.get("connect_errors", 0))
            console.print(
                f"[red]✗[/red] DB pool: workers {started}/{expected} "
                f"(max {max_conn}), connected {connected}, "
                f"failed {failed} (connect errors {errors})"
            )
        else:
            console.print(
                f"[green]✓[/green] DB pool: workers {started}/{expected} "
                f"(max {max_conn}), connected {connected}"
            )
    except Exception:
        console.print("[red]✗[/red] DB pool: статус недоступен")


def _check_websocket_port_available(ctx) -> None:
    """Проверить занятость порта WebSocket-канала перед стартом цикла.

    ``WebSocketChannel.start()`` биндит ``127.0.0.1:8765`` через
    ``websockets.asyncio.server.serve``. Если предыдущий запуск gateway
    был убит некорректно (крестик окна, диспетчер задач, kill -9), порт
    остаётся занятым процессом, который не успел закрыть сокет. Без
    этой проверки gateway падает с криптическим ``OSError: [Errno 10048]``
    в недрах ``asyncio.create_server`` уже после прохождения половины
    стартапа (включая Postgres-канал).

    Хост/порт — upstream default из
    ``nanobot.channels.websocket.runtime.WebSocketConfig`` (см.
    ``runtime.py:197-198``). Функция читает фактические значения из
    ``ctx.config.channels.websocket``, если они там заданы; иначе —
    дефолты.

    При занятости — печатает понятную диагностику (PID процесса-владельца
    и подсказку про ``taskkill``/Ctrl+C) и завершает процесс с кодом 1
    ДО запуска ``run_forever()``. Это предотвращает частичный старт
    (прогрев кэша DuckDB) с последующим падением.
    """
    import socket

    from rich.console import Console as _Console
    _console = _Console()

    host = "127.0.0.1"
    port = 8765
    try:
        ws_cfg = getattr(getattr(ctx.config, "channels", None), "websocket", None)
        if ws_cfg is not None:
            host = getattr(ws_cfg, "host", host) or host
            port = int(getattr(ws_cfg, "port", port) or port)
    except Exception:
        pass

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except OSError as e:
            owner_pid = _find_listener_pid(host, port)
            holder = (
                f"его держит процесс {owner_pid}"
                if owner_pid
                else "его держит НЕИЗВЕСТНЫЙ процесс"
            )
            _console.print(f"[red]✗[/red] Порт {host}:{port} уже занят — {holder}.")
            if owner_pid and not sys.platform.startswith("win"):
                _console.print(f"    kill {owner_pid}")
            elif owner_pid:
                _console.print(f"    taskkill /PID {owner_pid} /F")
            else:
                _console.print("    найдите держателя: netstat -ano -p TCP")
            _console.print(
                "  Чаще всего это живой второй gateway в соседнем окне — "
                "остановите его через Ctrl+C в его окне."
            )
            raise SystemExit(1) from e


def _find_listener_pid(host: str, port: int) -> int | None:
    """Найти PID процесса, слушающего ``host:port``.

    Реализация одна и у владельца её (``client.find_listener_pid``):
    ею пользуется и проверка порта канала здесь, и проверка закреплённого
    порта платформы в клиенте. Второе определение разошлось бы с первым
    при первой же правке одной из двух веток ОС — а расхождение двух
    копий и было причиной, по которой порт канала на Linux показывал
    ``PID ?``.

    Ветка Linux у владельца на этой машине (Windows) не проверена.
    """
    from lib.services.enterprise_mcp_client import find_listener_pid

    return find_listener_pid(host, port)


    import re
    import subprocess

    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout
    except Exception:
        return None

    pattern = re.compile(
        rf"\s+TCP\s+{re.escape(host)}:{port}\s+\S+\s+LISTENING\s+(\d+)\s*"
    )
    m = pattern.search(out)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


console = Console()


def main(argv: list[str] | None = None) -> int:
    """Точка входа gateway с единым error-lifecycle boundary.

    ``parse → validate → _initialize_settings → runtime imports →
    ApplicationContext`` — это ЕДИНСТВЕННЫЙ startup-путь (см. design.md
    Decision 2 unification). Все три exception-проверки
    (whitelist/unknown profile, прочие ConfigurationError) поднимают
    ``ConfigurationError``; этот boundary ловит её и превращает
    в ``sys.stderr.write + return 2`` — никаких прямых ``sys.exit``
    из validation-кода.
    """
    try:
        args = _parse_args(argv)
    except ConfigurationError as exc:
        sys.stderr.write(f"FATAL: {exc}\n")
        return 2

    script_dir = script_dir_for_runtime()
    workspace_dir = script_dir / "workspace"

    # Добавляем корень проекта и workspace в sys.path, чтобы импортировать
    # lib.hooks.* и workspace.utils.*. Префикс (0) — приоритет
    # над site-packages (нужно для подмены модулей в тестах).
    if str(script_dir) not in sys.path:
        sys.path.insert(0, str(script_dir))
    if str(workspace_dir) not in sys.path:
        sys.path.insert(0, str(workspace_dir))

    try:
        _entrypoint_main(args, script_dir, workspace_dir)
    except ConfigurationError as exc:
        sys.stderr.write(f"FATAL: {exc}\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
