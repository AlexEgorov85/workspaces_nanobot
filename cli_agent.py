"""
CLI-агент для nanobot с поддержкой PostgreSQL-хранилища.

══════════════════════════════════════════════════════════════════════════════
НАЗНАЧЕНИЕ
══════════════════════════════════════════════════════════════════════════════

Скрипт запускает nanobot в интерактивном CLI-режиме. Пользователь может
общаться с LLM-агентом напрямую из терминала: вводить сообщения, получать
ответы в реальном времени (со стримингом), использовать инструменты.

В отличие от gateway, этот режим не требует запуска каналов связи — всё
общение происходит через stdin/stdout.

══════════════════════════════════════════════════════════════════════════════
ПАРАМЕТРЫ ЗАПУСКА
══════════════════════════════════════════════════════════════════════════════

  -P, --patched        Включить локальные доработки (см. ниже).
  -S, --storage        Хранилище сессий: auto | file | postgres
                       (только с --patched, по умолчанию auto)

══════════════════════════════════════════════════════════════════════════════
РЕЖИМЫ ЗАПУСКА
══════════════════════════════════════════════════════════════════════════════

1. Без --patched (режим по умолчанию)
   ─────────────────────────────────
   Стандартный CLI-агент, аналогичный `nanobot agent`. Используются
   стандартные JSONL-файлы для истории. Кастомные инструменты из
   workspace/tools/ не сканируются.

2. С --patched (режим с доработками)
   ─────────────────────────────────
   Подключаются:
     • PGSessionManager — хранение истории сессий в PostgreSQL
     • Сканирование workspace/tools/ для поиска кастомных Tool-подклассов
     • Гибкий выбор хранилища через --storage

══════════════════════════════════════════════════════════════════════════════
ПРИМЕРЫ ЗАПУСКА
══════════════════════════════════════════════════════════════════════════════

  # Стандартный запуск (как библиотека nanobot)
  python cli_agent.py

  # С локальными доработками и автоопределением хранилища
  python cli_agent.py --patched

  # Локальные доработки + принудительно PostgreSQL
  python cli_agent.py --patched --storage postgres

  # Локальные доработки + принудительно JSONL-файлы
  python cli_agent.py --patched --storage file
"""

import argparse
import asyncio
import contextlib
import importlib
import signal
import sys
from pathlib import Path

# ── Подключаем директорию скрипта к sys.path, чтобы импортировать ───────────
#    локальные модули: pg_session_manager
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
from nanobot.config.loader import load_config, resolve_config_env_vars
from nanobot.config.paths import is_default_workspace
from nanobot.cron.service import CronService
from nanobot.utils.helpers import sync_workspace_templates

from pg_session_manager import PGSessionManager

# ── Путь к конфигу и кастомным workspace-директориям ─────────────────────────
#    Эти переменные можно менять под своё окружение.
_CONFIG_PATH: str = str(Path(__file__).parent / "config.json")
_WORKSPACE_DIR: Path = Path(__file__).parent / "workspace"
_TOOLS_DIR: Path = _WORKSPACE_DIR / "tools"
_HOOKS_DIR: Path = _WORKSPACE_DIR / "hooks"
sys.path.insert(0, str(_TOOLS_DIR))
sys.path.insert(0, str(_HOOKS_DIR))
sys.path.insert(0, str(_WORKSPACE_DIR))


# ══════════════════════════════════════════════════════════════════════════════
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (используются только в patched-режиме)
# ══════════════════════════════════════════════════════════════════════════════


def _scan_and_register_tools(registry) -> None:
    """
    Сканирует workspace/tools/ на предмет Tool-подклассов и регистрирует их.

    Для каждой поддиректории ищет файл tool.py, импортирует его как модуль
    и извлекает все классы, наследуемые от Tool (но не сам Tool).
    """
    for pkg_dir in sorted(_TOOLS_DIR.iterdir()):
        # Пропускаем скрытые и системные директории
        if not pkg_dir.is_dir() or pkg_dir.name.startswith("_") or pkg_dir.name.startswith("."):
            continue
        tool_file = pkg_dir / "tool.py"
        if not tool_file.exists():
            continue
        try:
            # Импортируем модуль (директория уже в sys.path)
            mod = importlib.import_module(f"{pkg_dir.name}.tool")
        except Exception as exc:
            console.print(f"[yellow]⚠[/yellow] tool.py in {pkg_dir.name}: {exc}")
            continue
        # Ищем Tool-подклассы
        for attr_name in dir(mod):
            attr = getattr(mod, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, Tool)
                and attr is not Tool
                and not attr_name.startswith("_")
            ):
                try:
                    # Регистрируем экземпляр инструмента в реестре агента
                    registry.register(attr())
                    console.print(f"[green]✓[/green] {attr.__name__} registered")
                except Exception as exc:
                    console.print(f"[yellow]⚠[/yellow] {attr.__name__}: {exc}")


