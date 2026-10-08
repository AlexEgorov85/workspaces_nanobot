"""gateway.py — серверный режим работы агента.

Тонкий оркестратор: вся инициализация сервисов — в ``ApplicationContext``,
каналы — в ``ChannelFactory``, lifecycle — в ``GatewayRunner``.
Файл отвечает ТОЛЬКО за gateway-специфику: spawn Streamlit, preload
FAISS-индексов, вывод Rich-баннера.
"""

from __future__ import annotations

import argparse
import asyncio
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
from lib.services.startup_gate import (  # noqa: E402
    StartupGateError,
    ThreadSafeSignal,
)

from lib.utils.windows_terminal import enable_vt, is_windows_console

if is_windows_console():
    enable_vt()


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

    # 1. Lifecycle-gate: публикация SETTINGS на основе argv --profile.
    #    ``_SUPPORTED_PROFILES`` в argparse уже гарантирует whitelist,
    #    но ``_initialize_settings`` повторяет проверку (defensive —
    #    если кто-то вызовет lifecycle-gate напрямую минуя CLI).
    _cfg._initialize_settings(profile=args.profile)

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
    from nanobot.cli.commands import __logo__, __version__

    if args.smoke:
        runtime_table = ctx.settings["logging"]["db"]["table_name"]
        console.print(
            f"{__logo__} nanobot gateway smoke · "
            f"project v{project_version()} · nanobot {__version__} · "
            f"profile={args.profile} · logging.db.table_name={runtime_table}"
        )
        console.print("OK_SMOKE_COMPLETE")
        return

    _configure_logging(ctx.settings)

    console.print(
        f"{__logo__} Starting nanobot gateway · project v{_project_version()} "
        f"(nanobot {__version__}) · profile={args.profile}..."
    )

    # Назначаем callbacks и подменяем on_sync ДО ctx.start() — иначе
    # PgDuckDbSyncService.worker-тред успеет сделать initial_load раньше,
    # чем мы поставим callback (set_on_new_records_callback=None), и
    # данные не попадут in-memory DuckDB.
    #
    # ``ThreadSafeSignal``, а не ``asyncio.Event``: сигнал выставляется из
    # worker-треда, а ждёт его startup-фаза в своём loop. Без явного
    # ``call_soon_threadsafe`` такое ожидание (а таймаутов здесь нет по
    # решению фичи) могло бы длиться бесконечно уже после загрузки.
    first_sync_event: "ThreadSafeSignal | None" = None
    if ctx.sync_service is not None and ctx.cache_store is not None:
        ctx.cache_store.connect()
        # Пересоздаём снапшот при каждом старте: удаляем устаревший файл,
        # чтобы CLI/skill не читали данные с прошлого запуска, пока
        # initial_load не заполнит свежий снимок заново.
        _old_snapshot = ctx.cache_store.get_stats().get("publish_path")
        if _old_snapshot:
            # Чистим и финальный снапшот, и осиротевший .tmp (publish мог быть
            # убит между ATTACH и os.replace — тогда .tmp лежит залоченный
            # через NFS lockd, и новый publish отстрелит "PID 0" на ATTACH).
            for _candidate in (Path(_old_snapshot),
                               Path(_old_snapshot + ".tmp")):
                try:
                    _candidate.unlink(missing_ok=True)
                except OSError:
                    pass
        ctx.sync_service.set_on_new_records_callback(
            ctx.cache_store.upsert_records
        )
        # Сохраняем оригинальный callback и подменяем на обёртку,
        # которая публикует снимок DuckDB в publish_path после каждого
        # цикла синхронизации и выставляет сигнал готовности данных.
        # Без publish() файл workspace/data_store/duckdb/cache.duckdb
        # не создаётся — CLI/skill читают пусто/404. Путь вычисляется
        # через table_registry.snapshot_path() в ApplicationContext.
        prev_cb = getattr(ctx.sync_service, "_on_sync_callback", None)
        first_sync_event = ThreadSafeSignal()
        memory_store = ctx.cache_store
        _first_sync_done = False

        def _on_first_sync() -> None:
            if first_sync_event is not None:
                first_sync_event.set()

        def _wrapped() -> None:
            nonlocal _first_sync_done
            try:
                # Первая публикация — принудительная: снапшот пересоздаётся
                # даже если initial_load не нашёл ни одной строки (иначе
                # старый файл, удалённый при старте, не восстановится).
                memory_store.publish(force=not _first_sync_done)
                _first_sync_done = True
            except Exception:
                pass
            # Event выставляется ПОСЛЕ publish: «DuckDB загружен» должно
            # означать и данные в памяти, и снапшот на диске. Раньше event
            # снимался до publish, поэтому стартовый гейт мог начать
            # сборку векторов, пока снапшот ещё писался.
            _on_first_sync()
            if prev_cb is not None:
                try:
                    prev_cb()
                except Exception:
                    pass

        ctx.sync_service.set_on_sync_callback(_wrapped)

    # Сигнал готовности данных публикуем на ctx: фоновый режим
    # (``await_ready=false``) ждёт его уже в рабочем цикле.
    ctx.cache_ready_signal = first_sync_event  # type: ignore[attr-defined]

    ctx.start()

    _report_db_pool_startup()

    _check_websocket_port_available(ctx)

    # Фаза подготовки — ДО ``run_forever``: DuckDB → векторы. Только
    # после неё поднимаются каналы, то есть очередь вопросов начинает
    # обслуживаться с готовыми FAISS-индексами. Вынесено из рабочего
    # цикла намеренно: у подготовки своя семантика отказа (fail → exit 2),
    # а ``GatewayRunner`` перезапускает именно рабочий цикл.
    _run_startup_preparation(ctx, first_sync_event)

    try:
        GatewayRunner().run_forever(lambda: asyncio.run(_run(ctx)))
    finally:
        # Финальный снимок в publish_path — гарантируем, что CLI/skill
        # увидят свежие данные даже если цикл поллинга не успел
        # отработать после последнего апдейта.
        if ctx.cache_store is not None:
            try:
                ctx.cache_store.publish()
            except Exception:
                pass
        # Останавливаем фоновые сервисы, которые создал ApplicationContext,
        # но Streamlit/channels — отдельно (живут в shutdown(ctx))
        ctx.stop()


