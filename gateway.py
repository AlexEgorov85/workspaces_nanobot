"""
Шлюз (gateway) для nanobot — долгоживущий сервер с каналами связи.

Настройки — в gateway_settings.py в той же директории.
Запуск:
    python gateway.py
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import traceback
from pathlib import Path

_SCRIPT_DIR = Path(__file__).parent
_WORKSPACE_DIR = _SCRIPT_DIR / "workspace"
sys.path.insert(0, str(_SCRIPT_DIR))
sys.path.insert(0, str(_WORKSPACE_DIR))

import json

from loguru import logger
from postgres_channel import PostgresChannel
from redis_channel import RedisChannel
from utils.session_file_store import SessionFileStore, prepare_content

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.manager import ChannelManager, _default_webui_dist
from nanobot.cli.commands import _load_runtime_config, console, __logo__, __version__
from pg_session_manager import PGSessionManager
from nanobot.utils.helpers import sync_workspace_templates

from hooks.tool_audit_hook import ToolAuditHook

from gateway_settings import GatewaySettings

SETTINGS = GatewaySettings()


def _patch_webui_dist(channels: ChannelManager) -> None:
    """Replace WebUI dist with custom build from webui-dist/."""
    ws = channels.channels.get("websocket")
    if ws is None or not hasattr(ws, "_static_dist_path"):
        return
    orig = _default_webui_dist()
    if orig is None:
        return
    custom = Path(__file__).parent / "webui-dist"
    if not custom.is_dir():
        import shutil
        shutil.copytree(str(orig), str(custom))
        console.print(f"[green]✓[/green] Created custom webui dist at {custom}")
    ws._static_dist_path = custom.resolve()


def _resolve_transcription_key(config):
    provider = config.channels.transcription_provider
    try:
        if provider == "openai":
            return config.providers.openai.api_key
        return config.providers.groq.api_key
    except AttributeError:
        return ""


def _resolve_transcription_base(config):
    provider = config.channels.transcription_provider
    try:
        if provider == "openai":
            return config.providers.openai.api_base or ""
        return config.providers.groq.api_base or ""
    except AttributeError:
        return ""


def main() -> None:
    """Запускает gateway со всеми локальными доработками."""

    # ── 1. Загрузка конфигурации ─────────────────────────────────────────
    config = _load_runtime_config()
    sync_workspace_templates(config.workspace_path)
    console.print(f"{__logo__} Starting nanobot gateway v{__version__}...")

    # ── 2. Шина сообщений и SessionManager ───────────────────────────────
    bus = MessageBus()

    pg = SETTINGS.pg
    dsn = pg.dsn
    if dsn:
        from utils.db import db
        db.configure(dsn, min_size=pg.pool_min_conn, max_size=pg.pool_max_conn)
        import os
        os.environ["DATABASE_URL"] = dsn
    use_postgres = SETTINGS.storage == "postgres" or (SETTINGS.storage == "auto" and bool(dsn))
    if use_postgres:
        if not dsn:
            console.print("[red]✗[/red] storage=postgres but pg.dsn is empty in gateway_settings.py")
            sys.exit(1)
        session_manager = PGSessionManager(
            workspace=config.workspace_path,
            dsn=dsn,
            schema=pg.schema,
            messages_table=pg.messages_table,
            meta_table=pg.meta_table,
            min_conn=pg.pool_min_conn,
            max_conn=pg.pool_max_conn,
            pool_timeout=pg.pool_timeout,
        )
        session_manager.ensure_tables()
        console.print("[green]✓[/green] PGSessionManager: sessions stored in PostgreSQL")
    else:
        from nanobot.session.manager import SessionManager
        session_manager = SessionManager(config.workspace_path)
        if dsn:
            console.print("[dim]PostgreSQL DSN available but storage=file; using JSONL files[/dim]")
        else:
            console.print("[yellow]⚠[/yellow] No PostgreSQL DSN — using JSONL files")

    # ── 3. Monkey-patch _normalize_tool_result ────────────────────────────
    if SETTINGS.persist_threshold > 0:
        # Увеличиваем лимит вывода shell-команды, чтобы сохранять полный вывод
        try:
            from nanobot.agent.tools.shell import ExecTool
            ExecTool._MAX_OUTPUT = 10_000_000
        except Exception:
            pass

        _persisted_store = SessionFileStore(
            _WORKSPACE_DIR / "data_store",
            max_files=SETTINGS.persist_max_files,
            max_age_hours=SETTINGS.persist_max_age_hours,
        )
        try:
            from nanobot.agent.runner import AgentRunner
            from nanobot.utils.runtime import ensure_nonempty_tool_result

            # read_file — exempt from offload to prevent persist→read→persist loops
            _EXEMPT_TOOLS = frozenset({"read_file"})
            _original = AgentRunner._normalize_tool_result

            def _normalize_with_persist(self, spec, tool_call_id, tool_name, result):
                result = ensure_nonempty_tool_result(tool_name, result)
                if tool_name in _EXEMPT_TOOLS:
                    return result

                text = None
                if isinstance(result, str):
                    text = result
                elif not isinstance(result, bytes):
                    try:
                        text = json.dumps(result, ensure_ascii=False, indent=2)
                    except (TypeError, ValueError):
                        pass

                if text is not None and len(text.encode("utf-8")) > SETTINGS.persist_threshold:
                    try:
                        content, ext = prepare_content(text)
                        save_info = _persisted_store.save(
                            session_key=spec.session_key or "default",
                            content=content,
                            source_tool=tool_name,
                            ext=ext,
                        )
                        return (
                            f"[Result saved to data_store/"
                            f"{save_info['path']} ({save_info['size_kb']} KB)]"
                        )
                    except OSError as _exc:
                        # disk full, permissions, etc — fall back to original
                        pass

                return _original(self, spec, tool_call_id, tool_name, result)

            AgentRunner._normalize_tool_result = _normalize_with_persist
            console.print("[green]✓[/green] _normalize_tool_result patched")
        except Exception as exc:
            console.print(f"[yellow]⚠[/yellow] _normalize_tool_result patch failed: {exc}")

    # ── 4. Таймауты ───────────────────────────────────────────────────────
    import os
    if SETTINGS.llm_timeout >= 0:
        os.environ["NANOBOT_LLM_TIMEOUT_S"] = str(SETTINGS.llm_timeout)
    if SETTINGS.exec_timeout >= 0:
        try:
            config.tools.exec.timeout = SETTINGS.exec_timeout
        except Exception:
            pass

    # ── 5. Логирование ───────────────────────────────────────────────────
    try:
        logger.remove()
        logger.add(sys.stderr, level=SETTINGS.log_level)
    except Exception:
        pass

    # ── 6. Создание AgentLoop ────────────────────────────────────────────
    tool_audit_hook = ToolAuditHook()
    agent = AgentLoop.from_config(
        config, bus,
        session_manager=session_manager,
        hooks=[tool_audit_hook],
    )

    # Monkey-patch _assemble_outbound to inject tool audit trail into metadata
    _orig_assemble = agent._assemble_outbound

    def _assemble_with_audit(msg, final_content, all_msgs, stop_reason, had_injections, on_stream, *, turn_latency_ms=None):
        result = _orig_assemble(msg, final_content, all_msgs, stop_reason, had_injections, on_stream, turn_latency_ms=turn_latency_ms)
        if result is not None:
            entries = tool_audit_hook.drain()
            if entries:
                result.metadata["_tool_audit"] = entries
        return result

    agent._assemble_outbound = _assemble_with_audit
    # ── 7. ChannelManager ────────────────────────────────────────────────
    channels = ChannelManager(config, bus, session_manager=session_manager)

    # ── 8. Redis-канал ────────────────────────────────────────────────────
    rs = SETTINGS.redis
    if rs.enabled:
        redis_cfg = {
            "enabled": True,
            "host": rs.host,
            "port": rs.port,
            "db": rs.db,
            "password": rs.password,
            "incoming_key": rs.incoming_key,
            "outgoing_prefix": rs.outgoing_prefix,
            "poll_timeout": rs.poll_timeout,
            "max_concurrent": rs.max_concurrent,
            "allow_from": rs.allow_from,
        }
        redis_channel = RedisChannel(redis_cfg, bus)
        redis_channel.send_progress = config.channels.send_progress
        redis_channel.send_tool_hints = config.channels.send_tool_hints
        redis_channel.show_reasoning = config.channels.show_reasoning
        channels.channels["redis"] = redis_channel
        console.print("[green]✓[/green] Redis channel enabled")
    else:
        console.print("[dim]Redis channel disabled[/dim]")

    # ── 9. Postgres-канал ────────────────────────────────────────────────
    ch = pg.channel
    ch_dsn = ch.dsn or dsn
    ch_schema = ch.schema or pg.schema
    if ch.enabled:
        if not ch_dsn:
            console.print("[red]✗[/red] PostgresChannel enabled but no DSN (pg.dsn or pg.channel.dsn)")
        else:
            ch_cfg = {
                "enabled": True,
                "dsn": ch_dsn,
                "schema": ch_schema,
                "table_name": ch.table_name,
                "poll_interval": ch.poll_interval,
                "flush_interval": ch.flush_interval,
                "max_concurrent": ch.max_concurrent,
                "processing_timeout": ch.processing_timeout,
                "allow_from": ch.allow_from,
            }
            pg_channel = PostgresChannel(ch_cfg, bus)
            pg_channel.transcription_provider = config.channels.transcription_provider
            pg_channel.transcription_api_key = _resolve_transcription_key(config)
            pg_channel.transcription_api_base = _resolve_transcription_base(config)
            pg_channel.transcription_language = config.channels.transcription_language
            pg_channel.send_progress = config.channels.send_progress
            pg_channel.send_tool_hints = config.channels.send_tool_hints
            pg_channel.show_reasoning = config.channels.show_reasoning
            channels.channels["postgres"] = pg_channel
            console.print("[green]✓[/green] PostgreSQL channel enabled")
    else:
        console.print("[dim]PostgreSQL channel disabled[/dim]")

    # ── 11. Кастомная WebUI-сборка ──────────────────────────────────────
    _patch_webui_dist(channels)

    console.print(f"[green]✓[/green] Channels enabled: {', '.join(channels.enabled_channels)}")

    # ── 12. Запуск ───────────────────────────────────────────────────────
    async def run():
        channels_task = asyncio.create_task(channels.start_all())

        # Start DB API server (if PostgreSQL configured)
        db_api_runner = None
        if dsn:
            try:
                from db_api.server import _build_app
                import aiohttp
                db_api_app = _build_app()
                db_api_runner = aiohttp.web.AppRunner(db_api_app)
                await db_api_runner.setup()
                db_api_site = aiohttp.web.TCPSite(db_api_runner, "127.0.0.1", 8777)
                await db_api_site.start()
                console.print("[green]✓[/green] DB API server started on :8777")
            except Exception as exc:
                console.print(f"[yellow]⚠[/yellow] DB API server failed to start: {exc}")

        try:
            await agent.run()
        except asyncio.CancelledError:
            console.print("\nShutting down...")
        except KeyboardInterrupt:
            console.print("\nShutting down...")
        except Exception:
            console.print("\n[red]Gateway crashed[/red]")
            console.print(traceback.format_exc())
        finally:
            channels_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await channels_task
            if db_api_runner:
                with contextlib.suppress(Exception):
                    await db_api_runner.cleanup()
            await agent.close_mcp()
            agent.stop()
            await channels.stop_all()
            flushed = agent.sessions.flush_all()
            if flushed:
                logger.info("Flushed {} session(s) to disk", flushed)

    while True:
        try:
            asyncio.run(run())
        except KeyboardInterrupt:
            console.print("\nExiting...")
            break
        except Exception:
            console.print("[red]Gateway exited unexpectedly, restarting in 5s...[/red]")
            import time
            time.sleep(5)
            continue
        break  # clean shutdown


if __name__ == "__main__":
    main()