def _migrate_cron_store(config) -> None:
    """
    Переносит файл cron-задач из глобальной директории (~/.nanobot/cron/)
    в workspace-специфичную директорию.

    Нужен для совместимости после перехода на мульти-воркспейсную модель.
    """
    from nanobot.config.paths import get_cron_dir

    legacy_path = get_cron_dir() / "jobs.json"
    new_path = config.workspace_path / "cron" / "jobs.json"
    if legacy_path.is_file() and not new_path.exists():
        new_path.parent.mkdir(parents=True, exist_ok=True)
        import shutil

        shutil.move(str(legacy_path), str(new_path))


# ══════════════════════════════════════════════════════════════════════════════
# ОБЩИЙ ИНТЕРАКТИВНЫЙ ЦИКЛ (используется в обоих режимах)
# ══════════════════════════════════════════════════════════════════════════════
#
# Этот цикл:
#   1. Принимает ввод пользователя через prompt_toolkit (с историей, автодополнением)
#   2. Публикует сообщение в шину как InboundMessage
#   3. Ожидает ответ от агента (со стримингом через bus)
#   4. Выводит ответ пользователю в терминал


def _run_interactive_loop(agent, config) -> None:
    """
    Запускает интерактивный цикл ввода-вывода.

    Параметры:
        agent: экземпляр AgentLoop (уже сконфигурированный)
        config: конфигурация nanobot для получения настроек отображения
    """
    from nanobot.bus.events import InboundMessage

    # Получаем ссылку на шину сообщений из агента
    bus = agent.bus

    # ── 1. Инициализация prompt_toolkit ──────────────────────────────────────
    #    Создаём сессию с поддержкой истории, автодополнения и т.д.
    _init_prompt_session()
    _model, _preset_tag = _model_display(config)
    console.print(
        f"Interactive mode [bold blue]({_model})[/bold blue]{_preset_tag} "
        f"\u2014 type [bold]exit[/bold] or [bold]Ctrl+C[/bold] to quit\n"
    )

    # ── 2. Определяем идентификатор сессии и канала ─────────────────────────
    #    Для CLI используем фиксированный session_id = "cli:direct",
    #    из которого извлекаем channel="cli" и chat_id="direct".
    session_id = "cli:direct"
    if ":" in session_id:
        cli_channel, cli_chat_id = session_id.split(":", 1)
    else:
        cli_channel, cli_chat_id = "cli", session_id

    # ── 3. Обработчики сигналов ──────────────────────────────────────────────
    #    Нужны для корректного восстановления терминала при Ctrl+C и т.д.
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

    # ── 4. Асинхронный цикл ──────────────────────────────────────────────────
    async def run_interactive():
        # Запускаем агента в фоновой задаче
        bus_task = asyncio.create_task(agent.run())

        # turn_done — сигнал о завершении обработки текущего сообщения
        turn_done = asyncio.Event()
        turn_done.set()
        turn_response: list[tuple[str, dict]] = []

        # StreamRenderer для стриминга ответов (постепенный вывод токенов)
        renderer: StreamRenderer | None = None

        # ── 4a. Фоновая задача: потребление исходящих сообщений ──────────────
        #     Получает сообщения из шины и выводит их пользователю.
        reasoning_buf: str = ""
        reasoning_active: bool = False

        async def _show_reasoning(finalize: bool = False):
            nonlocal reasoning_buf, reasoning_active
            if not reasoning_active or not reasoning_buf:
                reasoning_buf = ""
                reasoning_active = False
                return
            text = reasoning_buf.strip()
            if not text or not renderer:
                reasoning_buf = ""
                reasoning_active = False
                return
            with renderer.pause_spinner():
                renderer.ensure_header()
                f = renderer.console.file
                if finalize:
                    f.write(f"\r  > {text}\n")
                else:
                    f.write(f"\r  > {text}")
                f.flush()
            if finalize:
                reasoning_buf = ""
                reasoning_active = False

        async def _consume_outbound():
            nonlocal reasoning_buf, reasoning_active
            """
            Бесконечный цикл, который читает сообщения из outbound-очереди bus
            и обрабатывает их: стриминг-дельты, прогресс, финальные ответы.
            """
            while True:
                try:
                    # Ожидаем новое сообщение с таймаутом 1с
                    msg = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue  # Таймаут — просто продолжаем ждать
                except asyncio.CancelledError:
                    break  # Задачу отменили — выходим

                meta = msg.metadata or {}

                # ── Обработка размышлений (reasoning) ─────────────────────────
                if meta.get("_reasoning_delta"):
                    if agent.channels_config and not agent.channels_config.show_reasoning:
                        continue
                    if msg.content:
                        reasoning_buf += msg.content
                        reasoning_active = True
                        await _show_reasoning(finalize=False)
                    continue
                if meta.get("_reasoning_end"):
                    await _show_reasoning(finalize=True)
                    continue

                # ── Обработка стриминговых сообщений ─────────────────────────
                # _stream_delta — очередной фрагмент ответа (стриминг)
                if meta.get("_stream_delta"):
                    await _show_reasoning(finalize=True)
                    if renderer:
                        await renderer.on_delta(msg.content)
                    continue
                # _stream_end — завершение стриминга
                if meta.get("_stream_end"):
                    await _show_reasoning(finalize=True)
                    if renderer:
                        await renderer.on_end(
                            resuming=meta.get("_resuming", False),
                        )
                    continue
                # _streamed — ответ полностью получен (не по фрагментам)
                if meta.get("_streamed"):
                    await _show_reasoning(finalize=True)
                    if renderer and renderer.streamed:
                        turn_done.set()
                        continue
                    # Без стриминга — пропускаем в fallback для вывода контента

                # ── Обработка прогресс-сообщений ─────────────────────────────
                #     (вызовы инструментов и т.д.)
                if await _maybe_print_interactive_progress(
                    msg, None, agent.channels_config, renderer,
                ):
                    continue

                # ── Обработка финального ответа ──────────────────────────────
                if not turn_done.is_set():
                    # Первый ответ с контентом — сохраняем
                    if msg.content:
                        turn_response.append((msg.content, dict(meta)))
                    turn_done.set()
                elif msg.content:
                    # Последующие ответы (отложенные сообщения) — выводим сразу
                    await _print_agent_response(
                        msg.content,
                        render_markdown=True,
                        metadata=meta,
                    )

        outbound_task = asyncio.create_task(_consume_outbound())

        # ── 4b. Основной цикл ввода-вывода ──────────────────────────────────
        try:
            while True:
                try:
                    # Сбрасываем накопленный ввод (нажатия во время генерации)
                    _flush_pending_tty_input()
                    if renderer:
                        renderer.stop_for_input()

                    # Читаем ввод пользователя через prompt_toolkit
                    user_input = _sanitize_surrogates(await _read_interactive_input_async())
                    command = user_input.strip()
                    if not command:
                        continue

                    # Проверяем команды выхода
                    if _is_exit_command(command):
                        _restore_terminal()
                        console.print("\nGoodbye!")
                        break

                    # Сбрасываем состояние перед новым запросом
                    turn_done.clear()
                    turn_response.clear()
                    reasoning_buf = ""
                    reasoning_active = False
                    renderer = StreamRenderer(
                        render_markdown=True,
                        bot_name=config.agents.defaults.bot_name,
                        bot_icon=config.agents.defaults.bot_icon,
                    )

                    # Публикуем сообщение в шину для агента
                    await bus.publish_inbound(InboundMessage(
                        channel=cli_channel,
                        sender_id="user",
                        chat_id=cli_chat_id,
                        content=user_input,
                        metadata={"_wants_stream": True},  # Запрашиваем стриминг
                    ))

                    # Ждём завершения обработки (turn_done)
                    await turn_done.wait()

                    # ── Вывод финального ответа ──────────────────────────────
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
            # ── 4c. Очистка при выходе ───────────────────────────────────────
            agent.stop()
            outbound_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await outbound_task
            await agent.close_mcp()
            # Сохраняем все кэшированные сессии на диск
            flushed = agent.sessions.flush_all()
            if flushed:
                logger.info("Flushed {} session(s) to disk", flushed)

    asyncio.run(run_interactive())


