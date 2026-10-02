"""Клиент агента к MCP-серверу ``enterprise-mcp``.

Зачем он агентским сервисам, если в nanobot уже есть MCP-клиент: тот
клиент обслуживает **модель** — он поднимает сервер, регистрирует его
операции как tool'ы и прячет сессию в приватном поле. Программно вызвать
операцию из сервиса агента через него нельзя, а лезть в приватное поле
означало бы привязаться к внутренности нанобота.

Поэтому у агента свой клиент. Он читает то же объявление сервера
(``config.json → gateway.agent.enterprise_mcp``) и поднимает **единственный**
процесс сервера. Второй экземпляр означал бы второго владельца пула
PostgreSQL, а владелец у разделяемого ресурса должен быть один.

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
import uuid
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

DEFAULT_TOOL_TIMEOUT_SEC = 30.0


def _new_request_id() -> str:
    """``request_id`` для вызова, у которого нет оборота.

    Форма та же, что у остальных ``request_id`` агента (``db_logging_bus``,
    ``DbLoggingService``): ``str(uuid4())``. Именно агент создаёт это значение —
    сервер не придумывает его по двум причинам: во-первых, он не знает, к
    какому обороту вызов относится, во-вторых, выдуманный на сервере ключ
    выглядел бы в журнале как существующий оборот.

    Отдельная префиксная форма платформы (``local-``) тут не нужна: она
    помечает локальные вызовы самой платформы, а этот ``request_id`` держит
    агент, у которого своя форма во всём остальном журнале.
    """
    return str(uuid.uuid4())

#: ``[code] message`` — формат доменной ошибки операции (см. build_server).
_ERROR_PREFIX = re.compile(r"^\[([a-z_]+)\]\s*(.*)$", re.DOTALL)

#: Префикс проектных ключей в ``params._meta``. Обязан совпадать с
#: ``META_PREFIX`` из ``mcp-platform/libs/enterprise_common/execution/context.py``:
#``MCP`` резервирует ``io.modelcontextprotocol/*`` под свой служебный обмен,
# поэтому голые имена без префикса — шаг к коллизии с чужим расширением.
META_PREFIX = "workspaces/"


@dataclass(frozen=True, slots=True)
class CallIdentity:
    """Идентичность одного вызова: сессия, пользователь, оборот.

    ``session_id`` и ``user_id`` обязательны: вызов без изоляции выполнять
    нельзя, и сервер отклоняет его сам (``identity_missing``). Подставляет
    их вызывающая сторона агента — из ``RequestContext`` и журнала, а не
    модель: значение, присланное моделью, границей изоляции не является.

    ``request_id`` — PK оборота в ``agent_question_runs``. Поле может быть
    ``None``: в момент, когда личность собирается, оборота может ещё не
    существовать, и это не повод отказывать в вызове. Но **перед отправкой
    ``request_id`` обязателен** — его дополняет :meth:`EnterpriseMcpClient.call`,
    единственная точка сборки ``_meta``.

    Обход этого правила на стороне сервера выглядел бы безобидно, пока не
    посчитать: сервер требует все три ключа и отвечает ``identity_missing``
    на двухключевой ``_meta``. То есть вызов вне оборота, для которого не нашли
    PK, тихо не проходил бы — а падал бы отказом, который выглядит как
    «платформа не работает».
    """

    session_id: str
    user_id: str
    request_id: str | None = None

    def with_request_id(self, request_id: str) -> CallIdentity:
        """Возвращает копию с заданным ``request_id``."""
        return CallIdentity(
            session_id=self.session_id,
            user_id=self.user_id,
            request_id=request_id,
        )

    def as_meta(self) -> dict[str, str]:
        """Собрать ``params._meta``. Требует полного набора ключей.

        Отказ здесь, а не молчаливое опускание ``request_id``: два ключа вместо
        трёх отвергаются на сервере одинаково — с ``identity_missing``, — но
        приходят с другого конца и выглядят совсем иначе. Здесь виден вызов и
        видно, чего в нём не хватило.
        """
        missing = [
            name
            for name, value in (
                ("session_id", self.session_id),
                ("user_id", self.user_id),
                ("request_id", self.request_id),
            )
            if not str(value or "").strip()
        ]
        if missing:
            raise CallIdentityIncomplete(
                "идентичность вызова неполна: " + ", ".join(missing)
            )
        return {
            f"{META_PREFIX}session_id": self.session_id,
            f"{META_PREFIX}user_id": self.user_id,
            f"{META_PREFIX}request_id": str(self.request_id),
        }


class CallIdentityIncomplete(ValueError):
    """Идентичность вызова собрана не полностью.

    Отдельный тип, а не ``ValueError``, потому что это не ошибка значения, а
    нарушение контракта на границе: сначала должен быть найден оборот, и только
    потом отправлен вызов.
    """


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
        db_logging_service: Any = None,
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
        # Источник ``request_id`` текущего оборота. Приходит сюда же, куда и
        # остальные сервисы, - из ApplicationContext. Создавать ради этого
        # отдельный компонент незачем: нужен метод журнала, а не новый объект.
        self._db_logging_service = db_logging_service

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

    def _identity_from_turn(self) -> "CallIdentity | None":
        """Собрать личность вызова из доверенного контекста оборота.

        Это единственное место, где личность вызова появляется сама. Раньше
        каждый tool' собирал её руками - одна и та же функция копировалась по
        проекту, и любая из копий могла разойтись с остальными, а расхождение
        не было бы заметно нигде: вызов либо проходил, либо нет.

        Источник - ``RequestContext`` и журнал оборотов, а не аргументы
        вызова. Значение, присланное моделью или вызывающим tool'ом, границей
        изоляции не является.

        Вне оборота возвращается ``None``: тогда ``_meta`` не отправляется
        вовсе, и сервер сам отвечает ``identity_missing``. Выдумывать
        значения здесь нельзя - подставленная сессия выглядела бы в журнале
        как настоящая.
        """
        try:
            from nanobot.agent.tools.context import (
                current_request_context,
                current_request_session_key,
            )
        except Exception:
            return None
        try:
            turn = current_request_context()
        except Exception:
            return None
        if turn is None:
            return None

        try:
            session_id = current_request_session_key()
        except Exception:
            session_id = None
        if not session_id:
            return None

        sender_id = getattr(turn, "sender_id", None)
        user_id = sender_id if isinstance(sender_id, str) and sender_id else None
        if not user_id:
            return None

        request_id = None
        logging_service = self._db_logging_service
        if logging_service is not None:
            try:
                request_id = logging_service.get_request_id(str(session_id))
            except Exception:
                # Журнал недоступен - это не повод отказывать в вызове: связь
                # с agent_question_runs выразится признаком correlated=false,
                # который проставит сервер.
                request_id = None

        return CallIdentity(
            session_id=str(session_id),
            user_id=user_id,
            request_id=request_id,
        )

    async def call(
        self,
        operation: str,
        arguments: dict[str, Any] | None = None,
        *,
        identity: "CallIdentity | None" = None,
    ) -> str:
        """Вызвать операцию и вернуть её текстовый ответ.

        ``identity`` едет в ``params._meta`` вызова, а не в аргументы: сервер
        читает идентичность только оттуда (см. ``execution/context.py``),
        поэтому в аргументах её быть не должно — иначе у вызова появляется
        второй источник идентичности, а событие в журнале и каталог сессии
        могут описывать разные вызовы.

        ``request_id`` дополняется здесь, если вызывающая сторона его не
        нашла: сервер требует все три ключа и создавать ``request_id`` не
        вправе, поэтому единственное место, где его можно получить законно, —
        эта сборка. Отсутствие связи с ``agent_question_runs`` выражается не
        отказом, а признаком ``metadata.correlated = false``, который сервер
        проставит сам.

        Без ``identity`` ``_meta`` не отправляется вовсе, а не отправляется
        пустым: пустой ``_meta`` на сервере неотличим от «идентичность была и
        оказалась пустой».

        Raises:
            EnterpriseOperationError: операция ответила доменной ошибкой.
            EnterpriseMcpUnavailable: сервер недоступен или не ответил.
        """
        session = await self._ensure_session()
        # Личность, которую не собрал вызывающий, достраивается здесь.
        # Явный ``identity`` всегда выигрывает: подмена источника - это
        # осознанное решение вызывающей стороны, а не запасной путь.
        meta = self._meta_for(
            identity if identity is not None else self._identity_from_turn()
        )
        try:
            # ``meta`` не передаётся вовсе, когда идентичности нет: пустой
            # ``_meta`` на сервере неотличим от «идентичность была и пустая».
            call = (
                session.call_tool(operation, arguments or {}, meta=meta)
                if meta is not None
                else session.call_tool(operation, arguments or {})
            )
            result = await asyncio.wait_for(call, timeout=self._timeout)
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

    def _meta_for(self, identity: "CallIdentity | None") -> dict[str, str] | None:
        """``_meta`` для вызова: полный набор ключей или ничего.

        ``request_id`` вызывающая сторона знает не всегда — оборот мог ещё не
        зарегистрироваться, а журнал мог не ответить. Здесь он досоставляется
        самостоятельным значением, и именно поэтому сервер не станет
        придумывать его сам: единственное место, где значение можно получить
        законно, — сборка вызова на стороне агента.

        Сервер определит, ссылается ли полученный ``request_id`` на реальный
        оборот, и проставит ``metadata.correlated = false`` сам. Поэтому
        сгенерированное значение не выдаёт себя за существующий оборот: в
        журнале связь просто не найдётся, и это нормально.
        """
        if identity is None:
            return None
        if not identity.request_id:
            generated = _new_request_id()
            logger.info(
                "вызов без request_id оборота: подставлен самостоятельный "
                f"request_id={generated}, связь с agent_question_runs не появится"
            )
            identity = identity.with_request_id(generated)
        return identity.as_meta()

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


def client_from_settings(
    settings: Any, *, db_logging_service: Any = None
) -> EnterpriseMcpClient | None:
    """Собрать клиента из ``config.json → gateway.agent.enterprise_mcp``.

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
        db_logging_service=db_logging_service,
    )
