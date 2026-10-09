"""ConsoleLoop — интерактивный REPL CLI-агента поверх MessageBus + AgentLoop.

Структура REPL — зеркало upstream ``run_interactive`` из
``nanobot/cli/agent.py`` (HKUDS/nanobot @ main). Не изобретаем —
делегируем:

  * ``cli_terminal._init_prompt_session`` / ``_read_interactive_input_async`` /
    ``_is_exit_command`` / ``_restore_terminal`` / ``_flush_pending_tty_input`` /
    ``_print_agent_response`` / ``_print_interactive_response`` /
    ``_maybe_print_interactive_progress`` / ``_ReasoningBuffer`` —
    все через ``lib.cli.nanobot_cli_compat.get_repl_helpers()`` (где
    уже сделана кросс-версионная адаптация для nanobot 0.3.0..0.3.5).

Единственное наше расширение: ``turn_wait_timeout`` на
``turn_done.wait()`` как safety net — если LLM полностью отказал
(нет ``StreamedResponseEvent`` после ``RetryWaitEvent``), REPL печатает
fallback и возвращается к ``You:``. Upstream полагается на Ctrl+C.

Отличия от upstream ``run_interactive``:

  * Создание ``agent_loop`` идёт через наш ``ApplicationContext`` —
    ``agent`` уже передан параметром, не создаётся внутри.
  * ``StreamRenderer`` импортируется опционально (graceful fallback
    если upstream stream-модуль недоступен).
  * Наши runtime-patch metadata (``_tool_audit``, ``context_window``)
    извлекаются из ``StreamedResponseEvent.metadata`` ПОСЛЕ основного
    рендера upstream'а и печатаются как пост-блоки.
"""

from __future__ import annotations

import asyncio
from typing import Any

from rich.console import Console

from lib.cli.display_config import DisplayConfig
from lib.utils.windows_terminal import enable_vt, is_windows_console

console = Console()


# ---------------------------------------------------------------------------
# Runtime-patch display helpers (наши, не покрыты upstream).
# ``_tool_audit`` / ``context_window`` кладёт в ``metadata``
# ``RuntimePatcher.patch_assemble_outbound`` (см. ``lib/services/runtime_patcher.py``).
# Upstream эти поля НЕ читает — рендерим сами после финального ответа.
# ---------------------------------------------------------------------------


async def _print_tool_events(events: list, cfg: DisplayConfig) -> None:
    """Рендер ``_tool_audit`` блока (per-call tool events)."""
    if not cfg.show_tool_calls or not events:
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
            console.print(f"[dim]{label}[/dim]")
        elif status == "error":
            err = ev.get("error", "failed")
            if params_str:
                label = f"✗ {name}({params_str}): {err}"
            else:
                label = f"✗ {name}: {err}"
            console.print(f"[dim]{label}[/dim]")


async def _print_context_window(block: Any, cfg: DisplayConfig) -> None:
    """Рендер ``context_window`` блока (UI индикатор занятости)."""
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


# ---------------------------------------------------------------------------
# /compact — наша CLI-команда (upstream её не обрабатывает)
# ---------------------------------------------------------------------------


async def _run_cli_compact(
    agent: Any,
    command: str,
    chat_id: str,
    cli_channel: str,
) -> None:
    """CLI-команда ``/compact``: локальное сжатие контекста."""
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


# ---------------------------------------------------------------------------
# REPL — структура upstream ``run_interactive`` (см. ``nanobot/cli/agent.py``)
# ---------------------------------------------------------------------------