# ══════════════════════════════════════════════════════════════════════════════
# РЕЖИМ "VANILLA" — СТАНДАРТНЫЙ ЗАПУСК БЕЗ ДОРАБОТОК
# ══════════════════════════════════════════════════════════════════════════════
#
# В этом режиме не используются PGSessionManager и кастомные инструменты.
# Всё работает как в стандартном `nanobot agent`.


def _run_vanilla_agent() -> None:
    """
    Запускает стандартный CLI-агент (как `nanobot agent` из библиотеки).

    Без PGSessionManager, без сканирования кастомных инструментов.
    История сессий хранится в JSONL-файлах (стандартный SessionManager).
    """
    # Загружаем конфигурацию
    config = _load_runtime_config(config=_CONFIG_PATH, workspace=str(_WORKSPACE_DIR))
    # Синхронизируем шаблоны workspace
    sync_workspace_templates(config.workspace_path)
    console.print(f"{__logo__} Starting nanobot CLI agent v{__version__}...")

    # Создаём шину сообщений
    bus = MessageBus()

    # Миграция cron-задач (если нужно)
    if is_default_workspace(config.workspace_path):
        _migrate_cron_store(config)

    # Создаём сервис cron для периодических задач
    cron_store_path = config.workspace_path / "cron" / "jobs.json"
    cron = CronService(cron_store_path)

    # Создаём AgentLoop без кастомного session_manager.
    # Используется стандартный SessionManager из библиотеки (JSONL-файлы).
    agent = AgentLoop.from_config(
        config, bus,
        cron_service=cron,
    )

    _run_interactive_loop(agent, config)