def _run_startup_preparation(ctx, first_sync_event) -> None:
    """Явная фаза «данные → векторы» перед стартом каналов.

    Шаги (без таймаутов, порядок держится на сигнале готовности):

    1. ``StartupGate.wait_for_cache`` — ждёт сигнал ``PgDuckDbSyncService``
       «данные в DuckDB + снапшот опубликован»;
    2. ``StartupGate.load_vectors`` — дождаться сборки FAISS-индексов.

    Пока шаг 1 не завершён, процесс жив, но каналы закрыты: вопросы не
    принимаются. Это видно и в логе (строка ожидания печатается), и в
    readiness (``vector_search=DOWN``, ``gate: pending``).

    Raises:
        StartupGateError: ``gateway.startup.vector_preload.on_unavailable
            ="fail"`` и векторы не готовы — ``gateway.main()`` превращает
            это в ``FATAL`` + ``exit 2``, каналы не поднимаются.
    """
    from lib.services.startup_gate import StartupGateError

    gate = getattr(ctx, "startup_gate", None)
    cache_store = getattr(ctx, "cache_store", None)
    sync_service = getattr(ctx, "sync_service", None)

    # Без сервиса синхронизации (аудит выключен) данных ждать нечего, а
    # FAISS строится из DuckDB-кэша, которого нет: гейт не применяется.
    if gate is None or cache_store is None or sync_service is None:
        return

    config = getattr(gate, "config", None)
    if config is None or not config.enabled:
        console.print(
            "[dim]vector preload выключен "
            "(gateway.startup.vector_preload.enabled=false)[/dim]"
        )
        return

    if not config.await_ready:
        # Явный отказ от ожидания по требованию конфига: каналы стартуют
        # сразу, индексы догружаются в фоне рабочего цикла (там же, где
        # живёт loop фоновой задачи — из подготовки её запускать нельзя).
        ctx.background_vector_preload = True  # type: ignore[attr-defined]
        console.print(
            "[yellow]⚠[/yellow] Векторные индексы готовятся в фоне "
            "(gateway.startup.vector_preload.await_ready=false): "
            "первые вопросы уйдут агенту без векторов"
        )
        return

    console.print(
        "[dim]⏳ Подготовка данных: PG → DuckDB → FAISS-индексы. "
        "Каналы стартуют после этого шага.[/dim]"
    )

    try:
        asyncio.run(
            _run_startup_preparation_async(ctx, first_sync_event)
        )
    except StartupGateError as exc:
        _print_gate_report(exc.report)
        raise


