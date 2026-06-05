"""
CLI-агент для nanobot — интерактивный терминальный интерфейс к LLM-агенту.

╔══════════════════════════════════════════════════════════════════════════╗
║                          АРХИТЕКТУРА                                   ║
╚══════════════════════════════════════════════════════════════════════════╝

cli_agent.py — это точка входа для запуска nanobot в режиме прямого
общения через терминал (stdin/stdout), без использования Telegram, Slack
или других каналов связи.

Скрипт имеет два режима работы:

  vanilla
    Стандартный `nanobot agent` без каких-либо доработок.
    Сессии хранятся в JSONL-файлах (стандартный SessionManager).
    Используется для отладки и тестирования базовой функциональности.

  patched  (--patched, -P)
    Расширенная версия с локальными доработками:
      • PGSessionManager — хранение истории сессий в PostgreSQL
      • AgentHook-хуки из workspace/hooks/ — автоматически загружаются
        и подключаются к циклу агента

    patched-режим НЕ включает сканирование инструментов — все инструменты
    должны быть объявлены через стандартный механизм nanobot (config.json).


╔══════════════════════════════════════════════════════════════════════════╗
║                          ЖИЗНЕННЫЙ ЦИКЛ                                ║
╚══════════════════════════════════════════════════════════════════════════╝

  1. Загрузка конфига
     Читается config.json из той же директории, что и cli_agent.py.
     Разрешаются переменные окружения через resolve_config_env_vars().

  2. Создание шины сообщений (MessageBus)
     Центральная шина, через которую агент и CLI обмениваются сообщениями.
     Inbound  → пользователь → агент
     Outbound → агент → пользователь

  3. Выбор хранилища сессий (только patched)
     • --storage=postgres  → принудительно PGSessionManager
     • --storage=file      → принудительно JSONL-файлы
     • --storage=auto      → PG если есть dsn в конфиге, иначе JSONL

  4. Загрузка хуков (только patched)
     Сканируется workspace/hooks/*.py, ищутся AgentHook-подклассы.
     Хуки подключаются к циклу агента (до/после вызова LLM, после
     вызова инструментов и т.д.)

  5. Создание AgentLoop
     Главный цикл агента: получает сообщения из шины, отправляет их
     в LLM, обрабатывает вызовы инструментов, публикует ответы.

  6. Интерактивный цикл (_run_interactive_loop)
     ┌─────────────────────────────────────────────────────┐
     │  while True:                                        │
     │    read_input() → publish_inbound() → consume()     │
     │                                                     │
     │    consume_outbound():                              │
     │      reasoning → tool_events → progress → response  │
     │                                                     │
     │    _print_agent_response(content)                   │
     └─────────────────────────────────────────────────────┘

     Цикл не использует стриминг — агент возвращает полный ответ.
     Все промежуточные сообщения (размышления, вызовы инструментов)
     буферизируются и выводятся с псевдо-стримингом (пауза перед
     выводом каждого блока). Прогресс-бары показываются по мере
     поступления.


╔══════════════════════════════════════════════════════════════════════════╗
║                      ФОРМАТ OUTBOUND-СООБЩЕНИЙ                         ║
╚══════════════════════════════════════════════════════════════════════════╝

Агент публикует в outbound-очередь сообщения с метаданными, которые
позволяют CLI различать типы сообщений:

  _reasoning_delta
    Фрагмент текста размышлений LLM (chain-of-thought).
    Приходит частями. CLI буферизирует всё до _reasoning_end,
    затем выводит целиком с паузой ``len(text) * speed``.

  _reasoning_end
    Сигнал, что размышления завершены. Сбрасывается буфер.

  _tool_events
    Массив событий вызова инструментов. Каждый элемент:
      { "name": "<tool_name>", "phase": "end"|"error", "result": ..., "error": "..." }
    CLI выводит: "✓ tool_name → result" или "✗ tool_name: error"

  _tool_hint
    UI-подсказки (визуальные разделители) — игнорируются (всегда).

  Обычное сообщение (без служебных меток)
    Считается финальным ответом. После его получения consume_outbound()
    сначала сбрасывает накопленные tool_events (если есть),
    затем завершает работу, и ответ выводится через _typewriter()
    или _print_agent_response().


╔══════════════════════════════════════════════════════════════════════════╗
║                     СТРУКТУРА ФАЙЛА                                    ║
╚══════════════════════════════════════════════════════════════════════════╝

  1. Импорты и константы
     ─────────────────────
     _CONFIG_PATH   — путь к config.json
     _WORKSPACE_DIR — корень workspace (tools/, hooks/, data_store/)
     _HOOKS_DIR     — директория с AgentHook-файлами

  2. Пользовательская конфигурация
     ──────────────────────────────
     DISPLAY        — настройки вывода (блоки, typewriter)
     LLM_TIMEOUT    — таймаут LLM вызова (сек)
     EXEC_TIMEOUT   — таймаут exec-скриптов (сек)
     MAX_ITERATIONS — макс. итераций инструментов
     LOG_LEVEL      — уровень лога в stderr (WARNING подавляет INFO)

  3. Вспомогательные функции
     ─────────────────────────
     _scan_and_register_hooks()  — сканирует hooks/ и загружает хуки
     _migrate_cron_store()      — перенос cron-задач в workspace

  4. Интерактивный цикл
     ────────────────────
     _run_interactive_loop()    — главный цикл ввода-вывода
       └─ run_interactive()     — асинхронная обёртка
           ├─ agent.run()       — фоновая задача агента
           ├─ consume_outbound()— чтение и обработка ответов
           └─ while True:       — ввод → публикация → вывод

  5. Режимы запуска
     ────────────────
     _run_vanilla_agent()       — стандартный запуск
     _run_patched_agent()       — запуск с доработками

  6. Точка входа
     ─────────────
     _parse_args()              — парсинг --patched, --storage, --session
     main()                     — выбор режима


╔══════════════════════════════════════════════════════════════════════════╗
║                         ПРИМЕРЫ ЗАПУСКА                                ║
╚══════════════════════════════════════════════════════════════════════════╝

  # Стандартный режим (JSONL-файлы, без доработок)
  python cli_agent.py

  # С PGSessionManager (автоопределение — PG если есть dsn)
  python cli_agent.py --patched

  # С PGSessionManager принудительно
  python cli_agent.py --patched --storage postgres

  # Принудительно JSONL (даже если есть PostgreSQL dsn)
  python cli_agent.py --patched --storage file

  # Сессии (--session / -s)
  python cli_agent.py --session my-project          # сессия cli:my-project, JSONL
  python cli_agent.py -s my-project                 # краткая форма
  python cli_agent.py -s my-project --patched       # сессия + PGSessionManager
  python cli_agent.py -s my-project -P -S postgres  # сессия + PG принудительно
  python cli_agent.py -s my-project -P -S file      # сессия + JSONL (даже если есть dsn)

  # Разные сессии — разная история диалога
  python cli_agent.py -s work                       # рабочая сессия
  python cli_agent.py -s play                       # личная сессия

  # Продолжить существующую сессию по session_key из БД
  python cli_agent.py -P -s cli:abc123

  # Комбинации
  python cli_agent.py                       # vanilla, сессия cli:direct
  python cli_agent.py -P                    # patched, авто-storage, cli:direct
  python cli_agent.py -P -s dev -S postgres # patched, PG, сессия cli:dev

  # Конфигурация вывода и таймаутов — в скрипте, раздел ПОЛЬЗОВАТЕЛЬСКАЯ КОНФИГУРАЦИЯ:


╔══════════════════════════════════════════════════════════════════════════╗
║                         ПРИМЕРЫ ХУКОВ                                  ║
╚══════════════════════════════════════════════════════════════════════════╝

  Хук — это наследник nanobot.agent.AgentHook. Он получает контекст
  после каждого шага агента (вызов LLM, вызов инструмента и т.д.)

  workspace/hooks/auto_store_hook.py:
    ─────────────────────────────────
    from nanobot.agent import AgentHook, AgentHookContext

    class AutoStoreHook(AgentHook):
        def __init__(self, workspace_dir):
            super().__init__()
            store = SessionFileStore(workspace_dir / "data_store")

        async def after_iteration(self, ctx: AgentHookContext) -> None:
            for i, res in enumerate(ctx.tool_results):
                # сохранить результат в файл если он большой
                ...

  Если конструктор хука принимает workspace_dir — он будет вызван
  с workspace_dir=.... Если нет — хук создастся без аргументов.
"""

