"""Инструменты навыка ``follow_up`` (D5) для агента.

Сервер Follow Up работает отдельным процессом: у него своя SQLite с
единственным писателем и фоновые потоки синхронизации корпуса, и падение
навыка не должно ронять gateway. Этот модуль — тонкий клиент к нему:

* при старте gateway (``gateway.py``) запускает
  ``workspace/skills/follow_up/scripts/follow_up_mcp`` тем же Python, что и
  сам gateway, и держит с ним MCP-сессию по stdio; в CLI и тестах сервер
  поднимается по первому вызову;
* регистрирует семь инструментов ``mcp_follow_up_<имя>`` — по одному на
  инструмент сервера, с его описанием и схемой из ``tools.json`` навыка;
* вызов инструмента агента передаёт аргументы серверу и возвращает его
  ответ текстом. Ключ беседы и отправителя берёт из контекста запроса
  nanobot: модель свой ключ сессии не видит, а без него память диалога
  у всех аудиторов была бы общей.

Почему не ``tools.mcpServers`` в ``config.json``: в ``nanobot-ai`` 0.3.5
серверы оттуда поднимает ``MCPProvider`` собственных CLI фреймворка, а
gateway проекта собирает ``AgentLoop`` сам и MCP не подключает.

Модуль импортирует только стандартную библиотеку, ``nanobot``, ``mcp``,
``anyio`` и ``loguru`` (зависимости ``nanobot-ai``); код сервера в процесс
gateway не попадает. Журнал — через ``loguru``, как у загрузчика проекта:
стандартный ``logging`` gateway никуда не выводит.
Настройка — ``tools.follow_up`` в ``config.json``: ``enable`` (по умолчанию
``true``) и ``timeout_sec`` (по умолчанию 240).
"""
from __future__ import annotations

import asyncio
import atexit
import builtins
import json
import os
import sys
import threading
import time
from abc import abstractmethod
from concurrent.futures import Future
from pathlib import Path
from typing import Any, ClassVar

import anyio
from loguru import logger
from nanobot.agent.tools.base import Tool, ToolResult

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "workspace" / "skills" / "follow_up"
LAUNCHER = SKILL_DIR / "scripts" / "follow_up_mcp"
TOOLS_FILE = SKILL_DIR / "tools.json"
SERVER_LOG = SKILL_DIR / "logs" / "server.log"

TOOL_PREFIX = "mcp_follow_up_"
DEFAULT_TIMEOUT_SEC = 240.0       # ask укладывается в дедлайн хода сервера, 150 с
START_TIMEOUT_SEC = 180.0         # подъём: индексы, модели, база
LOG_ROTATE_BYTES = 2_000_000      # server.log дописывается; больше — в server.log.1

# Запрос не ушёл в мёртвую сессию — повторить на свежем сервере безопасно.
_NOT_DELIVERED = (anyio.ClosedResourceError, anyio.BrokenResourceError)


class _Generation:
    """Один запуск сервера: процесс, сессия и вызовы, которые идут через неё.

    Всё, что гасит сервер, гасит своё поколение, а не «текущее»: иначе
    поздняя очистка после одной смерти убила бы уже поднятую замену.
    """

    def __init__(self) -> None:
        self.session: Any = None
        self.ready = asyncio.Event()
        self.stop = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.pending: set[asyncio.Task] = set()
        self.dead = False
        self.error = ""
        self.log_offset = 0


