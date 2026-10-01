"""Клиент агента к MCP-серверу ``enterprise-mcp``.

Зачем он агентским сервисам, если в nanobot уже есть MCP-клиент: тот
клиент обслуживает **модель** — он поднимает сервер, регистрирует его
операции как tool'ы и прячет сессию в приватном поле. Программно вызвать
операцию из сервиса агента через него нельзя, а лезть в приватное поле
означало бы привязаться к внутренности нанобота.

Поэтому у агента свой клиент. Он читает то же объявление сервера
(``project.json → enterprise_mcp``) и поднимает **единственный** процесс
сервера. Второй экземпляр означал бы второго владельца пула PostgreSQL,
а владелец у разделяемого ресурса должен быть один.

Жизненный цикл: сессию поднимает gateway рукопожатием на старте, до
каналов и работы агента (``gateway._connect_enterprise_mcp``) — сервер
всегда нужен, поэтому «платформа не отвечает» обязано обнаруживаться на
старте, а не посреди оборота. Ленивый путь в ``_ensure_session`` остался
как восстановление: оборвавшаяся сессия поднимается заново при следующем
вызове.

Про LLM
-------

Этот клиент не передаёт платформе ничего о провайдере модели. Настройки
живут в ``mcp-platform/platform.json`` и принадлежат платформе: она и
делает вызов. Агенту они не нужны, и из этого следует практическое
следствие — ключ провайдера не попадает в окружение процессов скиллов,
которые этот клиент не поднимает.
"""

from __future__ import annotations

import asyncio
import os
import re
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from loguru import logger

DEFAULT_TOOL_TIMEOUT_SEC = 30.0

#: ``[code] message`` — формат доменной ошибки операции (см. build_server).
_ERROR_PREFIX = re.compile(r"^\[([a-z_]+)\]\s*(.*)$", re.DOTALL)


class EnterpriseMcpUnavailable(RuntimeError):
    """Сервер не поднялся, упал или не ответил вовремя."""