import argparse
import asyncio
import importlib
import json
import os
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from loguru import logger

from nanobot.agent import AgentHook
from nanobot.agent.loop import AgentLoop
from nanobot.bus.queue import MessageBus
from nanobot.cli.commands import (
    _flush_pending_tty_input,
    _init_prompt_session,
    _is_exit_command,
    _load_runtime_config,
    _model_display,
    _read_interactive_input_async,
    _restore_terminal,
    _sanitize_surrogates,
    console,
    __logo__,
    __version__,
)
from nanobot.config.loader import get_config_path
from nanobot.config.paths import is_default_workspace
from nanobot.cron.service import CronService
from nanobot.utils.helpers import sync_workspace_templates

from pg_session_manager import PGSessionManager

_CONFIG_PATH: str = str(Path(__file__).parent / "config.json")
_WORKSPACE_DIR: Path = Path(__file__).parent / "workspace"
_HOOKS_DIR: Path = _WORKSPACE_DIR / "hooks"
sys.path.insert(0, str(_HOOKS_DIR))
sys.path.insert(0, str(_WORKSPACE_DIR))


# ══════════════════════════════════════════════════════════════════════════════
# ПОЛЬЗОВАТЕЛЬСКАЯ КОНФИГУРАЦИЯ
# ══════════════════════════════════════════════════════════════════════════════
# Меняйте значения ниже под свои задачи.

