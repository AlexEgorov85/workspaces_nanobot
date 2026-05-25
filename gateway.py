"""Gateway entry point with PostgresChannel — run with: python gateway.py"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import sys
import traceback
from pathlib import Path

# Ensure the directory of this file is on sys.path so PostgresChannel can be imported
sys.path.insert(0, str(Path(__file__).parent))

from loguru import logger
from postgres_channel import PostgresChannel

from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.base import Tool
from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.manager import ChannelManager
from nanobot.cli.commands import _load_runtime_config, console, __logo__, __version__
from nanobot.config.loader import load_config, resolve_config_env_vars
from nanobot.config.paths import get_workspace_path, is_default_workspace
from nanobot.session.manager import SessionManager
from nanobot.utils.helpers import sync_workspace_templates

_WORKSPACE_DIR = Path(__file__).parent / "workspace"
_TOOLS_DIR = _WORKSPACE_DIR / "tools"
_HOOKS_DIR = _WORKSPACE_DIR / "hooks"
sys.path.insert(0, str(_TOOLS_DIR))
sys.path.insert(0, str(_HOOKS_DIR))
sys.path.insert(0, str(_WORKSPACE_DIR))


def _auto_format_result(result: object) -> object:
    """Convert non-string tool results (dict, list) into readable JSON so the
    LLM never sees raw Python repr like ``{'key': 'value'}``."""
    if isinstance(result, (dict, list)):
        return json.dumps(result, ensure_ascii=False, indent=2)
    return result


def _wrap_tool_execute(registry) -> None:
    """Wrap every registered tool's ``execute`` with automatic JSON formatting."""
    for name in registry.tool_names:
        tool = registry.get(name)
        if tool is None:
            continue
        original = tool.execute

        async def _execute(self, original=original, **kwargs: object) -> object:
            return _auto_format_result(await original(**kwargs))

        tool.execute = _execute.__get__(tool, type(tool))


def _scan_and_register_tools(registry) -> None:
    """Scan workspace/tools/*/tool.py for Tool subclasses and register them."""
    for pkg_dir in sorted(_TOOLS_DIR.iterdir()):
        if not pkg_dir.is_dir() or pkg_dir.name.startswith("_") or pkg_dir.name.startswith("."):
            continue
        tool_file = pkg_dir / "tool.py"
        if not tool_file.exists():
            continue
        try:
            mod = importlib.import_module(f"{pkg_dir.name}.tool")
        except Exception as exc:
            console.print(f"[yellow]⚠[/yellow] tool.py in {pkg_dir.name}: {exc}")
            continue
        for attr_name in dir(mod):
            attr = getattr(mod, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, Tool)
                and attr is not Tool
                and not attr_name.startswith("_")
            ):
                try:
                    registry.register(attr())
                    console.print(f"[green]✓[/green] {attr.__name__} registered")
                except Exception as exc:
                    console.print(f"[yellow]⚠[/yellow] {attr.__name__}: {exc}")


def main():
    # 1. Load config -----------------------------------------------------------
    config = _load_runtime_config()

    sync_workspace_templates(config.workspace_path)
    console.print(f"{__logo__} Starting nanobot gateway v{__version__}...")


    # 2. Bus + AgentLoop -------------------------------------------------------
    bus = MessageBus()
    session_manager = SessionManager(config.workspace_path)

    hooks = []
    try:
        from auto_store_hook import AutoStoreHook, set_session_key
        hooks.append(AutoStoreHook(workspace_dir=_WORKSPACE_DIR))
        console.print("[green]✓[/green] AutoStoreHook loaded")
    except Exception as exc:
        console.print(f"[yellow]⚠[/yellow] AutoStoreHook: {exc}")

    agent = AgentLoop.from_config(
        config, bus,
        session_manager=session_manager,
        hooks=hooks,
    )

    # Проброс session_key в AutoStoreHook без изменения ядра nanobot
    _original_run_loop = agent._run_agent_loop

    async def _run_agent_loop_with_session_key(*args, **kwargs):
        set_session_key(kwargs.get("session_key"))
        return await _original_run_loop(*args, **kwargs)

    agent._run_agent_loop = _run_agent_loop_with_session_key

    # 3. Discover and register custom tools from workspace/tools/ --------------
    _scan_and_register_tools(agent.tools)
    _wrap_tool_execute(agent.tools)
    console.print("[green]✓[/green] Tool result auto-formatting applied")

    # 4. ChannelManager — discovers telegram, websocket etc. from config -------
    channels = ChannelManager(config, bus, session_manager=session_manager)

    # 5. Inject PostgresChannel (only if enabled) -------------------------------
    pg_cfg = getattr(config.channels, "postgres", {})
    if pg_cfg.get("enabled", False):
        pg_channel = PostgresChannel(pg_cfg, bus)

        # Mirror the transcription/display settings that _init_channels() sets
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

    # 6. Run -------------------------------------------------------------------
    async def run():
        channels_task = asyncio.create_task(channels.start_all())

        try:
            await agent.run()
        except KeyboardInterrupt:
            console.print("\nShutting down...")
        except Exception:
            console.print("\n[red]Gateway crashed[/red]")
            console.print(traceback.format_exc())
        finally:
            channels_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await channels_task
            await agent.close_mcp()
            agent.stop()
            await channels.stop_all()
            flushed = agent.sessions.flush_all()
            if flushed:
                logger.info("Flushed {} session(s) to disk", flushed)

    asyncio.run(run())


def _resolve_transcription_key(config):
    """Resolve the transcription API key from provider config."""
    provider = config.channels.transcription_provider
    try:
        if provider == "openai":
            return config.providers.openai.api_key
        return config.providers.groq.api_key
    except AttributeError:
        return ""


def _resolve_transcription_base(config):
    """Resolve the transcription API base URL from provider config."""
    provider = config.channels.transcription_provider
    try:
        if provider == "openai":
            return config.providers.openai.api_base or ""
        return config.providers.groq.api_base or ""
    except AttributeError:
        return ""


if __name__ == "__main__":
    main()