class _Bridge:
    """Сервер и MCP-сессия с ним — в своём потоке со своим циклом.

    Свой поток, а не цикл gateway: контексты ``stdio_client`` и
    ``ClientSession`` должны открываться и закрываться в одной задаче, а
    сессия — жить дольше любого отдельного хода агента и не зависеть от
    того, сколько циклов событий у gateway.
    """

    def __init__(self, command: str, args: list[str], cwd: Path,
                 log_path: Path | None = None) -> None:
        self.command, self.args, self.cwd, self.log_path = command, args, cwd, log_path
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._gen: _Generation | None = None
        self._warmed = False

    # ── поток и цикл ────────────────────────────────────────────────

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None or not self._thread or not self._thread.is_alive():
                loop = asyncio.new_event_loop()
                thread = threading.Thread(target=loop.run_forever,
                                          name="follow_up-mcp", daemon=True)
                thread.start()
                self._loop, self._thread = loop, thread
            return self._loop

    def submit(self, coro) -> Future:
        return asyncio.run_coroutine_threadsafe(coro, self._ensure_loop())

    # ── журнал сервера ──────────────────────────────────────────────

    def _open_log(self, gen: _Generation):
        """stderr сервера — в файл навыка; не открылся — в stderr gateway."""
        if self.log_path is None:
            return sys.stderr
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            if self.log_path.exists() and self.log_path.stat().st_size > LOG_ROTATE_BYTES:
                self.log_path.replace(self.log_path.with_name(self.log_path.name + ".1"))
            f = open(self.log_path, "a", encoding="utf-8")   # noqa: SIM115
            f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} старт сервера навыка "
                    f"(gateway pid {os.getpid()}) ===\n")
            f.flush()
            gen.log_offset = f.tell()
            return f
        except OSError as e:
            logger.warning("follow_up: журнал сервера {} не открылся ({}) — пишу в stderr",
                           self.log_path, e)
            return sys.stderr

    def _log_tail(self, gen: _Generation, lines: int = 3) -> str:
        """Последние строки журнала этого запуска — почему он не поднялся."""
        if self.log_path is None:
            return ""
        try:
            with open(self.log_path, encoding="utf-8", errors="replace") as f:
                f.seek(gen.log_offset)
                tail = [ln.strip() for ln in f.read().splitlines() if ln.strip()]
        except OSError:
            return ""
        return " | ".join(tail[-lines:])[-400:]

    # ── поколение сервера (внутри потока моста) ─────────────────────

    async def _run(self, gen: _Generation) -> None:
        errlog = sys.stderr
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client

            errlog = self._open_log(gen)
            params = StdioServerParameters(command=self.command, args=self.args,
                                           env=dict(os.environ), cwd=str(self.cwd))
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    gen.session = session
                    gen.ready.set()
                    await gen.stop.wait()
        except BaseException as e:                          # noqa: BLE001
            gen.error = _describe(e)
            if not isinstance(e, Exception):
                raise
        finally:
            gen.session = None
            gen.dead = True
            gen.ready.set()                   # разбудить ждущих: сессии нет
            # Вызовы на этом поколении ответа уже не получат: ClientSession
            # при закрытии отменяет свой приёмный цикл раньше, чем успевает
            # вернуть им «соединение закрыто», — без отмены они ждали бы
            # свой полный таймаут.
            me = asyncio.current_task()
            for t in list(gen.pending):
                if t is not me:
                    t.cancel()
            if errlog is not sys.stderr:
                errlog.close()

    async def _retire(self, gen: _Generation) -> None:
        """Погасить именно это поколение. Повторно — ничего не делает."""
        if gen.dead and (gen.task is None or gen.task.done()):
            return
        gen.dead = True
        gen.pending.discard(asyncio.current_task())
        gen.stop.set()
        task = gen.task
        if task is not None and not task.done():
            if not gen.ready.is_set():
                task.cancel()                 # висит на подъёме — ждать нечего
            try:
                await asyncio.wait_for(asyncio.shield(task), 10)
            except BaseException:                           # noqa: BLE001
                pass

    async def _current(self) -> _Generation:
        """Живое поколение: текущее или новое. Не поднялось — исключение с причиной."""
        gen = self._gen
        if gen is None or gen.dead or (gen.task is not None and gen.task.done()):
            gen = _Generation()
            self._gen = gen
            gen.task = asyncio.get_running_loop().create_task(self._run(gen))
        if not gen.ready.is_set():
            try:
                await asyncio.wait_for(gen.ready.wait(), START_TIMEOUT_SEC)
            except TimeoutError:
                await self._retire(gen)
                tail = self._log_tail(gen)
                raise RuntimeError(f"сервер не поднялся за {int(START_TIMEOUT_SEC)} с"
                                   + (f"; журнал сервера: {tail}" if tail else "")) from None
        if gen.session is None:
            tail = self._log_tail(gen)
            reason = gen.error or "сервер завершился при старте"
            raise RuntimeError(reason + (f"; журнал сервера: {tail}" if tail else ""))
        return gen

    async def _call(self, name: str, arguments: dict[str, Any]) -> tuple[bool, str]:
        me = asyncio.current_task()
        for attempt in (1, 2):
            gen = await self._current()
            gen.pending.add(me)
            try:
                result = await gen.session.call_tool(name, arguments)
            except _NOT_DELIVERED:
                # Сервер умер, пока ждал: запрос никуда не ушёл. Один повтор
                # на свежем сервере — иначе первый вопрос после тихой смерти
                # всегда кончался бы отказом.
                await self._retire(gen)
                if attempt == 1:
                    continue
                raise ConnectionError("сервер навыка недоступен") from None
            except asyncio.CancelledError:
                if gen.dead:
                    raise ConnectionError("сервер навыка упал во время вызова") from None
                raise
            except Exception:
                # Ошибка по делу и умерший процесс выглядят одинаково; пинг
                # различает. Гасим только своё поколение.
                if not await self._alive(gen):
                    await self._retire(gen)
                raise
            finally:
                gen.pending.discard(me)
            text = "\n".join(getattr(c, "text", "") for c in (result.content or [])
                             if getattr(c, "type", "") == "text")
            return bool(getattr(result, "isError", False)), text
        raise ConnectionError("сервер навыка недоступен")   # недостижимо

    @staticmethod
    async def _alive(gen: _Generation) -> bool:
        session = gen.session
        if session is None or gen.dead:
            return False
        try:
            await asyncio.wait_for(session.send_ping(), 5)
            return True
        except BaseException:                               # noqa: BLE001
            return False

    async def _close(self) -> None:
        if self._gen is not None:
            await self._retire(self._gen)

    # ── снаружи ─────────────────────────────────────────────────────

    def warm_up(self) -> None:
        """Поднять сервер заранее, не дожидаясь первого вопроса. Один раз.

        Без этого фоновая синхронизация корпуса стартовала бы только после
        первого вызова, а первый вопрос ждал бы подъёма сервера.
        """
        with self._lock:
            if self._warmed:
                return
            self._warmed = True
        fut = self.submit(self._current())
        fut.add_done_callback(_log_warm_up)

    async def call(self, name: str, arguments: dict[str, Any],
                   timeout: float) -> tuple[bool, str]:
        fut = self.submit(self._call(name, arguments))
        try:
            return await asyncio.wait_for(asyncio.wrap_future(fut), timeout)
        except TimeoutError:
            fut.cancel()
            raise

    def close(self) -> None:
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        try:
            asyncio.run_coroutine_threadsafe(self._close(), loop).result(15)
        except Exception:                                   # noqa: BLE001
            pass
        loop.call_soon_threadsafe(loop.stop)