from dataclasses import dataclass


@dataclass
class DisplayConfig:
    """Настройки вывода: какие блоки показывать и как."""
    show_reasoning: bool = True
    show_tool_calls: bool = True
    show_tool_results: bool = True
    show_tool_params: bool = True
    show_progress: bool = True
    typewriter_speed: float = 0.01  # секунд на символ; 0 = мгновенно


# Активная конфигурация вывода
DISPLAY: DisplayConfig = DisplayConfig()

# Таймауты (секунды; 0 = без лимита)
LLM_TIMEOUT: float = 300      # LLM call timeout
EXEC_TIMEOUT: int = 60        # Script execution timeout
MAX_ITERATIONS: int = 200     # Max tool call iterations per turn

# Логирование в stderr (подавляем INFO, чтобы не мешали typewriter)
LOG_LEVEL: str = "WARNING"    # "DEBUG" | "INFO" | "WARNING" | "ERROR"


# ══════════════════════════════════════════════════════════════════════════════
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ══════════════════════════════════════════════════════════════════════════════


def _scan_and_register_hooks() -> list:
    """Сканирует workspace/hooks/ и возвращает экземпляры AgentHook-подклассов."""
    global _PARAMS_HOOK
    hooks: list = []
    _PARAMS_HOOK = None
    if not _HOOKS_DIR.is_dir():
        return hooks
    for f in sorted(_HOOKS_DIR.iterdir()):
        if not f.is_file() or not f.name.endswith(".py") or f.name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f.name[:-3])
        except Exception as exc:
            console.print(f"[yellow]⚠[/yellow] {f.name}: {exc}")
            continue
        for attr_name in dir(mod):
            attr = getattr(mod, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, AgentHook)
                and attr is not AgentHook
                and not attr_name.startswith("_")
            ):
                try:
                    hook = attr(workspace_dir=_WORKSPACE_DIR)
                except Exception:
                    try:
                        hook = attr()
                    except Exception as exc:
                        console.print(f"[yellow]⚠[/yellow] {attr_name}: {exc}")
                        continue
                hooks.append(hook)
                console.print(f"[green]✓[/green] {attr_name} loaded")
                # Detect ToolParamsHook for parameter display
                try:
                    from tool_params_hook import ToolParamsHook as _TPH
                    if isinstance(hook, _TPH):
                        _PARAMS_HOOK = hook
                except ImportError:
                    pass
    return hooks


