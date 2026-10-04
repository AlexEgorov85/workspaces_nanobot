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

Про stderr
----------

Всё, что печатает сервер, уходит в stderr, и по умолчанию этот
stderr — stderr агента: строки платформы оказываются в консоли
вперемешку с журналом агента, и границы между ними не видно.
Объявленный ``stderr_log`` (``gateway.agent.enterprise_mcp``)
перенаправляет stderr в файл и показывает этот файл в отдельном
окне; без объявления поведение прежнее. Механизм, зрители для обеих
платформ и граница со вторым процессом платформы — в
``lib/services/enterprise_mcp_stderr.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from lib.services.enterprise_mcp_stderr import (
    StderrRedirect,
    describe_redirect,
    open_redirect,
    write_marker,
)
from lib.services.session_files import SESSION_FILES_OPERATION

DEFAULT_TOOL_TIMEOUT_SEC = 30.0

#: Предел на закрытие оборванного процесса. Короче таймаута операции: сброс
#: вызывается на каждый отказ, и ждать его дольше, чем отвечает живая
#: платформа, незачем.
RESET_TIMEOUT_SEC = 5.0


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


def _reason(exc: BaseException) -> str:
    """Причина отказа, пригодная для строки лога.

    Отдельная функция потому, что у половины транспортных отказов ``str(exc)``
    ПУСТОЙ: оборванная труба и закрытый поток из anyio приходят с именем класса
    и без текста. В лог уезжало «ping не удался: » — то есть оператор видел
    сам факт и не видел причины, а это худший вид сообщения: выглядит как
    недописанная строка. Имя класса в таком случае и есть причина.
    """
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


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
        stderr_log: str | os.PathLike[str] | None = None,
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
        # Куда девать stderr процесса платформы. Объявленный путь
        # перенаправляет его в файл и открывает файл в отдельном окне;
        # без объявления stderr остаётся stderr агента, как было.
        # Открывается лениво, при первом подъёме сессии: файл журнала
        # должен появляться тогда, когда процесс действительно
        # поднимается, а не при сборке настроек.
        self._stderr_log = stderr_log
        self._stderr: StderrRedirect | None = None
        self._stderr_opened = False
        # Наблюдение за живостью процесса. Отдельные счётчики, а не флаг:
        # оператор должен видеть не только «упало», но и сколько раз подряд и
        # с какого момента. Пишет их только ``probe()`` — состояние сессии само
        # по себе не говорит, отвечает ли процесс.
        self._probes = 0
        self._probe_failures = 0
        self._session_established_at: float | None = None
        self._last_probe_at: float | None = None
        self._last_probe_ok_at: float | None = None
        self._last_probe_error: str | None = None

    # -- состояние ---------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._session is not None

    def health(self) -> dict[str, Any]:
        """Наблюдаемое здоровье клиента: снимок последнего ``probe()``.

        Не догадка по наличию сессии: объект сессии переживает смерть процесса
        до первого неудачного вызова, поэтому ``is_connected`` после остановки
        платформы ещё какое-то время показывает ``True``. Наблюдение — это
        последняя проба и её исход; ``probes``/``probe_failures`` позволяют
        отличить «никогда не проверяли» от «проверяли и падает».

        Проба не выполняется здесь намеренно: она асинхронная и поднимает
        сессию, а этот метод зовут синхронные проверки готовности, которым
        нужен быстрый ответ, а не поход в процесс.
        """
        return {
            "server": self._server_name,
            "connected": self.is_connected,
            "probes": self._probes,
            "probe_failures": self._probe_failures,
            "consecutive_failures": self._probe_failures,
            "session_established_at": self._session_established_at,
            "last_probe_at": self._last_probe_at,
            "last_ok_at": self._last_probe_ok_at,
            "last_error": self._last_probe_error,
        }

    def describe(self) -> dict[str, Any]:
        """Сведения для баннера запуска и диагностики."""
        return {
            "server": self._server_name,
            "command": self._command,
            "args": self._args,
            "cwd": self._cwd,
            "connected": self.is_connected,
            # Объявленный путь виден и до подъёма сессии: баннер
            # запуска печатается по ``describe()``, и перенаправление
            # не должно выглядеть включившимся само по себе.
            **describe_redirect(self._stderr, declared=self._stderr_log),
        }

    def stderr_report(self) -> str:
        """Стока для баннера запуска: куда ушёл stderr платформы.

        Отчёт, а не признак успеха: файл мог не открыться, а окно —
        не появиться (нет ``DISPLAY``, нет терминала). Молчаливое
        "смотреть можно" хуже, чем строка, где сказано, что именно
        видно и почему.
        """
        if self._stderr is not None:
            # Файл называет сам клиент, а не текст зрителя: отчёт
            # переживает подмену зрителя (в тестах, в headless) и
            # не должен зависеть от того, как именно окно себя
            # представило.
            return (
                f"stderr платформы: файл {self._stderr.path} — "
                f"{self._stderr.viewer}"
            )
        if self._stderr_log:
            return (
                f"stderr платформы: файл {self._stderr_log} не открыт — "
                "вывод идёт в stderr агента"
            )
        return (
            "stderr платформы: stderr агента "
            "(gateway.agent.enterprise_mcp.stderr_log не объявлен)"
        )

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
        except TimeoutError as exc:
            await self._reset()
            raise EnterpriseMcpUnavailable(
                f"discovery не ответил за {self._timeout:g}с"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - транспорт любой формы
            await self._reset()
            raise EnterpriseMcpUnavailable(f"discovery не удался: {exc}") from exc
        tools = sorted(getattr(result, "tools", None) or [], key=lambda t: t.name)
        return [str(t.name) for t in tools]

    async def probe(self) -> None:
        """Одно наблюдение за живостью: ping протокола на живой сессии.

        Единственный честный признак «процесс отвечает» — обмен по протоколу
        MCP. Проверять ``is_connected`` недостаточно: объект сессии переживает
        смерть процесса и остаётся не-``None`` до первого неудачного вызова,
        то есть система о «платформа остановлена» узнавала бы только тогда,
        когда кто-то уже попытался что-то сделать.

        Проба лечит, а не только измеряет: сессия поднимается, если её нет, и
        обрывается, если не отвечает. Поэтому вызывающая сторона получает
        восстановление без отдельного механизма — просто следующая проба.

        ``ping`` выбран намеренно: он не трогает бизнес-операцию, не ходит в
        PostgreSQL и не имеет побочных эффектов, то есть в отличие опроса
        зеркала или ``list_operations`` ничего не меняет.

        Raises:
            EnterpriseMcpUnavailable: сервер не поднялся, не ответил на ping
                или оборвался.
        """
        self._probes += 1
        self._last_probe_at = time.time()
        try:
            # Таймаут на ВСЮ пробу, а не только на ping. Подъём сессии тоже
            # ждёт ответа процесса — ``initialize()`` не имел своего предела,
            # и платформа, которая стартует, но не отвечает, навсегда
            # оставляла наблюдение в подвешенном состоянии: цикл не доходил
            # до записи DOWN и переставал замечать что-либо вообще. Смерть
            # процесса выглядит как «тишина», а не как отказ, — ровно то, что
            # наблюдение обязано ловить.
            await asyncio.wait_for(
                self._probe_once(), timeout=self._timeout
            )
        except EnterpriseMcpUnavailable:
            self._record_probe_failure("сервер недоступен")
            raise
        except TimeoutError as exc:
            await self._reset()
            self._record_probe_failure(
                f"проба не уложилась в {self._timeout:g}с"
            )
            raise EnterpriseMcpUnavailable(
                f"проба не уложилась в {self._timeout:g}с"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - транспорт любой формы
            await self._reset()
            reason = _reason(exc)
            self._record_probe_failure(reason)
            raise EnterpriseMcpUnavailable(f"ping не удался: {reason}") from exc
        self._probe_failures = 0
        self._last_probe_ok_at = self._last_probe_at
        self._last_probe_error = None

    async def _probe_once(self) -> None:
        """Подъём сессии и ping. Оборачивается таймаутом в ``probe()``.

        Отдельно от ``probe()`` — чтобы ``asyncio.wait_for`` отменял именно
        работу с процессом, а логика отказа и запись в снимок оставались в
        одном месте.
        """
        session = await self._ensure_session()
        await session.send_ping()

    def _record_probe_failure(self, reason: str) -> None:
        self._probe_failures += 1
        self._last_probe_error = reason

    def _identity_from_turn(self) -> CallIdentity | None:
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
        identity: CallIdentity | None = None,
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
        except TimeoutError as exc:
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

    async def session_files(
        self,
        identity: CallIdentity | None = None,
        *,
        ensure: bool = True,
    ) -> dict[str, Any]:
        """Каталог сессии от платформы — единственный источник пути на её стороне.

        Корень, имя каталога и раскладку объявляет платформа
        (``mcp-platform/platform.json → execution.session_root``), поэтому клиент
        не только не вычисляет путь, но и не передаёт никакого: аргумент у
        вызова один — ``ensure``. Переданный корень перебил бы объявление файла
        (среда приоритетнее объявления), и настройка снова выглядела бы
        настроенной, не применяясь.

        ``identity``, если он передан, уходит в ``_meta``; иначе личность
        достраивает сам ``call()`` из текущего оборота. Собирать её здесь
        второй раз нельзя: событие в журнале и каталог сессии тогда описали бы
        разные вызовы.

        Raises:
            EnterpriseOperationError: операция ответила не объектом JSON. Метод
                не подменяет содержимое ответа и не заполняет пробелы: путь,
                которого не прислали, резолвер трактует как отказ.
        """
        text = await self.call(
            SESSION_FILES_OPERATION, {"ensure": bool(ensure)}, identity=identity
        )
        try:
            answer = json.loads(text)
        except ValueError as exc:
            raise EnterpriseOperationError(
                "session_files_invalid",
                f"ответ операции {SESSION_FILES_OPERATION!r} не JSON: {exc}",
            ) from exc
        if not isinstance(answer, dict):
            raise EnterpriseOperationError(
                "session_files_invalid",
                f"ответ операции {SESSION_FILES_OPERATION!r} — не объект: "
                f"{type(answer).__name__}",
            )
        return answer

    def _meta_for(self, identity: CallIdentity | None) -> dict[str, str] | None:
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

    def _errlog(self) -> Any:
        """Приёмник stderr процесса: файл из объявления или ``None``.

        ``None`` — значение по умолчанию у ``stdio_client``, то есть
        stderr агента: без объявления перенаправления не происходит и
        SDK ведёт себя ровно как раньше. Файл открывается один раз на
        клиента, и переподключение после обрыва дописывает в него же.
        """
        if not self._stderr_opened:
            self._stderr_opened = True
            self._stderr = open_redirect(self._stderr_log)
        return self._stderr.handle if self._stderr is not None else None

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
                        ),
                        # Публичный параметр SDK: stderr процесса в
                        # stderr агента или в объявленный файл.
                        errlog=self._errlog(),
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
            self._session_established_at = time.time()
            return session

    async def _reset(self) -> None:
        """Сбросить сессию: оборванный процесс недоступен навсегда.

        Закрытие стека тоже под таймаутом. Сброс вызывается на каждый отказ, и
        ждать его завершения без предела опасно: если оборван был процесс,
        который не дошёл до конца рукопожатия, его закрытие может не вернуться,
        а тогда зависнет не только проба, но и любой вызов, который к ней
        обратится.
        """
        stack, self._stack, self._session = self._stack, None, None
        if stack is not None:
            try:
                await asyncio.wait_for(stack.aclose(), timeout=RESET_TIMEOUT_SEC)
            except (TimeoutError, Exception):  # noqa: BLE001 - закрытие не должно ронять вызов
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
        # Метка видна в окне зрителя: вывод перестанет появляться, и
        # без неё молчание окна выглядело бы зависшей платформой.
        write_marker(
            self._stderr, "— сессия enterprise-mcp закрывается агентом —"
        )
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


#: Путь к порогу журнала в конфигурации агента. Объявлен один раз и на
#: чтение, и на сверку: писатель журнала агента
#: (``lib/core/application_context.py``) читает **этот же** ключ, и страж
#: ``tests/test_journal_threshold_reaches_mcp.py`` падает, если чтения
#: разъедутся. Ключ поднят в корень ``SETTINGS`` из ``gateway.agent`` функцией
#: ``config._lift_agent_sections`` — читать надо ``SETTINGS["logging"]``, а не
#: ``SETTINGS["gateway"]["agent"]["logging"]``: второго пути к тому же значению
#: быть не должно.
#:
#: Второй читатель того же ключа — ``config._export_platform_process_env``,
#: он объявляет путь у себя в ``config.py``. Объявить его здесь и импортировать
#: отсюда нельзя: ``config`` — базовый модуль, и потянуть в него сервисный
#: слой нельзя. Равенство двух объявлений проверяет
#: ``tests/test_mcp_platform_declaration.py``.
JOURNAL_MIN_LEVEL_PATH: tuple[str, ...] = ("logging", "db", "min_level")

#: Имя флага запуска, которым порог доезжает до платформы. Литерал с обеих
#: сторон процесса, как ``--profile``: общего модуля у агента и платформы нет,
#: а объявлять флаг в третьем месте — значит завести ещё одно объявление.
LOG_MIN_LEVEL_FLAG = "--log-min-level"


def _journal_min_level(settings: Any) -> str:
    """Порог журнала из ``config.json → gateway.agent.logging.db.min_level``.

    Значение уходит строкой **как есть**: разбором уровня занимается
    платформа, где шкала объявлена один раз
    (``libs/enterprise_common/eventing/models.py``), и незнакомый уровень там
    роняет старт с названным значением. Тихая замена на ``INFO`` здесь
    переключила бы платформу на другую политику молча — ровно то, чем
    кончается любой откат «на всякий случай».

    Пустая строка — «флага нет»: платформа пишет всё, как сейчас.
    """
    if settings is None:
        return ""
    node: Any = settings
    for key in JOURNAL_MIN_LEVEL_PATH:
        node = node.get(key) if hasattr(node, "get") else None
        if node is None:
            return ""
    return str(node).strip()


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

    Третий ключ раздела, ``stderr_log``, до платформы не доезжает и
    остаётся на стороне агента: это путь файла-приёмника stderr
    процесса. Пустое значение или отсутствие ключа означают прежнее
    поведение — stderr уходит в stderr агента.

    Из настроек агента платформа получает ровно два значения: имя контура
    (``--profile``) и порог журнала (``--log-min-level``). Оба — при старте,
    оба читаются из ключей, которые платформа не объявляет, и оба приходят
    **как есть**: разбор и отказ на незнакомом значении — на стороне, где
    шкала объявлена.
    """
    section = settings.get("enterprise_mcp") if settings is not None else None
    if not section:
        return None
    if section.get("enabled") is False:
        return None
    command = section.get("command")
    if not command:
        return None
    args = list(section.get("args") or [])
    # Агент передаёт платформе имя контура (и только когда контур отличается
    # от базы) и НЕ значения имён таблиц: те объявлены в
    # ``mcp-platform/platform.json → profiles.<имя>``, и значение, присланное
    # вызывающей стороной, сделало бы вход в данные агента независимым от его
    # конфигурации (ровно тот класс дефекта, который чинили в фазе 9 —
    # «окружение приоритетнее файла»).
    profile = str((settings.get("profile") or "") if settings is not None else "").strip()
    if profile and profile != "prod":
        args += ["--profile", profile]
    # Порог журнала — тот же ключ, что у писателя агента, и тоже ОДИН раз при
    # старте. В ``params._meta`` каждого вызова он не едет: значение,
    # перечитываемое на каждый вызов, способно разъехаться между вызовами
    # одного оборота, а вызывающая сторона получила бы право решать, сколько
    # логировать платформа, — а это её собственные события (``tool.*``,
    # ``quality.check``), и подменять им политику вызывающего нельзя.
    min_level = _journal_min_level(settings)
    if min_level:
        args += [LOG_MIN_LEVEL_FLAG, min_level]
    # stderr_log — НЕ настройка платформы и в argv не едет: это
    # вопрос транспорта на стороне агента (куда девать stderr процесса,
    # который агент и поднимает), а не объявление платформы. Поэтому
    # его чтение не ломает контракт «агент не объявляет ничего для
    # платформы», и страж границы остаётся зелёным.
    stderr_log = str(section.get("stderr_log") or "").strip() or None
    return EnterpriseMcpClient(
        command=str(command),
        args=args,
        cwd=section.get("cwd"),
        stderr_log=stderr_log,
        tool_timeout_sec=float(
            section.get("tool_timeout_sec") or DEFAULT_TOOL_TIMEOUT_SEC
        ),
        db_logging_service=db_logging_service,
    )