# ══════════════════════════════════════════════════════════════════════════════
# РЕЖИМ "PATCHED" — ЗАПУСК С ЛОКАЛЬНЫМИ ДОРАБОТКАМИ
# ══════════════════════════════════════════════════════════════════════════════
#
# В этом режиме подключаются PGSessionManager и сканирование кастомных
# инструментов из workspace/tools/.


def _run_patched_agent(args: argparse.Namespace) -> None:
    """
    Запускает CLI-агента с локальными доработками.

    Параметры:
        args: распарсенные аргументы командной строки (storage)
    """
    # ── 1. Загрузка конфигурации и подготовка workspace ──────────────────────
    config = _load_runtime_config(config=_CONFIG_PATH, workspace=str(_WORKSPACE_DIR))
    sync_workspace_templates(config.workspace_path)
    console.print(f"{__logo__} Starting nanobot CLI agent v{__version__}...")

    # Создаём шину сообщений
    bus = MessageBus()

    # Миграция cron-задач (если нужно)
    if is_default_workspace(config.workspace_path):
        _migrate_cron_store(config)

    # Создаём сервис cron
    cron_store_path = config.workspace_path / "cron" / "jobs.json"
    cron = CronService(cron_store_path)

    # ── 2. Выбор хранилища сессий ────────────────────────────────────────────
    #    Достаём DSN для PostgreSQL из конфига (секция channels.postgres)
    pg_cfg = getattr(config.channels, "postgres", {})
    dsn = pg_cfg.get("dsn", "") if isinstance(pg_cfg, dict) else getattr(pg_cfg, "dsn", "")

    # Логика выбора:
    #   postgres → принудительно PG (ошибка если нет DSN)
    #   auto     → PG если есть DSN, иначе None (стандартный SessionManager)
    #   file     → принудительно None (стандартный SessionManager)
    use_postgres = args.storage == "postgres" or (args.storage == "auto" and bool(dsn))
    if use_postgres:
        if not dsn:
            console.print("[red]✗[/red] --storage=postgres but no PostgreSQL DSN in config")
            sys.exit(1)
        session_manager = PGSessionManager(
            workspace=config.workspace_path,
            dsn=dsn,
        )
        # Создаём таблицы в БД, если их ещё нет
        session_manager.ensure_tables()
        console.print("[green]✓[/green] PGSessionManager: sessions stored in PostgreSQL")
    else:
        session_manager = None
        if dsn:
            console.print("[dim]PostgreSQL DSN available but --storage=file; using JSONL files[/dim]")

    # ── 3. Создание AgentLoop ────────────────────────────────────────────────
    agent = AgentLoop.from_config(
        config, bus,
        cron_service=cron,
        session_manager=session_manager,
    )

    # ── 4. Сканирование кастомных инструментов ────────────────────────────────
    _scan_and_register_tools(agent.tools)

    # ── 5. Запуск интерактивного цикла ───────────────────────────────────────
    _run_interactive_loop(agent, config)


# ══════════════════════════════════════════════════════════════════════════════
# ТОЧКА ВХОДА
# ══════════════════════════════════════════════════════════════════════════════
#
# Парсинг аргументов и выбор режима запуска (vanilla / patched).


def _parse_args() -> argparse.Namespace:
    """Парсит аргументы командной строки."""
    parser = argparse.ArgumentParser(
        description="nanobot CLI agent — upstream by default, --patched for local extras",
    )
    parser.add_argument(
        "--patched", "-P",
        action="store_true",
        default=False,
        help="Enable local patches: PGSessionManager, workspace tool scanning, etc.",
    )
    parser.add_argument(
        "--storage", "-S",
        type=str,
        default="auto",
        choices=("auto", "file", "postgres"),
        help="Session storage backend. (only with --patched, default: auto)",
    )
    return parser.parse_args()


def main():
    """
    Главная функция: выбирает режим запуска в зависимости от флага --patched.

    Без --patched → стандартный CLI-агент (как `nanobot agent`).
    С --patched  → запуск с PGSessionManager и кастомными инструментами.
    """
    args = _parse_args()
    if args.patched:
        _run_patched_agent(args)
    else:
        _run_vanilla_agent()


if __name__ == "__main__":
    main()