def _migrate_cron_store(config) -> None:
    """Переносит cron-задачи из глобальной ~/.nanobot/cron/ в workspace."""
    from nanobot.config.paths import get_cron_dir

    legacy_path = get_cron_dir() / "jobs.json"
    new_path = config.workspace_path / "cron" / "jobs.json"
    if legacy_path.is_file() and not new_path.exists():
        new_path.parent.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.move(str(legacy_path), str(new_path))


import re as _re

_ANSI_RE = _re.compile(r'(\x1b\[[0-9;]*[a-zA-Z])')
_WRITE_LOCK = asyncio.Lock()
_PARAMS_HOOK: "Any | None" = None  # ToolParamsHook instance, set by _scan_and_register_hooks


async def _typewriter(text: str, style: str, speed: float) -> None:
    """Псевдо-стриминг: посимвольный вывод с раздельной записью ANSI.

    ANSI-последовательности пишутся целиком (чтобы терминал не сбивался),
    видимые символы — по одному с задержкой ``speed``.
    При speed=0 — мгновенный вывод через console.print.

    Весь вывод — под ``_WRITE_LOCK``, чтобы никакое фоновое сообщение
    не вклинилось между символами.
    """
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
    async with _WRITE_LOCK:
        for part in _ANSI_RE.split(buf.getvalue()):
            if not part:
                continue
            if _ANSI_RE.fullmatch(part):
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
    """Выводит блок размышлений целиком."""
    if not text.strip() or not cfg.show_reasoning:
        return
    await _typewriter(text.strip(), "dim italic", cfg.typewriter_speed)


async def _print_tool_events(events: list[dict], cfg: DisplayConfig) -> None:
    """Выводит вызовы инструментов с параметрами (если доступны)."""
    if not cfg.show_tool_calls:
        return

    # Параметры из ToolParamsHook
    param_lookup: dict[str, str] = {}
    if cfg.show_tool_params and _PARAMS_HOOK is not None:
        try:
            raw = _PARAMS_HOOK.drain_calls()
            from tool_params_hook import format_tool_params
            param_lookup = format_tool_params(raw)
        except Exception:
            pass

    for ev in events:
        if not isinstance(ev, dict):
            continue
        name = ev.get("name", "?")
        params_str = param_lookup.get(name, "")

        phase = ev.get("phase", "")
        if phase == "end":
            result = str(ev.get("result", ""))[:120] or "ok"
            if params_str:
                label = f"✓ {name}({params_str}) → {result}"
            elif cfg.show_tool_results:
                label = f"✓ {name} → {result}"
            else:
                label = f"✓ {name}"
            await _typewriter(label, "dim", cfg.typewriter_speed)
        elif phase == "error":
            err = ev.get("error", "failed")
            if params_str:
                label = f"✗ {name}({params_str}): {err}"
            else:
                label = f"✗ {name}: {err}"
            await _typewriter(label, "dim", cfg.typewriter_speed)


# ══════════════════════════════════════════════════════════════════════════════
# ИНТЕРАКТИВНЫЙ ЦИКЛ
# ══════════════════════════════════════════════════════════════════════════════