async def run_repl(
    agent: Any,
    config: Any,
    *,
    session: str | None = None,
    display: DisplayConfig | None = None,
    background_task_factory: Any | None = None,
) -> None:
    """Главный REPL — копия upstream ``run_interactive``.

    Args:
        agent: AgentLoop (создан через наш ApplicationContext).
        config: runtime config (логотип, пресет).
        session: имя сессии (cli:<session>).
        display: DisplayConfig.
        background_task_factory: callable() → Optional[Task].
    """
    from nanobot.bus.events import InboundMessage
    from nanobot.bus.outbound_events import (
        StreamDeltaEvent,
        StreamedResponseEvent,
        StreamEndEvent,
    )

    from lib.cli.nanobot_cli_compat import (
        get_logo_version,
        get_repl_helpers,
        model_display,
    )

    # Upstream helpers — все через ``get_repl_helpers`` (см. docs в
    # ``nanobot_cli_compat.py``).
    _helpers = get_repl_helpers()
    _init_prompt_session = _helpers["_init_prompt_session"]
    _is_exit_command = _helpers["_is_exit_command"]
    _read_interactive_input_async = _helpers["_read_interactive_input_async"]
    _restore_terminal = _helpers["_restore_terminal"]
    _flush_pending_tty_input = _helpers.get(
        "_flush_pending_tty_input", lambda: None,
    )
    _sanitize_surrogates = _helpers["_sanitize_surrogates"]
    _print_agent_response = _helpers["_print_agent_response"]
    _print_interactive_response = _helpers["_print_interactive_response"]
    _maybe_print_interactive_progress = _helpers[
        "_maybe_print_interactive_progress"
    ]
    _ReasoningBuffer = _helpers["_ReasoningBuffer"]

    # Upstream ``run_interactive`` создаёт ``StreamRenderer`` на каждый
    # turn. ``StreamRenderer`` — public в upstream ``nanobot.cli.stream``.
    # Импортируем опционально (graceful fallback на None, если
    # upstream-модуль недоступен).
    try:
        from nanobot.cli.stream import StreamRenderer as _StreamRenderer
    except Exception:
        _StreamRenderer = None  # type: ignore[misc, assignment]

    cfg = display or DisplayConfig()
    bus = agent.bus
    channels_config = getattr(agent, "channels_config", None)
    markdown = getattr(config, "markdown", True)
    if not isinstance(markdown, bool):
        markdown = True

    _init_prompt_session()
    # prompt_toolkit на старте может сбросить ENABLE_VIRTUAL_TERMINAL_PROCESSING →
    # Rich вывод уезжает с ANSI в legacy cmd/PowerShell ISE как ``?[2m...``.
    if is_windows_console():
        enable_vt()

    __logo__, __version__ = get_logo_version()
    _model, _preset_tag = model_display(config)
    _icon = getattr(config.agents.defaults, "bot_icon", None) or __logo__
    console.print(
        f"{_icon} nanobot {__version__} "
        f"Interactive [bold blue]({_model})[/bold blue]{_preset_tag} "
        f"— type [bold]exit[/bold] or [bold]Ctrl+C[/bold] to quit\n"
    )

    if session and ":" in session:
        cli_channel, chat_id = session.split(":", 1)
    else:
        cli_channel, chat_id = "cli", session or "direct"

    # Лимит ожидания финального ответа. Upstream этого нет (полагается
    # на Ctrl+C); мы добавляем safety net.
    turn_wait_timeout = 300.0
    try:
        from config import SETTINGS

        turn_wait_timeout = float(
            SETTINGS.get("cli", {}).get("turn_wait_timeout_sec", turn_wait_timeout)
        )
    except Exception:
        pass

    # REPL state — инициализация один раз (см. upstream).
    turn_done = asyncio.Event()
    turn_done.set()
    turn_response: list[Any] = []
    reasoning_buffer = _ReasoningBuffer()
    renderer = None

    # ``_consume_outbound`` — точная копия upstream ``run_interactive``
    # (см. ``nanobot/cli/agent.py``).
    async def _consume_outbound() -> None:
        nonlocal renderer
        while True:
            try:
                msg = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            event = msg.event

            if isinstance(event, StreamDeltaEvent):
                if renderer:
                    await renderer.on_delta(msg.content)
                continue
            if isinstance(event, StreamEndEvent):
                if renderer:
                    await renderer.on_end(resuming=event.resuming)
                continue
            if isinstance(event, StreamedResponseEvent):
                if msg.content and renderer and not getattr(renderer, "streamed", True):
                    try:
                        await renderer.close()
                    except Exception:
                        pass
                    print_kwargs: dict[str, Any] = {}
                    if getattr(renderer, "header_printed", False):
                        print_kwargs["show_header"] = False
                    _print_agent_response(
                        msg.content,
                        render_markdown=markdown,
                        metadata=msg.metadata,
                        **print_kwargs,
                    )
                turn_done.set()
                continue

            try:
                handled = await _maybe_print_interactive_progress(
                    msg,
                    None,
                    channels_config,
                    renderer,
                    reasoning_buffer,
                )
            except Exception:
                handled = False
            if handled:
                continue

            if not turn_done.is_set():
                if msg.content:
                    turn_response.append(msg)
                turn_done.set()
            elif msg.content:
                try:
                    await _print_interactive_response(
                        msg.content,
                        render_markdown=markdown,
                        metadata=msg.metadata,
                    )
                except Exception:
                    pass

    bus_task = asyncio.create_task(agent.run())  # noqa: F841 — anti-GC ref
    outbound_task = asyncio.create_task(_consume_outbound())

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
                try:
                    _flush_pending_tty_input()
                except Exception:
                    pass
                if renderer and hasattr(renderer, "stop_for_input"):
                    try:
                        renderer.stop_for_input()
                    except Exception:
                        pass

                user_input = _sanitize_surrogates(
                    await _read_interactive_input_async()
                )
                if is_windows_console():
                    enable_vt()

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

                # Начинаем turn: сбрасываем state + создаём renderer.
                turn_done.clear()
                turn_response.clear()
                reasoning_buffer.clear()
                if _StreamRenderer is not None:
                    try:
                        renderer = _StreamRenderer(
                            render_markdown=markdown,
                            bot_name=getattr(
                                config.agents.defaults, "bot_name", None,
                            ),
                            bot_icon=getattr(
                                config.agents.defaults, "bot_icon", None,
                            ),
                        )
                    except Exception:
                        renderer = None
                else:
                    renderer = None

                await bus.publish_inbound(
                    InboundMessage(
                        channel=cli_channel,
                        sender_id="user",
                        chat_id=chat_id,
                        content=user_input,
                        metadata={"_wants_stream": True},
                    )
                )

                # Ждём StreamedResponseEvent (финальный ответ). Upstream
                # ждёт бесконечно; мы добавляем safety net.
                try:
                    await asyncio.wait_for(
                        turn_done.wait(), timeout=turn_wait_timeout,
                    )
                except TimeoutError:
                    console.print(
                        f"[red](нет ответа {turn_wait_timeout:.0f}s — "
                        "проверьте LLM connectivity)[/red]"
                    )
                    continue

                # Рендер результата turn'а — копия upstream ``run_interactive``.
                if turn_response:
                    response_msg = turn_response[0]
                    content = response_msg.content
                    meta = response_msg.metadata
                    if content and not isinstance(
                        response_msg.event, StreamedResponseEvent,
                    ):
                        # Fallback для сообщений, не идущих через
                        # StreamedResponseEvent — upstream печатает
                        # через ``_print_agent_response``.
                        if renderer:
                            try:
                                await renderer.close()
                            except Exception:
                                pass
                        print_kwargs: dict[str, Any] = {}
                        if renderer and getattr(
                            renderer, "header_printed", False,
                        ):
                            print_kwargs["show_header"] = False
                        try:
                            _print_agent_response(
                                content,
                                render_markdown=markdown,
                                metadata=meta,
                                **print_kwargs,
                            )
                        except Exception:
                            console.print(content)
                elif renderer and not getattr(renderer, "streamed", True):
                    try:
                        await renderer.close()
                    except Exception:
                        pass

                # Наши runtime-patch metadata (``_tool_audit``,
                # ``context_window``) — извлекаем из
                # ``StreamedResponseEvent.metadata`` или fallback-пути
                # (``turn_response[0].metadata``). Печатаем как пост-блоки.
                tool_audit: list | None = None
                context_window: dict | None = None
                if turn_response:
                    meta_dict = turn_response[0].metadata or {}
                    tool_audit = meta_dict.get("_tool_audit")
                    context_window = meta_dict.get("context_window")
                # Если основной путь был через StreamedResponseEvent, то
                # ``turn_response`` пуст, но ``agent_loop`` уже отрендерил
                # ответ через ``_print_agent_response``. Наши поля мы НЕ
                # получаем в этом пути — известный gap (upstream не
                # передаёт ``turn_response`` через ``StreamedResponseEvent``
                # handler). TODO: расширить upstream pattern, чтобы
                # ``StreamedResponseEvent`` тоже мержил metadata в
                # общий turn-state.

                if tool_audit:
                    await _print_tool_events(tool_audit, cfg)
                if context_window:
                    await _print_context_window(context_window, cfg)
            except (KeyboardInterrupt, EOFError):
                _restore_terminal()
                console.print("\nGoodbye!")
                break
    finally:
        outbound_task.cancel()
        try:
            await outbound_task
        except (asyncio.CancelledError, Exception):
            pass
        try:
            await asyncio.gather(bus_task, return_exceptions=True)
        except Exception:
            pass
        # ``AgentLoop.close_mcp`` удалён в nanobot 0.3.5 — shutdown-API
        # это ``aclose()`` (контракт: openspec/specs/runtime/agent-hooks/spec.md:37-45).
        # Раньше здесь стоял только ``agent.stop()``, и MCP-ресурсы не
        # освобождались при выходе из REPL.
        try:
            await agent.aclose()
        except Exception as exc:
            from loguru import logger

            logger.warning("agent.aclose() failed during REPL shutdown: {}", exc)
        agent.stop()
        if bg is not None and not bg.done():
            bg.cancel()
        flushed = agent.sessions.flush_all()
        if flushed:
            from loguru import logger

            logger.info("Flushed {} session(s) to disk", flushed)