_GROUP = getattr(builtins, "BaseExceptionGroup", ())


def _describe(e: BaseException) -> str:
    while _GROUP and isinstance(e, _GROUP) and e.exceptions:
        e = e.exceptions[0]
    return f"{type(e).__name__}: {str(e).strip()[:300]}" if str(e).strip() else type(e).__name__


def _log_warm_up(fut: Future) -> None:
    try:
        fut.result()
        logger.info("follow_up: сервер навыка поднят")
    except Exception as e:                                  # noqa: BLE001
        logger.warning("follow_up: сервер навыка не поднялся ({}); подробности — "
                       "{} и `follow_up_mcp --check`", _describe(e), SERVER_LOG)


_BRIDGE: _Bridge | None = None
_BRIDGE_LOCK = threading.Lock()


def should_warm_up(argv: list[str] | None = None, modules: Any = None) -> bool:
    """Поднимать сервер при регистрации — только в долгоживущем gateway.

    Инструменты регистрирует и CLI, и тесты проекта (`ApplicationContext`),
    и `gateway.py --smoke`: там сервер навыка поднимется по первому вызову
    или не нужен вовсе. На машине с корпусом лишний экземпляр стал бы
    владельцем базы и начал бы синхронизацию. `FOLLOW_UP_WARM_UP=1`/`0`
    решает явно.
    """
    argv = sys.argv if argv is None else argv
    modules = sys.modules if modules is None else modules
    flag = os.environ.get("FOLLOW_UP_WARM_UP", "").strip().lower()
    if flag in ("1", "true", "yes"):
        return True
    if flag in ("0", "false", "no") or "pytest" in modules or "--smoke" in argv:
        return False
    return Path(argv[0] if argv else "").name == "gateway.py"


def bridge() -> _Bridge:
    global _BRIDGE
    with _BRIDGE_LOCK:
        if _BRIDGE is None:
            _BRIDGE = _Bridge(sys.executable, [str(LAUNCHER)], REPO_ROOT, SERVER_LOG)
            atexit.register(_BRIDGE.close)
        return _BRIDGE


def _request_identity() -> tuple[str | None, str | None]:
    """Ключ беседы и отправитель текущего хода — из контекста запроса nanobot."""
    try:
        from nanobot.agent.tools.context import current_request_context
        ctx = current_request_context()
    except Exception:                                       # noqa: BLE001
        return None, None
    if ctx is None:
        return None, None
    key = getattr(ctx, "session_key", None)
    sender = getattr(ctx, "sender_id", None)
    return (key if isinstance(key, str) and key else None,
            sender if isinstance(sender, str) and sender else None)