def _run_interactive_loop(agent, config, *, session: str | None = None,
                          display: DisplayConfig | None = None) -> None:
    """
    Интерактивный цикл: ввод пользователя → агент → вывод ответа.

    Сообщения потребляются из outbound-очереди:
      1. Размышления буферизируются до _reasoning_end, затем _typewriter
      2. Вызовы инструментов буферизируются и выводятся пачками (_typewriter)
      3. Прогресс-бары показываются по мере поступления
      4. Финальный ответ — _typewriter (или _print_agent_response если speed=0)
    """
    from nanobot.bus.events import InboundMessage

    # Подавляем INFO-логи в stderr (мешают typewriter-выводу)
    try:
        logger.remove()
        logger.add(sys.stderr, level=LOG_LEVEL)
    except Exception:
        pass

    cfg = display or DisplayConfig()
    bus = agent.bus
    _init_prompt_session()
    _model, _preset_tag = _model_display(config)
    console.print(
        f"Interactive mode [bold blue]({_model})[/bold blue]{_preset_tag} "
        f"\u2014 type [bold]exit[/bold] or [bold]Ctrl+C[/bold] to quit\n"
    )

    chat_id = session or "direct"
    cli_channel, cli_chat_id = "cli", chat_id

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

        async def consume_outbound() -> tuple[str, dict]:
            """
            Читает outbound-сообщения до получения финального ответа.

            Выводится только:
              • reasoning — сразу по мере поступления
              • стриминг-чанки — сразу по мере поступления
              • финальный ответ — целиком (если не был отстримлен)
            """
            full_response = ""
            response_meta: dict = {}

            while True:
                try:
                    msg = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                except asyncio.CancelledError:
                    break

                meta = msg.metadata or {}

                if meta.get("_reasoning_delta"):
                    if msg.content and cfg.show_reasoning:
                        await _typewriter(msg.content, "dim italic", cfg.typewriter_speed)
                    continue

                if meta.get("_reasoning_end"):
                    continue

                if meta.get("_stream_delta"):
                    if msg.content:
                        async with _WRITE_LOCK:
                            sys.stdout.write(msg.content)
                            sys.stdout.flush()
                        full_response += msg.content
                    continue

                if meta.get("_stream_end"):
                    async with _WRITE_LOCK:
                        sys.stdout.write("\n")
                        sys.stdout.flush()
                    continue

                # Всё остальное (tool_events, progress, control) — тихо пропускаем
                if not msg.content or meta.get("_progress") or meta.get("_turn_end") or meta.get("_tool_events") or meta.get("_tool_hint"):
                    continue

                # Финальный ответ
                full_response = msg.content
                response_meta = meta
                break

            return full_response, response_meta

        # Основной цикл ввода-вывода
        try:
            while True:
                try:
                    _flush_pending_tty_input()

                    user_input = _sanitize_surrogates(await _read_interactive_input_async())
                    command = user_input.strip()
                    if not command:
                        continue
                    if _is_exit_command(command):
                        _restore_terminal()
                        console.print("\nGoodbye!")
                        break

                    await bus.publish_inbound(InboundMessage(
                        channel=cli_channel,
                        sender_id="user",
                        chat_id=cli_chat_id,
                        content=user_input,
                    ))

                    content, meta = await consume_outbound()

                    if content and not meta.get("_stream_delta"):
                        await _typewriter(content, "", cfg.typewriter_speed)

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
            await agent.close_mcp()
            flushed = agent.sessions.flush_all()
            if flushed:
                logger.info("Flushed {} session(s) to disk", flushed)

    asyncio.run(run_interactive())


# ══════════════════════════════════════════════════════════════════════════════
# РЕЖИМЫ ЗАПУСКА
# ══════════════════════════════════════════════════════════════════════════════


def _apply_timeouts(config) -> None:
    """Применяет таймауты из констант в config и переменные окружения."""
    if LLM_TIMEOUT >= 0:
        os.environ["NANOBOT_LLM_TIMEOUT_S"] = str(LLM_TIMEOUT)
    if EXEC_TIMEOUT >= 0:
        try:
            config.tools.exec.timeout = EXEC_TIMEOUT
        except Exception:
            pass
    if MAX_ITERATIONS > 0:
        try:
            config.agents.defaults.max_tool_iterations = MAX_ITERATIONS
        except Exception:
            pass


def _run_vanilla_agent(args: argparse.Namespace) -> None:
    """Стандартный CLI-агент (как `nanobot agent`). Без доработок."""
    config = _load_runtime_config(config=_CONFIG_PATH, workspace=str(_WORKSPACE_DIR))
    _apply_timeouts(config)
    sync_workspace_templates(config.workspace_path)
    console.print(f"{__logo__} Starting nanobot CLI agent v{__version__}...")

    bus = MessageBus()
    if is_default_workspace(config.workspace_path):
        _migrate_cron_store(config)

    cron_store_path = config.workspace_path / "cron" / "jobs.json"
    cron = CronService(cron_store_path)

    agent = AgentLoop.from_config(config, bus, cron_service=cron)
    _run_interactive_loop(agent, config, session=args.session, display=DISPLAY)