class EnterpriseOperationError(RuntimeError):
    """Операция ответила доменной ошибкой.

    Код отделён от текста: вызывающий решает по коду, показывать ли это
    модели, и не разбирает строку.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


def _first_text(result: Any) -> str:
    """Текст первого текстового блока ответа операции."""
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text is not None:
            return str(text)
    return ""


def _split_error_code(text: str) -> tuple[str, str]:
    """Разобрать ``[code] message``; без кода — код ``operation_failed``."""
    match = _ERROR_PREFIX.match(text.strip())
    if match is None:
        return "operation_failed", text.strip()
    return match.group(1), match.group(2).strip()


class EnterpriseMcpClient:
    """Долгоживущая stdio-сессия к одному MCP-серверу.

    Сессию поднимает gateway на старте (см. ``_connect_enterprise_mcp``),
    а если подъём не удался или транспорт оборвался посреди работы — она
    поднимается заново при следующем вызове, иначе агент залипал бы на
    мёртвом процессе до перезапуска.

    Подъём ленивый не как норма, а как единственный способ, которым сессия
    может появиться внутри живого loop: сессия привязана к тому loop, на
    котором создана, и поднять её синхронно (в ``ApplicationContext.start``)
    нельзя — ``asyncio.run`` закрыл бы loop сразу после создания.
    """

    def __init__(
        self,
        *,
        command: str,
        args: list[str] | None = None,
        cwd: str | os.PathLike[str] | None = None,
        tool_timeout_sec: float = DEFAULT_TOOL_TIMEOUT_SEC,
        server_name: str = "enterprise-mcp",
    ) -> None:
        self._command = command
        self._args = list(args or [])
        # Смешанные разделители из резолва ${VAR} недопустимы для cwd
        # на других платформах — приводим к нативному виду.
        self._cwd = str(Path(cwd)) if cwd else None
        self._timeout = float(tool_timeout_sec or DEFAULT_TOOL_TIMEOUT_SEC)
        self._server_name = server_name
        self._stack: AsyncExitStack | None = None
        self._session: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = asyncio.Lock()

    # -- состояние ---------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._session is not None

    def describe(self) -> dict[str, Any]:
        """Сведения для баннера запуска и диагностики."""
        return {
            "server": self._server_name,
            "command": self._command,
            "args": self._args,
            "cwd": self._cwd,
            "connected": self.is_connected,
        }

    # -- вызов -------------------------------------------------------------

    async def list_operations(self) -> list[str]:
        """Имена операций, которые сервер отдаёт в discovery.

        Метод двойного назначения: рукопожатие на старте gateway
        (``_connect_enterprise_mcp``) и диагностика. Нужен не для красоты:
        «сервер поднялся» и «сервер отдаёт те операции, ради которых
        поднялся» — разные утверждения. Без него несовпадение состава
        операций обнаруживалось бы только по жалобе пользователя на
        «индексы не ищутся».

        Raises:
            EnterpriseMcpUnavailable: сервер недоступен или не ответил.
        """
        session = await self._ensure_session()
        try:
            result = await asyncio.wait_for(
                session.list_tools(), timeout=self._timeout
            )
        except asyncio.TimeoutError as exc:
            await self._reset()
            raise EnterpriseMcpUnavailable(
                f"discovery не ответил за {self._timeout:g}с"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - транспорт любой формы
            await self._reset()
            raise EnterpriseMcpUnavailable(f"discovery не удался: {exc}") from exc
        tools = sorted(getattr(result, "tools", None) or [], key=lambda t: t.name)
        return [str(t.name) for t in tools]

    async def call(self, operation: str, arguments: dict[str, Any] | None = None) -> str:
        """Вызвать операцию и вернуть её текстовый ответ.

        Raises:
            EnterpriseOperationError: операция ответила доменной ошибкой.
            EnterpriseMcpUnavailable: сервер недоступен или не ответил.
        """
        session = await self._ensure_session()
        try:
            result = await asyncio.wait_for(
                session.call_tool(operation, arguments or {}),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError as exc:
            await self._reset()
            raise EnterpriseMcpUnavailable(
                f"операция {operation!r} не ответила за {self._timeout:g}с"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - транспорт любой формы
            await self._reset()
            raise EnterpriseMcpUnavailable(
                f"вызов операции {operation!r} не удался: {exc}"
            ) from exc

        text = _first_text(result)
        if getattr(result, "isError", False):
            code, message = _split_error_code(text)
            raise EnterpriseOperationError(code, message)
        return text

    # -- жизненный цикл ----------------------------------------------------

    async def _ensure_session(self) -> Any:
        if self._session is not None:
            return self._session
        async with self._lock:
            if self._session is not None:
                return self._session
            self._stack = AsyncExitStack()
            try:
                from mcp import ClientSession, StdioServerParameters
                from mcp.client.stdio import stdio_client

                read, write = await self._stack.enter_async_context(
                    stdio_client(
                        StdioServerParameters(
                            command=self._command,
                            args=self._args,
                            cwd=self._cwd,
                            env=self._child_env(),
                        )
                    )
                )
                session = await self._stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
            except BaseException as exc:
                await self._stack.aclose()
                self._stack = None
                raise EnterpriseMcpUnavailable(
                    f"сервер {self._server_name!r} не поднялся: {exc}"
                ) from exc
            self._session = session
            self._loop = asyncio.get_running_loop()
            return session

    async def _reset(self) -> None:
        """Сбросить сессию: оборванный процесс недоступен навсегда."""
        stack, self._stack, self._session = self._stack, None, None
        if stack is not None:
            try:
                await stack.aclose()
            except Exception:  # noqa: BLE001 - закрытие не должно ронять вызов
                pass

    async def aclose(self) -> None:
        await self._reset()

    def close(self) -> None:
        """Синхронное закрытие для ``ApplicationContext.stop()``.

        Сессия принадлежит event loop, на котором была создана, поэтому
        закрыть её можно только там же. Если loop уже закрыт (gateway
        закрывает его в ``asyncio.run`` до ``ctx.stop()``) — закрывать
        нечем: stdio-сервер завершается сам, как только у агента
        закроется stdin. Это штатный путь, а не ошибка.
        """
        loop, self._loop = self._loop, None
        if self._stack is None or loop is None:
            self._stack, self._session = None, None
            return
        try:
            if loop.is_running():
                loop.call_soon_threadsafe(
                    lambda: asyncio.ensure_future(self.aclose(), loop=loop)
                )
                return
            if not loop.is_closed():
                loop.run_until_complete(self.aclose())
                return
        except Exception as exc:  # noqa: BLE001 - остановка не должна падать
            logger.warning("enterprise-mcp: не удалось закрыть сессию: %s", exc)
        self._stack, self._session = None, None

    def _child_env(self) -> dict[str, str]:
        """Окружение процесса сервера.

        Полное наследование окружения агента, а не пустое: сервер поднимает
        fail-fast по ``DATABASE_URL`` и без него не стартует, а агент уже
        экспортировал секреты в ``os.environ``.

        ``PYTHONIOENCODING`` задаётся явно: сервер пишет в stderr по-русски,
        и на Windows с cp1251 кодировка консоли убила бы процесс на
        первом же сообщении.

        **Сверх наследования ничего не добавляется, и это не недосмотр.**
        Раньше отсюда уходили объявления индексов и путь снимка
        (``ENTERPRISE_VECTOR_*``, ``ENTERPRISE_EMBED_*``,
        ``ENTERPRISE_SNAPSHOT_PATH``), таблицы аудита с реестром скриптов и
        список runtime-таблиц для ``schema_check``: платформа знала о проекте
        только из чужого окружения. Объявления переехали в
        ``mcp-platform/platform.json`` — свой файл платформы.

        Почему это не «просто уборка»: окружение приоритетнее файла, поэтому
        экспорт не просто дублировал значение, а **молча затирал его**. Как
        только у платформы появлялось своё объявление, старая переменная
        побеждала, и источник значения уезжал в ``env:*`` — то есть файл
        выглядел настроенным и не применялся. Теперь у ``enterprise-mcp``
        нет ни одной настройки, которой владел бы агент.
        """
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        return env


def client_from_settings(settings: Any) -> EnterpriseMcpClient | None:
    """Собрать клиента из ``project.json → enterprise_mcp``.

    ``None`` — раздел выключен или не задан: тогда потребитель сообщает
    об этом структурной ошибкой, а не падает.

    Ни пути снимка, ни объявлений индексов, ни списка таблиц аудита: всё это
    платформа объявляет у себя в ``mcp-platform/platform.json``, и клиент их
    не вычисляет. Второе вычисление того же пути или того же списка разошлось
    бы с первым при первой же правке — а хуже того, окружение приоритетнее
    файла, и такой «экспорт» молча побеждал бы объявление платформы.
    """
    section = settings.get("enterprise_mcp") if settings is not None else None
    if not section:
        return None
    if section.get("enabled") is False:
        return None
    command = section.get("command")
    if not command:
        return None
    return EnterpriseMcpClient(
        command=str(command),
        args=list(section.get("args") or []),
        cwd=section.get("cwd"),
        tool_timeout_sec=float(
            section.get("tool_timeout_sec") or DEFAULT_TOOL_TIMEOUT_SEC
        ),
    )
