"""ConsoleLoop — интерактивный REPL CLI-агента поверх MessageBus + AgentLoop.

Рендерит reasoning/tool-events/final-ответ (typewriter), читает пользовательский
ввод, корректно глушит agent при выходе.
"""

from __future__ import annotations

import asyncio
import re
import sys
from typing import Any

from rich.console import Console

from lib.cli.display_config import DisplayConfig
from lib.utils.outbound_meta import is_stream_delta

console = Console()

ANSI_RE = re.compile(r"(\x1b\[[0-9;]*[a-zA-Z])")


async def _typewriter(text: str, style: str, speed: float) -> None:
    if not text:
        return
    if speed <= 0:
        if style:
            console.print(f"[{style}]{text}[/{style}]")
        else:
            console.print(text)
        return
    from io import StringIO

    from rich.console import Console as RichConsole
    buf = StringIO()
    tmp = RichConsole(file=buf, force_terminal=True)
    if style:
        tmp.print(f"[{style}]{text}[/{style}]", end="")
    else:
        tmp.print(text, end="")
    for part in ANSI_RE.split(buf.getvalue()):
        if not part:
            continue
        if ANSI_RE.fullmatch(part):
            sys.stdout.write(part)
            sys.stdout.flush()
        else:
            for char in part:
                sys.stdout.write(char)
                sys.stdout.flush()
                await asyncio.sleep(speed)
    sys.stdout.write("\n")
    sys.stdout.flush()


async def _print_reasoning_block(text: str, cfg: DisplayConfig) -> None:
    if not text.strip() or not cfg.show_reasoning:
        return
    await _typewriter(text.strip(), "dim italic", cfg.typewriter_speed)


async def _print_tool_events(events: list, cfg: DisplayConfig) -> None:
    if not cfg.show_tool_calls:
        return
    for ev in events:
        if not isinstance(ev, dict):
            continue
        name = ev.get("name", "?")
        status = ev.get("status") or ev.get("phase", "")
        args = ev.get("arguments")
        params_str = ""
        if args and cfg.show_tool_params:
            params_str = ", ".join(f"{k}={v}" for k, v in args.items())[:200]
        if status in ("ok", "end"):
            result = str(ev.get("result_preview") or ev.get("result", ""))[:120] or "ok"
            if params_str:
                label = f"✓ {name}({params_str}) → {result}"
            elif cfg.show_tool_results:
                label = f"✓ {name} → {result}"
            else:
                label = f"✓ {name}"
            await _typewriter(label, "dim", cfg.typewriter_speed)
        elif status == "error":
            err = ev.get("error", "failed")
            if params_str:
                label = f"✗ {name}({params_str}): {err}"
            else:
                label = f"✗ {name}: {err}"
            await _typewriter(label, "dim", cfg.typewriter_speed)


async def _print_context_window(block: Any, cfg: DisplayConfig) -> None:
    """Напечатать компактную строку занятости контекстного окна (M1 UI).

    Блок ``{used, limit, pct, model}`` кладётся в ``metadata.context_window``
    патчем ``RuntimePatcher.patch_assemble_outbound``. В CLI выводим
    однострочно, гейтом ``cfg.show_context_window`` (по умолчанию вкл).

    pct уже clamp'нут в 0..1 и округлён в патче; здесь только защита
    от нечисловых значений (если блок пришёл не из патча).
    """
    if not cfg.show_context_window or not isinstance(block, dict):
        return
    try:
        used = int(block.get("used") or 0)
        limit = int(block.get("limit") or 0)
    except (TypeError, ValueError):
        return
    if limit <= 0 or used < 0:
        return
    try:
        pct = float(block.get("pct", 0.0))
    except (TypeError, ValueError):
        pct = 0.0
    pct = max(0.0, min(1.0, pct))
    model = block.get("model") or ""
    label = f"📊 Контекст: {used} / {limit} · {int(round(pct * 100))}%"
    if model:
        label = f"{label} · {model}"
    console.print(f"[dim]{label}[/dim]")


async def _run_cli_compact(
    agent: Any,
    command: str,
    chat_id: str,
    cli_channel: str,
) -> None:
    """Обработать CLI-команду ``/compact``: безоговорочно сжать контекст.

    ``command`` — полная входная строка (``/compact`` или ``/compact idle``).

    Ручной запуск **всегда** сжимает сессию жёстко (``compact_idle_session``),
    независимо от текущего размера контекста и порога ``consolidationRatio``.
    Это требование явного пользовательского действия: ``/compact`` = «хочу
    уменьшить контекст прямо сейчас». Флаги ``idle``/``--idle``/``-i``
    допустимы для совместимости и поведения не меняют (``force=True`` уже
    подразумевает ``idle``).

    Session key собирается как ``"<cli_channel>:<chat_id>"`` — он совпадает
    с ключом, который nanobot создаёт при ``publish_inbound`` (формат
    ``<channel>:<chat_id>``).
    """
    from lib.services.context_compaction import ContextCompactionService

    tokens = command.split()
    idle = any(t in ("idle", "--idle", "-i") for t in tokens[1:])
    svc = ContextCompactionService(agent, settings=None)
    session_key = f"{cli_channel}:{chat_id}"
    report = await svc.compact(session_key=session_key, idle=idle, force=True)
    text = svc.format_report(report)
    if not report.get("ok"):
        console.print(f"[yellow]🗜️ {text}[/yellow]")
    else:
        console.print(f"[cyan]🗜️ {text}[/cyan]")