async def _run_startup_preparation_async(ctx, cache_ready_signal) -> None:
    """async-шаги подготовки (тело ``_run_startup_preparation``).

    Отделено от синхронной обёртки, чтобы порядок можно было проверять
    тестом изнутри одного loop, а не гонять ``asyncio.run`` в потоке.

    ``StartupGate.wait_for_cache`` сам привязывает loop к сигналу
    (``ThreadSafeSignal``), поэтому пробуждение из worker-треда
    синхронизации надёжно даже без таймаута.

    Raises:
        StartupGateError: при ``on_unavailable="fail"`` — векторы не
            готовы, значит каналы не поднимаются.
    """
    gate = ctx.startup_gate
    await gate.wait_for_cache(cache_ready_signal)
    report = await gate.load_vectors()
    _print_gate_report(report)


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


async def _run(ctx) -> None:
    """Основной рабочий цикл gateway: каналы + Streamlit + агент.

    Вызывается из ``GatewayRunner.run_forever`` — то есть **после**
    фазы подготовки (``_run_startup_preparation``): данные в DuckDB уже
    загружены, FAISS-индексы собраны, и только теперь поднимаются
    каналы. Отсюда и порядок в вопросах: канал стартует после векторов.
    """
    from lib.services.channel_factory import ChannelFactory

    channel_factory = ChannelFactory(
        transcription=ctx.transcription_service,
        print_worker_activity=_gateway_print_worker_activity(),
        db_logging_service=ctx.db_logging_service,
    )
    channels, messages = channel_factory.create_all(
        ctx.config, ctx.settings, ctx.bus, ctx.session_manager,
    )
    for msg in messages:
        console.print(msg)

    from lib.services.subprocess_manager import SubprocessManager
    subprocess_manager = SubprocessManager(log_dir=script_dir_for_runtime() / "logs")
    streamlit_script = script_dir_for_runtime() / "streamlit_app.py"
    if _streamlit_enabled() and subprocess_manager.spawn_streamlit(streamlit_script):
        console.print("[green]✓[/green] Streamlit UI started on :8501")

    cache_store = ctx.cache_store
    sync_service = ctx.sync_service
    if cache_store is not None and sync_service is not None:
        if cache_store.get_stats().get("publish_path"):
            console.print(
                f"[green]✓[/green] audit_analyzer sync started "
                f"(publish -> {cache_store.get_stats()['publish_path']})"
            )
        else:
            console.print("[green]✓[/green] audit_analyzer sync started")

    # Данные и векторы к этому моменту уже готовы — фаза подготовки
    # отработала до ``run_forever`` (``_run_startup_preparation``).
    # Здесь стартуют каналы, то есть очередь вопросов.
    channels_task = asyncio.create_task(channels.start_all())

    if getattr(ctx, "background_vector_preload", False):
        # Явный отказ от ожидания (``await_ready=false``): вопросы уже
        # принимаются, индексы догружаются здесь же, в loop агента.
        asyncio.create_task(_preload_in_background(ctx))

    if cache_store is not None and sync_service is not None:
        console.print(
            "[green]✓[/green] Channels started: очередь вопросов "
            "обслуживается с готовыми векторами"
        )

    try:
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

        subprocess_manager.terminate_all()

        await ctx.agent.aclose()
        ctx.agent.stop()
        await channels.stop_all()

        flushed = ctx.agent.sessions.flush_all()
        if flushed:
            logger.info("Flushed {} session(s) to disk", flushed)


def _print_gate_report(report) -> None:
    """Rich-вывод отчёта гейта: что загрузилось, что нет и почему."""
    if report.phase == "skipped":
        console.print(f"[dim]vector preload: {report.detail}[/dim]")
        return
    if not report.ok:
        console.print(
            f"[red]✗[/red] vector preload не завершён: {report.detail}"
        )
    for err in report.errors:
        name = err.get("index_name") or "?"
        console.print(
            f"  [red]✗[/red] '{name}': "
            f"{err.get('error_type')}: {err.get('error')}"
        )
    for item in report.loaded:
        console.print(
            f"[green]✓[/green] vector index '{item['index_name']}' "
            f"built in memory: {item['vectors']} vectors"
        )
    if report.ok and not report.loaded and not report.errors:
        console.print(
            "[dim]vector indexes: нет данных в кэше[/dim]"
        )
    if report.ok and report.loaded:
        console.print(
            f"[green]✓[/green] vector indexes готовы "
            f"({len(report.loaded)}, {report.duration_sec:.1f}s) — "
            "каналы стартуют"
        )


