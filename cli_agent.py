"""CLI agent entry point with tool registration — run with: python cli_agent.py

Launches nanobot in interactive CLI mode, scanning workspace/tools/*/tool.py
for custom Tool subclasses and registering them automatically.
"""

import asyncio
import contextlib
import importlib
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from loguru import logger

from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.base import Tool
from nanobot.bus.queue import MessageBus
from nanobot.cli.commands import (
    _flush_pending_tty_input,
    _init_prompt_session,
    _is_exit_command,
    _load_runtime_config,
    _maybe_print_interactive_progress,
    _model_display,
    _print_agent_response,
    _read_interactive_input_async,
    _restore_terminal,
    _sanitize_surrogates,
    console,
    __logo__,
    __version__,
)
from nanobot.cli.stream import StreamRenderer
from nanobot.config.paths import is_default_workspace
from nanobot.cron.service import CronService
from nanobot.utils.helpers import sync_workspace_templates

_WORKSPACE_DIR = Path(__file__).parent / "workspace"
_TOOLS_DIR = _WORKSPACE_DIR / "tools"
_HOOKS_DIR = _WORKSPACE_DIR / "hooks"
sys.path.insert(0, str(_TOOLS_DIR))
sys.path.insert(0, str(_HOOKS_DIR))
sys.path.insert(0, str(_WORKSPACE_DIR))


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


def _migrate_cron_store(config) -> None:
    """One-time migration: move legacy global cron store into the workspace."""
    from nanobot.config.paths import get_cron_dir

    legacy_path = get_cron_dir() / "jobs.json"
    new_path = config.workspace_path / "cron" / "jobs.json"
    if legacy_path.is_file() and not new_path.exists():
        new_path.parent.mkdir(parents=True, exist_ok=True)
        import shutil

        shutil.move(str(legacy_path), str(new_path))


def main():
    config = _load_runtime_config()
    sync_workspace_templates(config.workspace_path)
    console.print(f"{__logo__} Starting nanobot CLI agent v{__version__}...")

    bus = MessageBus()

    if is_default_workspace(config.workspace_path):
        _migrate_cron_store(config)

    cron_store_path = config.workspace_path / "cron" / "jobs.json"
    cron = CronService(cron_store_path)

    agent = AgentLoop.from_config(
        config, bus,
        cron_service=cron,
    )

    _scan_and_register_tools(agent.tools)

    # CLI interactive mode ------------------------------------------------------
    from nanobot.bus.events import InboundMessage

    _init_prompt_session()
    _model, _preset_tag = _model_display(config)
    console.print(
        f"Interactive mode [bold blue]({_model})[/bold blue]{_preset_tag} "
        f"\u2014 type [bold]exit[/bold] or [bold]Ctrl+C[/bold] to quit\n"
    )

    session_id = "cli:direct"
    if ":" in session_id:
        cli_channel, cli_chat_id = session_id.split(":", 1)
    else:
        cli_channel, cli_chat_id = "cli", session_id

    def _handle_signal(signum, frame):
        sig_name = signal.Signals(signum).name
        _restore_terminal()
        console.print(f"\nReceived {sig_name}, goodbye!")
        sys.exit(0)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _handle_signal)
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)

    async def run_interactive():
        bus_task = asyncio.create_task(agent.run())
        turn_done = asyncio.Event()
        turn_done.set()
        turn_response: list[tuple[str, dict]] = []
        renderer: StreamRenderer | None = None

        async def _consume_outbound():
            while True:
                try:
                    msg = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                except asyncio.CancelledError:
                    break

                if msg.metadata.get("_stream_delta"):
                    if renderer:
                        await renderer.on_delta(msg.content)
                    continue
                if msg.metadata.get("_stream_end"):
                    if renderer:
                        await renderer.on_end(
                            resuming=msg.metadata.get("_resuming", False),
                        )
                    continue
                if msg.metadata.get("_streamed"):
                    turn_done.set()
                    continue

                if await _maybe_print_interactive_progress(
                    msg, None, agent.channels_config, renderer,
                ):
                    continue

                if not turn_done.is_set():
                    if msg.content:
                        turn_response.append((msg.content, dict(msg.metadata or {})))
                    turn_done.set()
                elif msg.content:
                    await _print_agent_response(
                        msg.content,
                        render_markdown=True,
                        metadata=msg.metadata,
                    )

        outbound_task = asyncio.create_task(_consume_outbound())

        try:
            while True:
                try:
                    _flush_pending_tty_input()
                    if renderer:
                        renderer.stop_for_input()
                    user_input = _sanitize_surrogates(await _read_interactive_input_async())
                    command = user_input.strip()
                    if not command:
                        continue

                    if _is_exit_command(command):
                        _restore_terminal()
                        console.print("\nGoodbye!")
                        break

                    turn_done.clear()
                    turn_response.clear()
                    renderer = StreamRenderer(
                        render_markdown=True,
                        bot_name=config.agents.defaults.bot_name,
                        bot_icon=config.agents.defaults.bot_icon,
                    )

                    await bus.publish_inbound(InboundMessage(
                        channel=cli_channel,
                        sender_id="user",
                        chat_id=cli_chat_id,
                        content=user_input,
                        metadata={"_wants_stream": True},
                    ))

                    await turn_done.wait()

                    if turn_response:
                        content, meta = turn_response[0]
                        if content and not meta.get("_streamed"):
                            if renderer:
                                await renderer.close()
                            print_kwargs = {}
                            if renderer and renderer.header_printed:
                                print_kwargs["show_header"] = False
                            _print_agent_response(
                                content,
                                render_markdown=True,
                                metadata=meta,
                                **print_kwargs,
                            )
                    elif renderer and not renderer.streamed:
                        await renderer.close()
                except KeyboardInterrupt:
                    _restore_terminal()
                    console.print("\nGoodbye!")
                    break
                except EOFError:
                    _restore_terminal()
                    console.print("\nGoodbye!")
                    break
        finally:
            agent.stop()
            outbound_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await outbound_task
            await agent.close_mcp()
            flushed = agent.sessions.flush_all()
            if flushed:
                logger.info("Flushed {} session(s) to disk", flushed)

    asyncio.run(run_interactive())


if __name__ == "__main__":
    main()