# ──────────────────────────────────────────────────────────────────
# Инструменты агента
# ──────────────────────────────────────────────────────────────────

def _load_specs() -> list[dict[str, Any]]:
    try:
        specs = json.loads(TOOLS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("follow_up: {} не прочитан ({}) — инструментов нет", TOOLS_FILE, e)
        return []
    return [s for s in specs if isinstance(s, dict) and s.get("name")]


class FollowUpTool(Tool):
    """Общая часть семи инструментов: вызов одноимённого инструмента сервера.

    Абстрактный — загрузчик проекта регистрирует только конкретные
    подклассы, по одному на инструмент сервера (см. ``_make_tools``).
    """

    config_key: ClassVar[str] = "follow_up"
    _plugin_discoverable: ClassVar[bool] = False
    _server_tool: ClassVar[str] = ""
    _description: ClassVar[str] = ""
    _schema: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}

    def __init__(self, *, timeout_sec: float = DEFAULT_TIMEOUT_SEC) -> None:
        self.timeout_sec = timeout_sec

    @abstractmethod
    def _concrete(self) -> None:
        """Есть только у подклассов из ``_make_tools``."""

    @classmethod
    def _section(cls, ctx: Any) -> dict[str, Any]:
        settings = getattr(ctx, "_settings_ref", None)
        section = getattr(getattr(settings, "tools", None), "follow_up", None)
        if section is None:
            return {}
        if isinstance(section, dict):
            return dict(section)
        return {k: getattr(section, k) for k in ("enable", "timeout_sec")
                if hasattr(section, k)}

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        if not LAUNCHER.is_file() or not cls._server_tool:
            return False
        return bool(cls._section(ctx).get("enable", True))

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        try:
            timeout = float(cls._section(ctx).get("timeout_sec", DEFAULT_TIMEOUT_SEC))
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT_SEC
        if should_warm_up():
            bridge().warm_up()                # один раз на процесс — внутри моста
        return cls(timeout_sec=timeout)

    @property
    def name(self) -> str:
        return TOOL_PREFIX + self._server_tool

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._schema

    def _arguments(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        props = self._schema.get("properties") or {}
        args = {k: v for k, v in kwargs.items() if k in props}
        key, sender = _request_identity()
        # Ключ беседы — из хода, а не от модели: модель свой ключ не видит, а
        # придуманный ею («<КМ>-<дата>») совпал бы у двух аудиторов.
        if key and "session_key" in props:
            args["session_key"] = key
        if sender and "user_id" in props:
            args["user_id"] = sender
        return args

    async def execute(self, **kwargs: Any) -> Any:
        args = self._arguments(kwargs)
        try:
            is_error, text = await bridge().call(self._server_tool, args, self.timeout_sec)
        except TimeoutError:
            return ToolResult.error(
                f"Follow Up не ответил за {int(self.timeout_sec)} с. Карточку "
                f"исполнения собирать через card_start/card_status.")
        except Exception as e:                              # noqa: BLE001
            logger.exception("follow_up: вызов {} не прошёл", self._server_tool)
            return ToolResult.error(
                f"Follow Up сейчас недоступен ({_describe(e)}). Не отвечай по актам "
                f"«из головы». Журнал сервера — workspace/skills/follow_up/logs/"
                f"server.log, проверка — `python workspace/skills/follow_up/"
                f"scripts/follow_up_mcp --check`.")
        return ToolResult.error(text) if is_error else text


def _make_tools() -> dict[str, type]:
    made: dict[str, type] = {}
    for spec in _load_specs():
        server_name = str(spec["name"])
        cls_name = "FollowUp" + "".join(p.capitalize() for p in server_name.split("_")) + "Tool"
        # Через метакласс базового (ABCMeta): он пересчитывает абстрактные
        # методы, и подкласс с _concrete становится регистрируемым.
        made[cls_name] = type(FollowUpTool)(cls_name, (FollowUpTool,), {
            "__doc__": f"Инструмент сервера Follow Up «{server_name}».",
            "__module__": __name__,
            "_server_tool": server_name,
            "_description": str(spec.get("description") or ""),
            "_schema": dict(spec.get("schema") or {"type": "object", "properties": {}}),
            "_concrete": lambda self: None,
        })
    return made


# Загрузчик проекта ищет подклассы Tool среди атрибутов модуля.
globals().update(_make_tools())