def _run_patched_agent(args: argparse.Namespace) -> None:
    """CLI-агент с PGSessionManager и кастомными инструментами."""
    config = _load_runtime_config(config=_CONFIG_PATH, workspace=str(_WORKSPACE_DIR))
    _apply_timeouts(config)
    sync_workspace_templates(config.workspace_path)
    console.print(f"{__logo__} Starting nanobot CLI agent v{__version__}...")

    bus = MessageBus()
    if is_default_workspace(config.workspace_path):
        _migrate_cron_store(config)

    cron_store_path = config.workspace_path / "cron" / "jobs.json"
    cron = CronService(cron_store_path)

    # Выбор хранилища сессий
    pg_cfg = getattr(config.channels, "postgres", {})
    channel_dsn = pg_cfg.get("dsn", "") if isinstance(pg_cfg, dict) else getattr(pg_cfg, "dsn", "")
    try:
        _sm_raw = json.loads(get_config_path().read_text(encoding="utf-8"))
        sm_cfg = _sm_raw.get("session_manager", {}) or {}
    except Exception:
        sm_cfg = {}
    if isinstance(sm_cfg, dict):
        sm_dsn = sm_cfg.get("dsn") or channel_dsn
        sm_schema = sm_cfg.get("schema", "public")
        sm_messages_table = sm_cfg.get("messages_table", "session_messages")
        sm_meta_table = sm_cfg.get("meta_table", "session_meta")
        sm_min_conn = sm_cfg.get("min_conn", 1)
        sm_max_conn = sm_cfg.get("max_conn", 4)
        sm_pool_timeout = sm_cfg.get("pool_timeout", 5.0)
    else:
        sm_dsn = channel_dsn
        sm_schema = "public"
        sm_messages_table = "session_messages"
        sm_meta_table = "session_meta"
        sm_min_conn = 1
        sm_max_conn = 4
        sm_pool_timeout = 5.0
    dsn = sm_dsn

    use_postgres = args.storage == "postgres" or (args.storage == "auto" and bool(dsn))
    if use_postgres:
        if not dsn:
            console.print("[red]✗[/red] --storage=postgres but no PostgreSQL DSN in config")
            sys.exit(1)
        session_manager = PGSessionManager(
            workspace=config.workspace_path,
            dsn=dsn,
            schema=sm_schema,
            messages_table=sm_messages_table,
            meta_table=sm_meta_table,
            min_conn=sm_min_conn,
            max_conn=sm_max_conn,
            pool_timeout=sm_pool_timeout,
        )
        session_manager.ensure_tables()
        console.print("[green]✓[/green] PGSessionManager: sessions stored in PostgreSQL")
    else:
        session_manager = None
        if dsn:
            console.print("[dim]PostgreSQL DSN available but --storage=file; using JSONL files[/dim]")

    hooks = _scan_and_register_hooks()
    agent = AgentLoop.from_config(
        config, bus,
        cron_service=cron,
        session_manager=session_manager,
        hooks=hooks,
    )
    _run_interactive_loop(agent, config, session=args.session, display=DISPLAY)


# ══════════════════════════════════════════════════════════════════════════════
# ТОЧКА ВХОДА
# ══════════════════════════════════════════════════════════════════════════════


def _parse_args() -> argparse.Namespace:
    """Парсит аргументы командной строки."""
    parser = argparse.ArgumentParser(
        description="nanobot CLI agent",
    )
    parser.add_argument(
        "--patched", "-P",
        action="store_true",
        default=False,
        help="Enable local patches: PGSessionManager, workspace hooks",
    )
    parser.add_argument(
        "--storage", "-S",
        type=str,
        default="auto",
        choices=("auto", "file", "postgres"),
        help="Session storage (only with --patched, default: auto)",
    )
    parser.add_argument(
        "--session", "-s",
        type=str,
        default=None,
        help="Session key (default: cli:direct). Resume or start a named session.",
    )
    return parser.parse_args()


def main():
    args = _parse_args()
    if args.patched:
        _run_patched_agent(args)
    else:
        _run_vanilla_agent(args)


if __name__ == "__main__":
    main()
