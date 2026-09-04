"""gateway.py — серверный режим работы агента.

Тонкий оркестратор: вся инициализация сервисов — в ``ApplicationContext``,
каналы — в ``ChannelFactory``, lifecycle — в ``GatewayRunner``.
Файл отвечает ТОЛЬКО за gateway-специфику: spawn Streamlit, preload
FAISS-индексов, вывод Rich-баннера.
"""

from __future__ import annotations

import asyncio
import os
import sys
import traceback
from pathlib import Path

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

from lib.core.application_context import ApplicationContext
from lib.lifecycle.gateway_runner import GatewayRunner
from lib.services.channel_factory import ChannelFactory

_SCRIPT_DIR = Path(__file__).parent
_WORKSPACE_DIR = _SCRIPT_DIR / "workspace"

# Добавляем корень проекта и workspace в sys.path, чтобы импортировать
# lib.hooks.* и workspace.utils.*. Префикс (0) — приоритет
# над site-packages (нужно для подмены модулей в тестах).
sys.path.insert(0, str(_SCRIPT_DIR))
sys.path.insert(0, str(_WORKSPACE_DIR))

console = Console()


def main() -> None:
    """Точка входа gateway."""
    ctx = ApplicationContext.create(
        script_dir=_SCRIPT_DIR,
        workspace_dir=_WORKSPACE_DIR,
        enable_db_logging=True,
        enable_audit=True,
        print_llm_calls=_gateway_print_llm_calls(),
    )

    _configure_logging(ctx.settings)

    from nanobot.cli.commands import __logo__, __version__
    from lib.utils.project_version import project_version

    console.print(
        f"{__logo__} Starting nanobot gateway · project v{project_version()} "
        f"(nanobot {__version__})..."
    )

    # Назначаем callbacks и подменяем on_sync ДО ctx.start() — иначе
    # PgDuckDbSyncService.worker-тред успеет сделать initial_load раньше,
    # чем мы поставим callback (set_on_new_records_callback=None), и
    # данные не попадут в in-memory DuckDB.
    first_sync_event: "asyncio.Event | None" = None
    if ctx.sync_service is not None and ctx.cache_store is not None:
        ctx.cache_store.open()
        # Пересоздаём снапшот при каждом старте: удаляем устаревший файл,
        # чтобы CLI/skill не читали данные с прошлого запуска, пока
        # initial_load не заполнит свежий снимок заново.
        _old_snapshot = ctx.cache_store.get_stats().get("publish_path")
        if _old_snapshot:
            try:
                Path(_old_snapshot).unlink(missing_ok=True)
            except OSError:
                pass
        ctx.sync_service.set_on_new_records_callback(
            ctx.cache_store.upsert_records
        )
        # Сохраняем оригинальный callback и подменяем на обёртку,
        # которая set-ит Event при первом вызове И публикует снимок
        # DuckDB в publish_path после каждого цикла синхронизации.
        # Без publish() файл workspace/data_store/duckdb/cache.duckdb
        # не создаётся — CLI/skill читают пусто/404. Путь вычисляется
        # через table_registry.snapshot_path() в ApplicationContext.
        prev_cb = getattr(ctx.sync_service, "_on_sync_callback", None)
        first_sync_event = asyncio.Event()
        memory_store = ctx.cache_store
        _first_sync_done = False

        def _on_first_sync() -> None:
            if first_sync_event is not None:
                first_sync_event.set()

        def _wrapped() -> None:
            nonlocal _first_sync_done
            _on_first_sync()
            try:
                # Первая публикация — принудительная: снапшот пересоздаётся
                # даже если initial_load не нашёл ни одной строки (иначе
                # старый файл, удалённый при старте, не восстановится).
                memory_store.publish(force=not _first_sync_done)
                _first_sync_done = True
            except Exception:
                pass
            if prev_cb is not None:
                try:
                    prev_cb()
                except Exception:
                    pass

        ctx.sync_service.set_on_sync_callback(_wrapped)

    ctx.start()

    _report_db_pool_startup()

    try:
        GatewayRunner().run_forever(
            lambda: asyncio.run(_run(ctx, first_sync_event))
        )
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


async def _run(ctx: ApplicationContext, first_sync_event) -> None:
    """Основной рабочий цикл gateway: каналы + Streamlit + агент."""
    from lib.services.channel_factory import ChannelFactory

    channel_factory = ChannelFactory(
        transcription=ctx.transcription_service,
        print_worker_activity=_gateway_print_worker_activity(),
    )
    channels, messages = channel_factory.create_all(
        ctx.config, ctx.settings, ctx.bus, ctx.session_manager,
    )
    for msg in messages:
        console.print(msg)

    from lib.services.subprocess_manager import SubprocessManager
    subprocess_manager = SubprocessManager(log_dir=_SCRIPT_DIR / "logs")
    streamlit_script = _SCRIPT_DIR / "streamlit_app.py"
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

        # Фоновый прогрев FAISS-индексов в память; результат печатается
        # по мере готовности. Дожидаемся первого sync-callback от
        # PgDuckDbSyncService (он вызывается после initial_load), иначе
        # preload стартует на пустом DuckDB-кеше и видит "нет данных".
        async def _preload_and_report() -> None:
            if first_sync_event is not None:
                try:
                    await asyncio.wait_for(
                        first_sync_event.wait(), timeout=30.0
                    )
                except asyncio.TimeoutError:
                    console.print(
                        "[yellow]⚠[/yellow] audit_analyzer initial load "
                        "timeout (>30s), preload на текущем состоянии"
                    )
            loaded = await ctx.preload_service.preload_vector_indexes(
                cache_store
            )
            if not loaded:
                console.print(
                    "[dim]audit_analyzer vector indexes: нет данных в кэше[/dim]"
                )
                return
            for item in loaded:
                console.print(
                    f"[green]✓[/green] vector index '{item['index_name']}' "
                    f"built in memory: {item['vectors']} vectors"
                )

        asyncio.create_task(_preload_and_report())

    channels_task = asyncio.create_task(channels.start_all())

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

        await ctx.agent.close_mcp()
        ctx.agent.stop()
        await channels.stop_all()

        flushed = ctx.agent.sessions.flush_all()
        if flushed:
            logger.info("Flushed {} session(s) to disk", flushed)


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


if __name__ == "__main__":
    main()
