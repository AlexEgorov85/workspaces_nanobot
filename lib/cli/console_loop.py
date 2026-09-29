"""ConsoleLoop — интерактивный REPL CLI-агента поверх MessageBus + AgentLoop.

Переиспользует upstream-хелперы ``nanobot.cli.terminal``:

  * ``_ReasoningBuffer`` — буферизация reasoning с sentence-boundary flush.
  * ``_maybe_print_interactive_progress`` — обработка ``ProgressEvent``
    по флагам (``reasoning_delta`` / ``reasoning_end`` / ``tool_hint`` /
    ``send_progress``), а также ``ContextCompactionEvent`` и
    ``RetryWaitEvent``.
  * ``_print_agent_response`` — рендер финального ответа с markdown и
    стандартным header.
  * ``_print_interactive_response`` — рендер интерактивных сообщений,
    не идущих в основной ответ.

Структура REPL-цикла — зеркало upstream ``nanobot/cli/agent.py``:

  1. На старте: ``turn_done.set()``, ``turn_response.clear()``, создать
     ``_ReasoningBuffer`` + ``StreamRenderer`` (на каждый turn).
  2. Per turn:
     a. Очистить ``turn_done``, ``turn_response``, ``reasoning_buffer``.
     b. ``renderer = StreamRenderer(...)`` (новый на каждый turn, см.
        upstream).
     c. ``bus.publish_inbound(InboundMessage(...))`` с metadata
        ``_wants_stream=True``.
     d. ``await turn_done.wait()`` (с timeout — наш safety net на случай
        полного отказа LLM).
     e. Render ответа через ``_print_agent_response``.
  3. На ``KeyboardInterrupt`` / ``EOFError`` — restore_terminal, Goodbye,
     cancel outbound task, agent.stop.

Единственное наше расширение — timeout на ``turn_done.wait()``. Upstream
кода этого не делает (полагается на Ctrl+C); у нас явный fallback
(печать "(нет ответа Ns — проверьте LLM connectivity)") чтобы REPL не
висел навсегда.

Локальные хелперы, которые upstream НЕ покрывает:

  * ``_print_tool_events`` — рендер ``_tool_audit`` из metadata
    (RuntimePatcher.patch_assemble_outbound кладёт).
  * ``_print_context_window`` — рендер ``context_window`` блока (UI
    состояния контекста из того же patch).

Эти двое оставлены, так как они часть наших runtime-patches, не upstream.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from rich.console import Console

from lib.cli.display_config import DisplayConfig
from lib.utils.windows_terminal import enable_vt, is_windows_console

console = Console()


# ---------------------------------------------------------------------------
# Наши runtime-patch helpers (не покрыты upstream)
# ---------------------------------------------------------------------------


async def _print_tool_events(events: list, cfg: DisplayConfig) -> None:
    """Рендер ``_tool_audit`` блока из ``metadata``.

    ``RuntimePatcher.patch_assemble_outbound`` собирает события tool'ов
    (call/ok/error) и кладёт их в ``metadata["_tool_audit"]``. Здесь
    рисуем их dim-italic'ом.
    """
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
            console.print(f"[dim]{label}[/dim]")
        elif status == "error":
            err = ev.get("error", "failed")
            if params_str:
                label = f"✗ {name}({params_str}): {err}"
            else:
                label = f"✗ {name}: {err}"
            console.print(f"[dim]{label}[/dim]")


async def _print_context_window(block: Any, cfg: DisplayConfig) -> None:
    """Рендер ``context_window`` блока (UI индикатор занятости окна).

    ``{used, limit, pct, model}`` кладётся в ``metadata.context_window``
    патчем ``RuntimePatcher.patch_assemble_outbound``.
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
    """CLI-команда ``/compact``: локальное сжатие контекста.

    Upstream ``run_interactive`` не обрабатывает ``/compact`` —
    это особенность нашего CLI. Использует ``ContextCompactionService``
    напрямую с ``force=True``.
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


# ---------------------------------------------------------------------------
# REPL — адаптация upstream ``run_interactive`` (см. ``nanobot/cli/agent.py``)
# ---------------------------------------------------------------------------


async def run_repl(
    agent: Any,
    config: Any,
    *,
    session: str | None = None,
    display: DisplayConfig | None = None,
    background_task_factory: Any | None = None,
) -> None:
    """Главный REPL: ввод → publish_inbound → ждать turn_done → рендер.

    Args:
        agent: AgentLoop.
        config: runtime config (логотип, пресет).
        session: имя сессии (cli:<session>).
        display: DisplayConfig.
        background_task_factory: callable() → Optional[Task] — фоновая задача.
    """
    from nanobot.bus.events import InboundMessage
    from nanobot.bus.outbound_events import (
        StreamDeltaEvent,
        StreamedResponseEvent,
    )
    from nanobot.cli import terminal as cli_terminal

    from lib.cli.nanobot_cli_compat import (
        get_logo_version,
        get_repl_helpers,
        model_display,
    )

    _helpers = get_repl_helpers()
    _init_prompt_session = _helpers["_init_prompt_session"]
    _is_exit_command = _helpers["_is_exit_command"]
    _read_interactive_input_async = _helpers["_read_interactive_input_async"]
    _restore_terminal = _helpers["_restore_terminal"]
    _sanitize_surrogates = _helpers["_sanitize_surrogates"]

    cfg = display or DisplayConfig()
    bus = agent.bus
    channels_config = getattr(agent, "channels_config", None)

    _init_prompt_session()
    # prompt_toolkit на старте может сбросить ENABLE_VIRTUAL_TERMINAL_PROCESSING →
    # последующий Rich-вывод уезжает с ANSI в legacy cmd/PowerShell ISE как
    # ``?[2m...``. Возвращаем режим.
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

    # ``cli_terminal._flush_pending_tty_input()`` — upstream вызывает
    # перед каждым ``_read_interactive_input_async()``. Гарантирует, что
    # pending stdin flush до пользователя не приходит после ответа LLM.

    # Upstream ``run_interactive`` создаёт ``StreamRenderer`` на каждый
    # turn. У нас нет его public — берём из upstream если доступен,
    # иначе fallback на None (cli_terminal handles renderer=None
    # graceful).
    try:
        from nanobot.cli.stream import StreamRenderer  # noqa: F401
        _StreamRenderer = StreamRenderer
    except Exception:
        _StreamRenderer = None

    # Лимит ожидания финального ответа на один turn. Upstream кода
    # этого нет (полагается на Ctrl+C); мы добавляем safety net, чтобы
    # REPL не висел при полном отказе LLM. Значение больше upstream
    # retry-таймаута (~120-180 сек для 3 attempt'ов).
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
    reasoning_buffer = cli_terminal._ReasoningBuffer()
    renderer = None  # создаётся на каждый turn ниже, если ``_StreamRenderer`` доступен

    async def _consume_outbound() -> None:
        """Постоянный consumer — копия upstream ``_consume_outbound``.

        Не поднимает ``turn_done`` для terminal events, кроме
        ``StreamedResponseEvent``. ``StreamDeltaEvent`` пишет в
        renderer.on_delta (если есть). ``ProgressEvent`` /
        ``ContextCompactionEvent`` / ``RetryWaitEvent`` — через
        upstream ``_maybe_print_interactive_progress`` (он сам
        диспатчит по флагам ProgressEvent и обрабатывает compaction/
        retry через ``_print_interactive_progress_line``).
        """
        nonlocal renderer
        while True:
            try:
                msg = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            event = msg.event

            if isinstance(event, StreamDeltaEvent):
                if renderer:
                    await renderer.on_delta(msg.content)
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
                    cli_terminal._print_agent_response(
                        msg.content,
                        render_markdown=True,
                        metadata=msg.metadata,
                        **print_kwargs,
                    )
                turn_done.set()
                continue

            # ProgressEvent / ContextCompactionEvent / RetryWaitEvent /
            # GoalStatusEvent / TurnEndEvent / … — upstream обрабатывает.
            try:
                handled = await cli_terminal._maybe_print_interactive_progress(
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

            # Не terminal и не progress → это, видимо, финальный
            # ответ (fallback path из upstream).
            if not turn_done.is_set():
                if msg.content:
                    turn_response.append(msg)
                turn_done.set()
            elif msg.content:
                try:
                    await cli_terminal._print_interactive_response(
                        msg.content,
                        render_markdown=True,
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
                    cli_terminal._flush_pending_tty_input()
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
                            render_markdown=True,
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

                # Ждём StreamedResponseEvent (финальный ответ).
                try:
                    await asyncio.wait_for(
                        turn_done.wait(), timeout=turn_wait_timeout,
                    )
                except TimeoutError:
                    # LLM не вернул ответ за turn_wait_timeout сек (например,
                    # все retry-attempt'ы провалились). Не блокируем REPL —
                    # печатаем placeholder, идём дальше. LLM error уже
                    # залогирован выше (retry warnings в stderr).
                    console.print(
                        f"[red](нет ответа {turn_wait_timeout:.0f}s — "
                        "проверьте LLM connectivity)[/red]"
                    )
                    continue

                # Рендер результата turn'а. ``StreamedResponseEvent`` уже
                # отрендерен внутри consumer'а через ``_print_agent_response``.
                # Здесь обрабатываем только fallback-путь (turn_response
                # пришёл НЕ через StreamedResponseEvent).
                if turn_response:
                    response_msg = turn_response[0]
                    content = response_msg.content
                    meta = response_msg.metadata
                    if content:
                        try:
                            cli_terminal._print_agent_response(
                                content, render_markdown=True, metadata=meta,
                            )
                        except Exception:
                            console.print(content)

                # Наши runtime-patch metadata: tool audit + context window.
                meta_of_msg = (
                    turn_response[0].metadata if turn_response else None
                ) if turn_response else None
                meta_dict: dict = meta_of_msg or {}
                if "_tool_audit" in meta_dict:
                    await _print_tool_events(meta_dict["_tool_audit"], cfg)
                if "context_window" in meta_dict:
                    await _print_context_window(meta_dict["context_window"], cfg)
            except (KeyboardInterrupt, EOFError):
                _restore_terminal()
                console.print("\nGoodbye!")
                break
    finally:
        # Закрываем consumer первым (он ждёт bus.consume_outbound).
        outbound_task.cancel()
        try:
            await outbound_task
        except (asyncio.CancelledError, Exception):
            pass
        try:
            await asyncio.gather(bus_task, return_exceptions=True)
        except Exception:
            pass
        agent.stop()
        if bg is not None and not bg.done():
            bg.cancel()
        flushed = agent.sessions.flush_all()
        if flushed:
            from loguru import logger

            logger.info("Flushed {} session(s) to disk", flushed)
