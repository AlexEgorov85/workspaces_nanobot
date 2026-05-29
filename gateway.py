"""
Шлюз (gateway) для nanobot с поддержкой PostgresChannel.

══════════════════════════════════════════════════════════════════════════════
НАЗНАЧЕНИЕ
══════════════════════════════════════════════════════════════════════════════

Скрипт запускает nanobot в режиме gateway — долгоживущего сервера, который:
  • Одновременно обслуживает несколько каналов связи (Telegram, WebSocket,
    Discord, PostgresChannel и др.)
  • Принимает входящие сообщения от пользователей через любой канал,
    обрабатывает их через LLM-агента и отправляет ответ обратно
  • При необходимости использует инструменты (tools) из workspace/tools/

══════════════════════════════════════════════════════════════════════════════
ПАРАМЕТРЫ ЗАПУСКА
══════════════════════════════════════════════════════════════════════════════

  -P, --patched        Включить локальные доработки (см. ниже).
  -C, --channels       Список каналов через запятую (только с --patched).
                       Пример: --channels websocket,telegram
  -S, --storage        Хранилище сессий: auto | file | postgres
                       (только с --patched, по умолчанию auto)

══════════════════════════════════════════════════════════════════════════════
РЕЖИМЫ ЗАПУСКА
══════════════════════════════════════════════════════════════════════════════

1. Без --patched (режим по умолчанию)
   ─────────────────────────────────
   Используется стандартный gateway из библиотеки nanobot (функция
   _run_gateway из nanobot.cli.commands). Никаких локальных доработок
   не применяется. Каналы и сессии управляются стандартными средствами.

2. С --patched (режим с доработками)
   ─────────────────────────────────
   Включаются все локальные расширения:
      • PostgresChannel — канал на основе PostgreSQL LISTEN/NOTIFY
      • PGSessionManager — хранение истории сессий в PostgreSQL
      • AutoStoreHook — автоматическое сохранение контекста
      • Кастомная WebUI-сборка из директории webui-dist/

══════════════════════════════════════════════════════════════════════════════
ПРИМЕРЫ ЗАПУСКА
══════════════════════════════════════════════════════════════════════════════

  # Стандартный запуск (как библиотека nanobot)
  python gateway.py

  # Все локальные доработки + автоопределение хранилища
  python gateway.py --patched

  # Только Telegram и WebSocket, история в PostgreSQL
  python gateway.py --patched --channels websocket,telegram --storage postgres

  # Все каналы из конфига, история принудительно в JSONL
  python gateway.py --patched --storage file

  # Только PostgresChannel
  python gateway.py --patched --channels postgres
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
import traceback
from pathlib import Path

# ── Подключаем директорию скрипта к sys.path, чтобы импортировать ───────────
#    локальные модули: postgres_channel, pg_session_manager, auto_store_hook.
sys.path.insert(0, str(Path(__file__).parent))

from loguru import logger
from postgres_channel import PostgresChannel

from nanobot.agent.loop import AgentLoop
from nanobot.bus.queue import MessageBus
from nanobot.channels.manager import ChannelManager, _default_webui_dist
from nanobot.cli.commands import _load_runtime_config, console, __logo__, __version__
from pg_session_manager import PGSessionManager
from nanobot.utils.helpers import sync_workspace_templates

# ── Пути к workspace и hooks для AutoStoreHook ──────────────────────────────
_WORKSPACE_DIR = Path(__file__).parent / "workspace"
_HOOKS_DIR = _WORKSPACE_DIR / "hooks"
sys.path.insert(0, str(_HOOKS_DIR))
sys.path.insert(0, str(_WORKSPACE_DIR))


def _patch_webui_dist(channels: ChannelManager) -> None:
    """
    Заменяет стандартную WebUI-сборку на кастомную из webui-dist/.

    Если кастомная директория не существует — копирует туда оригинальную
    сборку из библиотеки, чтобы пользователь мог её модифицировать.
    """
    ws = channels.channels.get("websocket")
    if ws is None or not hasattr(ws, "_static_dist_path"):
        return  # Нет WebSocket-канала или он не поддерживает статику
    orig = _default_webui_dist()
    if orig is None:
        return  # Нет встроенной WebUI-сборки
    custom = Path(__file__).parent / "webui-dist"
    if not custom.is_dir():
        import shutil
        shutil.copytree(str(orig), str(custom))
        console.print(f"[green]✓[/green] Created custom webui dist at {custom}")
    ws._static_dist_path = custom.resolve()


def _resolve_transcription_key(config):
    """
    Возвращает API-ключ для сервиса транскрипции.

    Выбирает провайдера (openai или groq) в зависимости от настройки
    transcription_provider в конфиге.
    """
    provider = config.channels.transcription_provider
    try:
        if provider == "openai":
            return config.providers.openai.api_key
        return config.providers.groq.api_key
    except AttributeError:
        return ""


def _resolve_transcription_base(config):
    """
    Возвращает базовый URL для API транскрипции.

    Некоторые провайдеры используют кастомные эндпоинты.
    """
    provider = config.channels.transcription_provider
    try:
        if provider == "openai":
            return config.providers.openai.api_base or ""
        return config.providers.groq.api_base or ""
    except AttributeError:
        return ""


# ══════════════════════════════════════════════════════════════════════════════
# РЕЖИМ "VANILLA" — СТАНДАРТНЫЙ ЗАПУСК БЕЗ ДОРАБОТОК
# ══════════════════════════════════════════════════════════════════════════════
#
# В этом режиме вызывается нативная функция _run_gateway() из библиотеки
# nanobot. Никаких локальных патчей не применяется — только то, что
# предоставляет стандартная библиотека.
# Используется, когда скрипт запущен без флага --patched.


def _run_vanilla_gateway() -> None:
    """Запускает стандартный gateway из библиотеки nanobot (без локальных доработок)."""
    from nanobot.cli.commands import _run_gateway

    # Загружаем конфигурацию и делегируем запуск библиотечной функции
    config = _load_runtime_config()
    _run_gateway(config)


# ══════════════════════════════════════════════════════════════════════════════
# РЕЖИМ "PATCHED" — ЗАПУСК С ЛОКАЛЬНЫМИ ДОРАБОТКАМИ
# ══════════════════════════════════════════════════════════════════════════════
#
# В этом режиме скрипт самостоятельно конфигурирует и запускает gateway,
# добавляя все локальные расширения: PostgresChannel, PGSessionManager,
# AutoStoreHook, кастомная WebUI-сборка.


def _run_patched_gateway(args: argparse.Namespace) -> None:
    """
    Запускает gateway со всеми локальными доработками.

    Параметры:
        args: распарсенные аргументы командной строки (channels, storage)
    """
    # ── 1. Загрузка конфигурации и подготовка workspace ──────────────────────
    config = _load_runtime_config()

    # Синхронизируем шаблоны workspace (создаём недостающие директории)
    sync_workspace_templates(config.workspace_path)
    console.print(f"{__logo__} Starting nanobot gateway v{__version__}...")

    # ── 2. Создание шины сообщений и SessionManager ──────────────────────────
    #    MessageBus — центральная шина, через которую проходят все сообщения.
    #    SessionManager — хранилище истории сессий (файлы или PostgreSQL).
    bus = MessageBus()

    # Достаём DSN для PostgreSQL из конфига (секция channels.postgres)
    pg_cfg = getattr(config.channels, "postgres", {})
    dsn = pg_cfg.get("dsn", "") if isinstance(pg_cfg, dict) else getattr(pg_cfg, "dsn", "")

    # Логика выбора хранилища:
    #   postgres → принудительно PG (ошибка если нет DSN)
    #   auto     → PG если есть DSN, иначе JSONL
    #   file     → принудительно JSONL
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
        from nanobot.session.manager import SessionManager
        session_manager = SessionManager(config.workspace_path)
        if dsn:
            console.print("[dim]PostgreSQL DSN available but --storage=file; using JSONL files[/dim]")
        else:
            console.print("[yellow]⚠[/yellow] No PostgreSQL DSN — using JSONL files")

    # ── 3. Загрузка AutoStoreHook ─────────────────────────────────────────────
    #    Хук автоматически сохраняет контекст агента между вызовами.
    hooks = []
    try:
        from auto_store_hook import AutoStoreHook, set_session_key
        hooks.append(AutoStoreHook(workspace_dir=_WORKSPACE_DIR))
        console.print("[green]✓[/green] AutoStoreHook loaded")
    except Exception as exc:
        console.print(f"[yellow]⚠[/yellow] AutoStoreHook: {exc}")

    # ── 4. Создание AgentLoop ─────────────────────────────────────────────────
    #    AgentLoop — главный цикл агента: получает сообщения, отправляет их
    #    в LLM, обрабатывает вызовы инструментов, формирует ответ.
    agent = AgentLoop.from_config(
        config, bus,
        session_manager=session_manager,
        hooks=hooks,
    )

    # ── 5. Проброс session_key в AutoStoreHook ────────────────────────────────
    #    Перехватываем _run_agent_loop, чтобы передавать session_key в глобальную
    #    переменную set_session_key для AutoStoreHook.
    _original_run_loop = agent._run_agent_loop

    async def _run_agent_loop_with_session_key(*args, **kwargs):
        set_session_key(kwargs.get("session_key"))
        return await _original_run_loop(*args, **kwargs)

    agent._run_agent_loop = _run_agent_loop_with_session_key

    # ── 6. Инициализация ChannelManager ───────────────────────────────────────
    #    ChannelManager обнаруживает и запускает каналы, перечисленные в конфиге
    #    (telegram, websocket, discord и т.д.) через секцию channels.
    channels = ChannelManager(config, bus, session_manager=session_manager)

    # ── 7. Добавление PostgresChannel (канал поверх PostgreSQL) ───────────────
    #    Позволяет отправлять и получать сообщения через БД.
    if pg_cfg.get("enabled", False):
        pg_channel = PostgresChannel(pg_cfg, bus)

        # Копируем настройки транскрипции и отображения из конфига
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

    # ── 8. Фильтрация каналов (если указан --channels) ────────────────────────
    #    Оставляем только те каналы, которые перечислены в аргументе.
    if args.channels:
        allowed = {name.strip() for name in args.channels.split(",")}
        for name in list(channels.channels):
            if name not in allowed:
                del channels.channels[name]
        if not channels.channels:
            console.print("[yellow]⚠[/yellow] No matching channels in --channels list")

    # ── 9. Подмена WebUI-сборки на кастомную ─────────────────────────────────
    _patch_webui_dist(channels)

    # Выводим список активных каналов
    console.print(f"[green]✓[/green] Channels enabled: {', '.join(channels.enabled_channels)}")

    # ── 10. Главный цикл запуска ──────────────────────────────────────────────
    #    Запускаем агента и все каналы, обрабатываем остановку.
    async def run():
        # Запускаем каналы в фоновой задаче
        channels_task = asyncio.create_task(channels.start_all())

        try:
            # Запускаем основной цикл агента
            await agent.run()
        except KeyboardInterrupt:
            console.print("\nShutting down...")
        except Exception:
            console.print("\n[red]Gateway crashed[/red]")
            console.print(traceback.format_exc())
        finally:
            # Останавливаем каналы
            channels_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await channels_task
            # Закрываем MCP-соединения
            await agent.close_mcp()
            agent.stop()
            await channels.stop_all()
            # Сбрасываем все кэшированные сессии на диск с fsync
            flushed = agent.sessions.flush_all()
            if flushed:
                logger.info("Flushed {} session(s) to disk", flushed)

    asyncio.run(run())


# ══════════════════════════════════════════════════════════════════════════════
# ТОЧКА ВХОДА
# ══════════════════════════════════════════════════════════════════════════════
#
# Парсинг аргументов и выбор режима запуска (vanilla / patched).


def _parse_args() -> argparse.Namespace:
    """Парсит аргументы командной строки."""
    parser = argparse.ArgumentParser(
        description="nanobot gateway — upstream by default, --patched for local extras",
    )
    parser.add_argument(
        "--patched", "-P",
        action="store_true",
        default=False,
        help="Enable local patches: PostgresChannel, AutoStoreHook, WebUI, etc.",
    )
    parser.add_argument(
        "--channels", "-C",
        type=str,
        default=None,
        help="Comma-separated channel names to start (e.g. websocket,telegram). "
             "(only with --patched)",
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

    Без --patched → запуск стандартного библиотечного gateway.
    С --patched  → запуск с локальными доработками.
    """
    args = _parse_args()
    if args.patched:
        _run_patched_gateway(args)
    else:
        _run_vanilla_gateway()


if __name__ == "__main__":
    main()
