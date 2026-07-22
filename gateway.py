"""
Шлюз (gateway) для nanobot — долгоживущий сервер с каналами связи.

Настройки — в .env / config.json.
Запуск:
    python gateway.py
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import sys
import time
import traceback
from pathlib import Path

_SCRIPT_DIR = Path(__file__).parent
_WORKSPACE_DIR = _SCRIPT_DIR / "workspace"
sys.path.insert(0, str(_SCRIPT_DIR))
sys.path.insert(0, str(_WORKSPACE_DIR))

import json

from loguru import logger
from lib.channels.postgres_channel import PostgresChannel
from lib.channels.redis_channel import RedisChannel
from utils.session_file_store import SessionFileStore, prepare_content

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.manager import ChannelManager
from nanobot.cli.commands import _load_runtime_config, console, __logo__, __version__
from lib.session.pg_session_manager import PGSessionManager
from nanobot.utils.helpers import sync_workspace_templates

from hooks.tool_audit_hook import ToolAuditHook

from config import SETTINGS


def _resolve_transcription_key(config):
    """Вернуть API-ключ для провайдера транскрипции.

    Поддерживает openai и groq. Если ключ не найден — пустая строка.
    """
    provider = config.channels.transcription_provider
    try:
        if provider == "openai":
            return config.providers.openai.api_key
        return config.providers.groq.api_key
    except AttributeError:
        return ""


def _resolve_transcription_base(config):
    """Вернуть базовый URL для API транскрипции.

    Поддерживает openai и groq. Если не задан — пустая строка.
    """
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
    config = _load_runtime_config(config=str(_SCRIPT_DIR / "config.json"), workspace=str(_WORKSPACE_DIR))
    sync_workspace_templates(config.workspace_path)
    console.print(f"{__logo__} Starting nanobot gateway v{__version__}...")

    # ── 2. Шина сообщений и SessionManager ───────────────────────────────
    bus = MessageBus()

    pg = SETTINGS.postgresql
    dsn = pg.dsn
    if dsn:
        from utils.db import configure
        configure(dsn)
        import os
        os.environ["DATABASE_URL"] = dsn
    use_postgres = SETTINGS.gateway.storage == "postgres" or (SETTINGS.gateway.storage == "auto" and bool(dsn))
    if use_postgres:
        if not dsn:
            console.print("[red]✗[/red] storage=postgres but pg.dsn is empty in config")
            sys.exit(1)
        session_manager = PGSessionManager(
            workspace=config.workspace_path,
            dsn=dsn,
            schema=pg.schema,
            messages_table=pg.messages_table,
            meta_table=pg.meta_table,
        )
        console.print("[green]✓[/green] PGSessionManager: sessions stored in PostgreSQL")
    else:
        from nanobot.session.manager import SessionManager
        session_manager = SessionManager(config.workspace_path)
        if dsn:
            console.print("[dim]PostgreSQL DSN available but storage=file; using JSONL files[/dim]")
        else:
            console.print("[yellow]⚠[/yellow] No PostgreSQL DSN — using JSONL files")

    # ── 3. Monkey-patch _normalize_tool_result ────────────────────────────
    if SETTINGS.gateway.persist_threshold > 0:
        _persisted_store = SessionFileStore(
            _WORKSPACE_DIR / "data_store",
            max_files=SETTINGS.gateway.persist_max_files,
            max_age_hours=SETTINGS.gateway.persist_max_age_hours,
        )
        try:
            from nanobot.agent.runner import AgentRunner
            from nanobot.utils.runtime import ensure_nonempty_tool_result

            # read_file исключён из выгрузки, чтобы избежать циклов
            # persist → прочитал файл → persist прочитанного → ...
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

                if text is not None and len(text.encode("utf-8")) > SETTINGS.gateway.persist_threshold:
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
    if SETTINGS.gateway.llm_timeout >= 0:
        os.environ["NANOBOT_LLM_TIMEOUT_S"] = str(SETTINGS.gateway.llm_timeout)
    if SETTINGS.gateway.exec_timeout >= 0:
        try:
            config.tools.exec.timeout = SETTINGS.gateway.exec_timeout
        except Exception:
            pass

    # ── 5. Логирование ───────────────────────────────────────────────────
    try:
        logger.remove()
        logger.add(sys.stderr, level=SETTINGS.gateway.log_level)
    except Exception:
        pass

    # ── 6. Создание AgentLoop ────────────────────────────────────────────
    tool_audit_hook = ToolAuditHook()
    agent = AgentLoop.from_config(
        config, bus,
        session_manager=session_manager,
        hooks=[tool_audit_hook],
    )

    # Monkey-patch _assemble_outbound — внедряем аудит тулов в metadata
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
    ch = pg.get("channel", {})
    ch_dsn = ch.get("dsn", "") or dsn
    ch_schema = ch.get("schema", "") or pg.schema
    if ch.get("enabled", False):
        if not ch_dsn:
            console.print("[red]✗[/red] PostgresChannel enabled but no DSN (pg.dsn or pg.channel.dsn)")
        else:
            ch_cfg = {
                "enabled": True,
                "dsn": ch_dsn,
                "schema": ch_schema,
                "table_name": ch.get("table", "conversation_messages"),
                "poll_interval": ch.get("poll_interval", 2.0),
                "flush_interval": ch.get("flush_interval", 2.0),
                "max_concurrent": ch.get("max_concurrent", 1),
                "processing_timeout": ch.get("processing_timeout", 120),
                "allow_from": ch.get("allow_from", ["*"]),
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

    console.print(f"[green]✓[/green] Channels enabled: {', '.join(channels.enabled_channels)}")

    _streamlit_script = _SCRIPT_DIR / "streamlit_app.py"

    # ── 11. Фоновая загрузка DuckDB-кеша для audit_analyzer ──────────────
    async def _preload_audit_cache():
        """Фоновая загрузка DuckDB-кеша для навыка audit_analyzer при старте."""
        cache_path, db_cfg = _get_audit_cache_cfg()
        if not cache_path or not db_cfg:
            console.print("[dim]audit_analyzer in-memory cache: disabled[/dim]")
            return
        from skills.audit_analyzer.scripts.database import InMemoryDatabase
        from pathlib import Path
        cache_file = Path(cache_path)
        if cache_file.exists():
            import time
            age = time.time() - cache_file.stat().st_mtime
            if age < 3600:
                console.print(f"[green]✓[/green] audit_analyzer in-memory cache is fresh ({age/60:.0f}m old)")
                return
        try:
            InMemoryDatabase.load_from_postgres(cache_path, db_cfg)
            console.print(f"[green]✓[/green] audit_analyzer in-memory cache loaded ({Path(cache_path).name})")
        except Exception as exc:
            console.print(f"[yellow]⚠[/yellow] audit_analyzer cache preload failed: {exc}")

    async def _background_audit_cache_refresh():
        """Фоновая задача: каждый час проверять свежесть кеша и перезагружать при изменениях."""
        cache_path, db_cfg = _get_audit_cache_cfg()
        if not cache_path or not db_cfg:
            return
        from skills.audit_analyzer.scripts.database import InMemoryDatabase
        import logging as _logging
        while True:
            try:
                await asyncio.sleep(3600)
                result = InMemoryDatabase.check_stale(cache_path, db_cfg)
                if result.get("stale_tables"):
                    _logging.getLogger("cache").info(
                        "Audit cache stale tables: %s, reloading...", result["stale_tables"]
                    )
                    InMemoryDatabase.load_from_postgres(cache_path, db_cfg)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                _logging.getLogger("cache").warning("Audit cache refresh failed: %s", exc)

    def _get_audit_cache_cfg():
        """Вернуть (cache_path, db_config) для audit_analyzer или (None, None)."""
        try:
            acfg = SETTINGS.skills.audit_analyzer
            if not acfg.get("in_memory_enabled", False):
                return None, None
            cache_path = acfg.get("in_memory_cache_path", "")
            if not cache_path:
                return None, None
            cache_file = Path(cache_path)
            if not cache_file.is_absolute():
                cache_file = config.workspace_path / "skills" / "audit_analyzer" / cache_path
            from skills.audit_analyzer.scripts.skill_config import load_db_config
            return str(cache_file), load_db_config()
        except Exception:
            return None, None

    # ── 12. Запуск ───────────────────────────────────────────────────────
    async def run():
        channels_task = asyncio.create_task(channels.start_all())

        # Запуск Streamlit UI (веб-интерфейс чата)
        _streamlit_proc: subprocess.Popen | None = None
        _streamlit_log_handle = None
        if _streamlit_script.exists():
            try:
                _streamlit_log = _SCRIPT_DIR / "logs" / "streamlit.log"
                _streamlit_log_handle = open(_streamlit_log, "a", encoding="utf-8")
                _streamlit_proc = subprocess.Popen(
                    [sys.executable, "-m", "streamlit", "run", str(_streamlit_script),
                     "--server.headless", "true",
                     "--server.port", "8501"],
                    stdout=_streamlit_log_handle,
                    stderr=subprocess.STDOUT,
                )
                console.print("[green]✓[/green] Streamlit UI started on :8501")
            except Exception as exc:
                console.print(f"[yellow]⚠[/yellow] Streamlit failed to start: {exc}")

        try:
            asyncio.create_task(_preload_audit_cache())
            asyncio.create_task(_background_audit_cache_refresh())
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
            if _streamlit_proc:
                _streamlit_proc.terminate()
                try:
                    _streamlit_proc.wait(timeout=5)
                except Exception:
                    _streamlit_proc.kill()
            if _streamlit_log_handle:
                try:
                    _streamlit_log_handle.close()
                except Exception:
                    pass
            await agent.close_mcp()
            agent.stop()
            await channels.stop_all()
            flushed = agent.sessions.flush_all()
            if flushed:
                logger.info("Flushed {} session(s) to disk", flushed)

    # Перезапуск с exponential backoff: 1с → 2с → 4с → 8с → 16с → 30с
    # Если gateway упал с ошибкой, ждём всё дольше, чтобы не спамить
    # БД/Redis переподключениями. После успешного запуска (clean shutdown)
    # задержка сбрасывается, т.к. мы выходим из цикла.
    restart_delay = 1.0
    max_restart_delay = 30.0

    while True:
        try:
            asyncio.run(run())
        except KeyboardInterrupt:
            console.print("\nExiting...")
            break
        except Exception:
            console.print(f"[red]Gateway exited unexpectedly, restarting in {restart_delay}s...[/red]")
            console.print(traceback.format_exc())
            time.sleep(restart_delay)
            restart_delay = min(restart_delay * 2, max_restart_delay)
            continue
        break  # clean shutdown — выходим из цикла, процесс завершается


if __name__ == "__main__":
    main()
