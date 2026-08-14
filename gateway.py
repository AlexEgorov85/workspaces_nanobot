"""gateway.py — серверный режим работы агента.

Тонкий оркестратор: вся инициализация сервисов — в ``ApplicationContext``,
каналы — в ``ChannelFactory``, lifecycle — в ``GatewayRunner``.
Файл отвечает ТОЛЬКО за gateway-специфику: spawn Streamlit, preload
FAISS-индексов, вывод Rich-баннера.
"""

from __future__ import annotations

import asyncio
import sys
import traceback
from pathlib import Path

from loguru import logger
from rich.console import Console

from lib.core.application_context import ApplicationContext
from lib.lifecycle.gateway_runner import GatewayRunner
from lib.services.channel_factory import ChannelFactory

_SCRIPT_DIR = Path(__file__).parent
_WORKSPACE_DIR = _SCRIPT_DIR / "workspace"

# Добавляем корень проекта и workspace в sys.path, чтобы импортировать
# hooks.tool_audit_hook и workspace.utils.*. Префикс (0) — приоритет
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
    )

    _configure_logging(ctx.settings)

    from nanobot.cli.commands import __logo__, __version__

    console.print(f"{__logo__} Starting nanobot gateway v{__version__}...")

    # Назначаем callbacks и подменяем on_sync ДО ctx.start() — иначе
    # AuditSyncService.worker-тред успеет сделать initial_load раньше,
    # чем мы поставим callback (set_on_new_records_callback=None), и
    # данные не попадут в in-memory DuckDB.
    first_sync_event: "asyncio.Event | None" = None
    if ctx.audit_sync_service is not None and ctx.audit_memory_store is not None:
        ctx.audit_memory_store.open()
        ctx.audit_sync_service.set_on_new_records_callback(
            ctx.audit_memory_store.upsert_records
        )
        # Сохраняем оригинальный callback и подменяем на обёртку,
        # которая set-ит Event при первом вызове И публикует снимок
        # DuckDB в publish_path после каждого цикла синхронизации.
        # Без publish() файл workspace/skills/audit_analyzer/cache/
        # audit_cache.duckdb не создаётся — CLI/skill читают пусто/404.
        prev_cb = getattr(ctx.audit_sync_service, "_on_sync_callback", None)
        first_sync_event = asyncio.Event()
        memory_store = ctx.audit_memory_store

        def _on_first_sync() -> None:
            if first_sync_event is not None:
                first_sync_event.set()

        def _wrapped() -> None:
            _on_first_sync()
            try:
                memory_store.publish()
            except Exception:
                pass
            if prev_cb is not None:
                try:
                    prev_cb()
                except Exception:
                    pass

        ctx.audit_sync_service.set_on_sync_callback(_wrapped)

    ctx.start()

    try:
        GatewayRunner().run_forever(
            lambda: asyncio.run(_run(ctx, first_sync_event))
        )
    finally:
        # Финальный снимок в publish_path — гарантируем, что CLI/skill
        # увидят свежие данные даже если цикл поллинга не успел
        # отработать после последнего апдейта.
        if ctx.audit_memory_store is not None:
            try:
                ctx.audit_memory_store.publish()
            except Exception:
                pass
        # Останавливаем фоновые сервисы, которые создал ApplicationContext,
        # но Streamlit/channels — отдельно (живут в shutdown(ctx))
        ctx.stop()


async def _run(ctx: ApplicationContext, first_sync_event) -> None:
    """Основной рабочий цикл gateway: каналы + Streamlit + агент."""
    from lib.services.channel_factory import ChannelFactory

    channel_factory = ChannelFactory(transcription=ctx.transcription_service)
    channels, messages = channel_factory.create_all(
        ctx.config, ctx.settings, ctx.bus, ctx.session_manager,
    )
    for msg in messages:
        console.print(msg)

    from lib.services.subprocess_manager import SubprocessManager
    subprocess_manager = SubprocessManager(log_dir=_SCRIPT_DIR / "logs")
    streamlit_script = _SCRIPT_DIR / "streamlit_app.py"
    if subprocess_manager.spawn_streamlit(streamlit_script):
        console.print("[green]✓[/green] Streamlit UI started on :8501")

    audit_memory_store = ctx.audit_memory_store
    audit_sync_service = ctx.audit_sync_service
    if audit_memory_store is not None and audit_sync_service is not None:
        if audit_memory_store.get_stats().get("publish_path"):
            console.print(
                f"[green]✓[/green] audit_analyzer sync started "
                f"(publish -> {audit_memory_store.get_stats()['publish_path']})"
            )
        else:
            console.print("[green]✓[/green] audit_analyzer sync started")

        # Фоновый прогрев FAISS-индексов в память; результат печатается
        # по мере готовности. Дожидаемся первого sync-callback от
        # AuditSyncService (он вызывается после initial_load), иначе
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
                audit_memory_store
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
    """Настроить loguru из конфига."""
    try:
        from lib.services.config_service import ConfigService

        log_level = ConfigService().settings_section("gateway").get("log_level", "INFO")
    except Exception:
        log_level = "INFO"
    try:
        logger.remove()
        logger.add(sys.stderr, level=log_level)
    except Exception:
        pass


if __name__ == "__main__":
    main()