async def _preload_in_background(ctx) -> None:
    """Фоновый прогрев индексов (режим ``await_ready=false``).

    Каналы к этому моменту уже стартовали — это осознанный отказ от
    ожидания по требованию конфига, а не поведение по умолчанию.
    Задача живёт в loop рабочего цикла (в preparation-loop её создать
    нельзя: loop закроется вместе с фазой подготовки).

    Сигнал готовности данных здесь по-прежнему ждём **без таймаута**:
    пустой DuckDB-кэш дал бы нулевые индексы, то есть тихо худшие
    ответы — ровно то, что фича убирает.
    """
    gate = getattr(ctx, "startup_gate", None)
    if gate is None:
        return
    try:
        await gate.wait_for_cache(getattr(ctx, "cache_ready_signal", None))
        report = await gate.load_vectors()
    except Exception as exc:  # noqa: BLE001 - фоновая задача не роняет loop
        console.print(f"[yellow]⚠[/yellow] vector preload failed: {exc}")
        return
    _print_gate_report(report)


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
    """Настроить loguru из конфига (gateway.log_level)."""
    try:
        from lib.services.config_service import ConfigService

        log_level = ConfigService().settings_section("gateway").get("log_level", "INFO")
    except Exception:
        log_level = "INFO"
    from lib.utils.logging_utils import configure_loguru

    configure_loguru(log_level)


def _gateway_print_llm_calls() -> bool:
    """Прочитать флаг вывода токенов LLM в терминал из ``gateway.print_llm_calls``.

    Отключаемая опция: `false` по умолчанию, включается в `project.json`.
    """
    try:
        from lib.services.config_service import ConfigService

        value = ConfigService().settings_section("gateway").get("print_llm_calls", False)
    except Exception:
        return False
    return bool(value)


def _gateway_print_worker_activity() -> bool:
    """Прочитать флаг вывода активности пула воркеров в терминал.

    Читает ``gateway.print_worker_activity`` из `project.json` (секция gateway).
    Отключаемая опция: `false` по умолчанию.
    """
    try:
        from lib.services.config_service import ConfigService

        value = ConfigService().settings_section("gateway").get("print_worker_activity", False)
    except Exception:
        return False
    return bool(value)


def _streamlit_enabled() -> bool:
    """Прочитать флаг включения Streamlit UI.

    Читает ``streamlit.enabled`` из `project.json` (секция streamlit).
    ``false`` — gateway не поднимает веб-чат на :8501; ``true`` (по умолчанию)
    — поднимает.
    """
    try:
        from lib.services.config_service import ConfigService

        value = ConfigService().settings_section("streamlit").get("enabled", True)
    except Exception:
        return True
    return bool(value)


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
    стартапа (включая Streamlit и Postgres-канал).

    Хост/порт — upstream default из
    ``nanobot.channels.websocket.runtime.WebSocketConfig`` (см.
    ``runtime.py:197-198``). Функция читает фактические значения из
    ``ctx.config.channels.websocket``, если они там заданы; иначе —
    дефолты.

    При занятости — печатает понятную диагностику (PID процесса-владельца
    и подсказку про ``taskkill``/Ctrl+C) и завершает процесс с кодом 1
    ДО запуска ``run_forever()``. Это предотвращает частичный старт
    (Streamlit, синхронизация DuckDB) с последующим падением.
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
            owner_pid = _find_listener_pid(host, port) or "?"
            _console.print(
                f"[red]✗[/red] Порт {host}:{port} уже занят (PID {owner_pid}). "
                f"Вероятно, остался висеть предыдущий процесс gateway."
            )
            _console.print(
                "  Завершите его одним из способов и запустите снова:"
            )
            _console.print(f"    taskkill /PID {owner_pid} /F")
            _console.print("    (или закройте окно gateway через Ctrl+C в PowerShell)")
            raise SystemExit(1) from e


def _find_listener_pid(host: str, port: int) -> int | None:
    """Найти PID процесса, слушающего ``host:port`` (Windows).

    Использует ``netstat -ano`` через subprocess (PowerShell не имеет
    нативного API для этого). При ошибке парсинга или отсутствии
    процесса — возвращает ``None``.
    """
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
    except StartupGateError as exc:
        # Векторы обязательны (on_unavailable="fail"), но не готовы:
        # каналы не подняты, агент не запущен. Это отказ старта, а не
        # падение в цикле — тот бы перезапустился по backoff и записал
        # ту же ошибку в лог снова и снова.
        sys.stderr.write(f"FATAL: {exc}\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