async def run_repl(
    agent: Any,
    config: Any,
    *,
    session: str | None = None,
    display: DisplayConfig | None = None,
    background_task_factory: Any | None = None,
) -> None:
    """Главный REPL: ввод → publish_inbound → consume_outbound → рендер.

    Args:
        agent: AgentLoop.
        config: runtime config (логотип, пресет).
        session: имя сессии (cli:<session>).
        display: DisplayConfig.
        background_task_factory: callable() → Optional[Task] — фоновая задача
            (например, фоновая подгрузка кеша аудит-навыка).
    """
    from nanobot.bus.events import InboundMessage
    from nanobot.cli.commands import (
        __logo__,
        __version__,
        _init_prompt_session,
        _is_exit_command,
        _model_display,
        _read_interactive_input_async,
        _restore_terminal,
        _sanitize_surrogates,
    )

    cfg = display or DisplayConfig()
    bus = agent.bus
    _init_prompt_session()
    _model, _preset_tag = _model_display(config)
    console.print(
        f"{__logo__} nanobot {__version__} "
        f"Interactive [bold blue]({_model})[/bold blue]{_preset_tag} "
        f"— type [bold]exit[/bold] or [bold]Ctrl+C[/bold] to quit\n"
    )

    chat_id = session or "direct"
    cli_channel = "cli"
    try:
        from config import SETTINGS

        repl_idle_timeout = float(SETTINGS.get("cli", {}).get("repl_idle_timeout_sec", 1.0))
    except Exception:
        repl_idle_timeout = 1.0

    async def consume_outbound() -> tuple:
        full_response = ""
        response_meta: dict = {}
        while True:
            try:
                msg = await asyncio.wait_for(bus.consume_outbound(), timeout=repl_idle_timeout)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                return full_response, response_meta
            meta = msg.metadata or {}
            if meta.get("_reasoning_delta"):
                if msg.content and cfg.show_reasoning:
                    await _typewriter(msg.content, "dim italic", cfg.typewriter_speed)
                continue
            if meta.get("_reasoning_end") or meta.get("_stream_end"):
                continue
            if is_stream_delta(meta):
                if msg.content:
                    sys.stdout.write(msg.content)
                    sys.stdout.flush()
                continue
            if not msg.content or meta.get("_progress") or meta.get("_turn_end") or meta.get("_tool_hint"):
                continue
            full_response = msg.content
            response_meta = meta
            return full_response, response_meta

    bus_task = asyncio.create_task(agent.run())  # noqa: F841 — держит ссылку (anti-GC)
    if background_task_factory is not None:
        try:
            bg = background_task_factory()
            if asyncio.iscoroutine(bg):
                bg = asyncio.create_task(bg)
        except Exception:
            bg = None
    else:
        bg = None

    try:
        while True:
            try:
                user_input = _sanitize_surrogates(await _read_interactive_input_async())
                command = user_input.strip()
                if not command:
                    continue
                if _is_exit_command(command):
                    _restore_terminal()
                    console.print("\nGoodbye!")
                    break
                if command == "/compact" or command.startswith("/compact "):
                    await _run_cli_compact(agent, command, chat_id, cli_channel)
                    continue
                await bus.publish_inbound(InboundMessage(
                    channel=cli_channel,
                    sender_id="user",
                    chat_id=chat_id,
                    content=user_input,
                ))
                content, meta = await consume_outbound()
                if meta.get("_tool_audit"):
                    await _print_tool_events(meta["_tool_audit"], cfg)
                if content and not meta.get("_stream_delta"):
                    await _typewriter(content, "", cfg.typewriter_speed)
                if isinstance(meta.get("context_window"), dict):
                    await _print_context_window(meta["context_window"], cfg)
            except (KeyboardInterrupt, EOFError):
                _restore_terminal()
                console.print("\nGoodbye!")
                break
    finally:
        agent.stop()
        try:
            await agent.close_mcp()
        except Exception:
            pass
        if bg is not None and not bg.done():
            bg.cancel()
        flushed = agent.sessions.flush_all()
        if flushed:
            from loguru import logger
            logger.info("Flushed {} session(s) to disk", flushed)
