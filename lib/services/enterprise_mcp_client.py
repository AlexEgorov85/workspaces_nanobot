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

Про транспорт
------------

Два транспорта, один переключатель — ``transport`` в
``config.json → gateway.agent.enterprise_mcp``.

``stdio`` — путь отката и значение по умолчанию: процесс платформы
stdio-каналом, порт выбирает сам клиент.
``http`` — тот же дочерний процесс, обслуживаемый по
``streamable-http``. Процесс остаётся дочерним (второй владелец пула
PostgreSQL не появится), меняется только конвейер.

На http-ветке агент поднимает процесс сам, и это меняет три вещи.
Первое: адрес он узнаёт не из ответа клиента, а из строки в канале
уведомления — выделенном дескрипторе, который агент открывает ребёнку
сам (``stdin``-слот: на POSIX и на Windows это единственный канал,
наследуемый как номер дескриптора, доступный ребёнку — см.
:func:`_open_notify_channel`). Второе: stderr ребёнку открывает агент
(``errlog`` у ``stdio_client`` тут неприменим), в тот же объявленный
файл и тем же :func:`~lib.services.enterprise_mcp_stderr.open_redirect`,
то есть окно зрителя и прежний путь отката не меняются. Третье: stdout
ребёнка уходит в ``DEVNULL`` — на этой ветке протокола на нём нет, а
пустота держит страж ``TestServerNeverPrintsToStdout``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import subprocess
import sys
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
from config import ConfigurationError
from lib.services.service_identity import SERVICE_IDENTITIES
from lib.services.session_files import SESSION_FILES_OPERATION
from lib.services.turn_identity import read_turn_context, request_id_from

DEFAULT_TOOL_TIMEOUT_SEC = 30.0

#: Предел на закрытие оборванного процесса. Короче таймаута операции: сброс
#: вызывается на каждый отказ, и ждать его дольше, чем отвечает живая
#: платформа, незачем.
RESET_TIMEOUT_SEC = 5.0

#: Транспорт сессии. ``stdio`` — путь отката и значение по умолчанию,
#: ``http`` — обслуживание дочернего процесса по streamable-http.
TRANSPORT_STDIO = "stdio"
TRANSPORT_HTTP = "http"
TRANSPORTS: tuple[str, ...] = (TRANSPORT_STDIO, TRANSPORT_HTTP)

#: Предел на чтение строки уведомления. Платформа пишет её сразу после
#: бинда, поэтому здесь не «время работы», а время подъёма её
#: зависимостей: импорты, чтение реестра, сборка индексов.
NOTIFY_READ_TIMEOUT_SEC = 180.0

#: Потолок одной строки уведомления. Адрес — это ``host:port`` и три
#: числа; всё длиннее — не адрес, и читать дальше незачем.
NOTIFY_MAX_LINE = 4096


def _new_request_id() -> str:
    """``request_id`` для вызова, у которого нет оборота.

    **Здесь подстановка обязательна, и она НЕ противоречит запрету выдумывать
    идентификатор в журнале.** Это два разных контракта одного имени поля:
    в конверте MCP-вызова ``request_id`` обязателен всегда, и сервер не
    генерирует его никогда (``mcp-platform/docs/MCP-CONTRACTS.md:146-150``), а
    в журнале (``agent_gateway_logs.request_id``) он заполнен только когда есть
    оборот, и выдуманное значение там запрещено (change
    2026-10-04-queue-as-anchor-identity, Ф0). Этот идентификатор в
    ``agent_gateway_logs`` не попадает: он живёт внутри ``params._meta``
    конверта, а не в колонке журнала.

    Именно агент создаёт это значение: сервер не придумывает его по двум
    причинам: во-первых, он не знает, к какому обороту вызов относится,
    во-вторых, выдуманный на сервере ключ выглядел бы в журнале как
    существующий оборот.

    Отдельная префиксная форма платформы (``local-``) тут не нужна: она
    помечает локальные вызовы самой платформы, а этот ``request_id`` держит
    агент.
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


class EnterpriseMcpPortBusy(EnterpriseMcpUnavailable):
    """Закреплённый порт платформы занят — подъём отменён.

    Отдельный тип, а не текст в ``EnterpriseMcpUnavailable``: решение о
    подъёме не принято по существу (порт занят чужим процессом), и
    повтор/restart его не исправит. ``holder_pid`` — доказательство, на
    котором отказ стоит, поэтому оно и в тексте, и отдельным полем.
    """

    def __init__(self, message: str, *, holder_pid: int | None) -> None:
        super().__init__(message)
        self.holder_pid = holder_pid


#: Адреса, на которых сервер платформы вправе слушать. Выход за loopback
#: запрещён до появления потребителя, который не является дочерним процессом
#: того же агента.
LOOPBACK_BINDS = frozenset({"127.0.0.1", "::1"})


def loopback_bind(bind: str) -> str:
    """Проверить, что адрес слушания — loopback, и вернуть его.

    ``localhost`` разворачивается в ``127.0.0.1`` здесь, а не в резолвере ОС:
    результат резолва — это уже чужое решение о нашем адресе. Проверка живёт
    здесь, а не только в платформе: отказ платформы пришёл бы после подъёма
    процесса, и оператор увидел бы упавший ребёнок вместо отказа по
    объявлению.
    """
    value = str(bind or "").strip()
    if value.lower() == "localhost":
        return "127.0.0.1"
    if value not in LOOPBACK_BINDS:
        raise ConfigurationError(
            f"gateway.agent.enterprise_mcp.transport.bind={bind!r}: сервер "
            f"платформы слушает только loopback ({', '.join(sorted(LOOPBACK_BINDS))}), "
            f"а 0.0.0.0 выставил бы порт наружу машины."
        )
    return value


def find_listener_pid(host: str, port: int) -> int | None:
    """PID процесса, слушающего ``host:port``; ``None`` — не определён.

    Единственная реализация на обе платформы. Windows смотрит
    ``netstat -ano -p TCP`` (нативного API в PowerShell нет), Linux
    разбирает ``/proc``: сокеты в состоянии LISTEN на этом порту дают
    inode, а inode ищется в ``/proc/<pid>/fd``.

    Почему на Linux procfs, а не ``ss``/``lsof``: ``ss`` есть не везде
    (в минимальных образах его нет вовсе), его формат вывода меняется
    между версиями iproute2 и переводится в локализованных сборках, а
    ``/proc/net/tcp`` — это таблица ядра с фиксированными колонками.
    Адрес в ней не сверяется: занятость ``host:port`` уже доказана
    пробой bind, а слушатель этого порта — и есть держатель, даже если
    он висит на ``0.0.0.0``.

    Ветка Linux написана по коду и на этой машине (Windows) не проверена.
    """

    if sys.platform.startswith("win"):
        return _windows_listener_pid(host, port)
    return _linux_listener_pid(port)


def check_platform_port(host: str, port: int) -> None:
    """Отказать, если закреплённый порт платформы занят.

    Механизм тот же, что у проверки порта канала в
    ``gateway._check_websocket_port_available``: проба bind тем же
    сокетом, что и настоящий bind. Проверяется **только** закреплённый
    порт — при ``0`` адрес назначает ОС в момент бинда, конфликт
    структурно невозможен, и проверять нечего.

    Raises:
        EnterpriseMcpPortBusy: порт занят, в тексте названы PID держателя
            (либо сказано, что он не определён) и POSIX/Windows-средство
            завершения для этой ОС.
    """
    if not port:
        return
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError as exc:
            holder = find_listener_pid(host, port)
            raise EnterpriseMcpPortBusy(
                port_busy_message(host, port, holder), holder_pid=holder
            ) from exc


def port_busy_message(
    host: str, port: int, holder_pid: int | None, *, posix: bool | None = None
) -> str:
    """Формулировка отказа по занятому порту — под текущий случай.

    Различает живого держателя и «порт занят, а кто — не определилось»,
    и не утверждает, что «остался висеть предыдущий процесс»: чаще порт
    держит **живой** второй gateway в соседнем окне, и такое утверждение
    предлагает оператору погасить работающую систему.

    ``posix`` переопределяет определение ОС — им проверяется POSIX-ветка
    на машине без Linux.
    """
    if posix is None:
        posix = not sys.platform.startswith("win")
    where = f"{host}:{port}"
    if holder_pid is None:
        holder = "держатель НЕ ОПРЕДЕЛЁН (порт занят, а процесс не найден)"
        hint = (
            f"найдите его сами: ss -ltnp 'sport = :{port}' (Linux) "
            f"или netstat -ano -p TCP (Windows)"
        )
    else:
        holder = f"держит процесс {holder_pid}"
        hint = (
            f"kill {holder_pid} (POSIX)"
            if posix
            else f"taskkill /PID {holder_pid} /F"
        )
    return (
        f"порт {where} платформы уже занят — {holder}. Подъём отменён, "
        f"процесс платформы не запускался. Если это живой второй агент, "
        f"остановите его (Ctrl+C в его окне), иначе завершите держателя: {hint}"
    )


def _windows_listener_pid(host: str, port: int) -> int | None:
    """Держатель ``host:port`` на Windows: ``netstat -ano -p TCP``."""

    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout
    except Exception:
        return None

    pattern = re.compile(
        rf"\s+TCP\s+{re.escape(host)}:{port}\s+\S+\s+LISTENING\s+(\d+)\s*"
    )
    m = pattern.search(out)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


#: Состояние ``LISTEN`` в ``/proc/net/tcp`` — ``st`` в шестнадцатеричном виде.
_TCP_LISTEN = "0A"


def _linux_listener_pid(port: int) -> int | None:
    """Держатель порта на Linux по ``/proc``. На Windows не проверялось."""

    inodes = _linux_listen_inodes(port)
    if not inodes:
        return None
    proc = Path("/proc")
    try:
        entries = sorted(proc.iterdir(), key=lambda p: p.name)
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            handles = list((entry / "fd").iterdir())
        except OSError:
            # чужой процесс: /proc/<pid>/fd читает не владелец
            continue
        for handle in handles:
            try:
                target = os.readlink(handle)
            except OSError:
                continue
            if target.startswith("socket:[") and target[8:-1] in inodes:
                return int(entry.name)
    return None


def _linux_listen_inodes(port: int) -> set[str]:
    """inode'ы слушающих сокетов на ``port`` из ``/proc/net/tcp{,6}``.

    Колонки строки: ``sl local_address rem_address st tx_queue:rx_queue
    tr:tm->when retrnsmt uid timeout inode``; локальный порт — последняя
    часть ``local_address`` в верхнем регистре, ``st == 0A`` — LISTEN.
    """

    wanted = f"{port:04X}"
    inodes: set[str] = set()
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(name).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines[1:]:  # первая строка — заголовок колонок
            parts = line.split()
            if len(parts) < 10 or parts[3] != _TCP_LISTEN:
                continue
            if parts[1].rsplit(":", 1)[-1] == wanted:
                inodes.add(parts[9])
    return inodes


def _parse_notification(line: bytes) -> dict[str, Any]:
    """Разобрать строку уведомления платформы.

    Единственный формат описан в ``runtime/platform-settings`` (одна строка
    JSON с ``host``/``port``/``pid``). Всё, что этому не соответствует, —
    отказ: молча пропущенная строка означала бы подъём по чужому адресу.
    """
    try:
        payload = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EnterpriseMcpUnavailable(
            f"строка уведомления платформы не разбирается: {line[:120]!r}"
        ) from exc
    if not isinstance(payload, dict):
        raise EnterpriseMcpUnavailable(
            f"строка уведомления платформы — не объект: {line[:120]!r}"
        )
    host = payload.get("host")
    port = payload.get("port")
    pid = payload.get("pid")
    # ``bool`` — подкласс ``int``, и ``True`` вместо pid прошло бы как число.
    broken: list[str] = []
    if not isinstance(host, str) or not host.strip():
        broken.append("host")
    if not isinstance(port, int) or isinstance(port, bool) or port <= 0:
        broken.append("port")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        broken.append("pid")
    if broken:
        raise EnterpriseMcpUnavailable(
            f"в уведомлении платформы не годятся: {', '.join(broken)} — {line[:120]!r}"
        )
    return {"host": host, "port": port, "pid": pid}


async def _read_notification(fd: int, *, child_pid: int) -> dict[str, Any]:
    """Прочитать ровно одно уведомление, с отказом вместо ожидания.

    Наивное ``readline()`` от мёртвого ребёнка повесило бы старт, поэтому
    читаем под пределом и различаем три исхода: канал закрылся (процесс
    умер до бинда), время вышло, строка нечитаема. Повтор — тоже отказ:
    два разных адреса от одного процесса означают, что адрес не тот, за кем
    его приняли.
    """
    loop = asyncio.get_running_loop()
    deadline = time.monotonic() + NOTIFY_READ_TIMEOUT_SEC
    buffer = b""
    while b"\n" not in buffer:
        left = deadline - time.monotonic()
        if left <= 0:
            raise EnterpriseMcpUnavailable(
                f"платформа (pid {child_pid}) не сообщила фактический адрес за "
                f"{int(NOTIFY_READ_TIMEOUT_SEC)} с"
            )
        try:
            chunk = await asyncio.wait_for(
                loop.run_in_executor(None, os.read, fd, NOTIFY_MAX_LINE), timeout=left
            )
        except TimeoutError as exc:
            raise EnterpriseMcpUnavailable(
                f"платформа (pid {child_pid}) не сообщила фактический адрес за "
                f"{int(NOTIFY_READ_TIMEOUT_SEC)} с"
            ) from exc
        if not chunk:
            raise EnterpriseMcpUnavailable(
                f"платформа (pid {child_pid}) закрыла канал уведомления, не "
                f"сообщив адрес: процесс умер до бинда, причина — в его stderr"
            )
        buffer += chunk
        if len(buffer) > NOTIFY_MAX_LINE:
            raise EnterpriseMcpUnavailable(
                f"строка уведомления длиннее {NOTIFY_MAX_LINE} байт — это не адрес"
            )
    line, _, rest = buffer.partition(b"\n")
    if rest.strip() or _notify_tail_ready(fd):
        raise EnterpriseMcpUnavailable(
            "платформа прислала второе уведомление: один процесс объявил два "
            "адреса, и адрес не тот, за кем его приняли"
        )
    return _parse_notification(line)


def _notify_tail_ready(fd: int) -> bool:
    """Есть ли уже пришедший хвост канала. Неблокирующая проверка.

    Ждать второго уведомления нельзя (его может не быть никогда), а
    пропустить пришедшее нельзя. Неблокирующий режим есть не на всех
    платформах и версиях Python для анонимного канала — тогда проверка
    молча выдыхает, и это лучше отказа на живом подъёме.
    """
    try:
        os.set_blocking(fd, False)
    except OSError:
        return False
    try:
        os.read(fd, NOTIFY_MAX_LINE)
        return True
    except BlockingIOError:
        return False
    except OSError:
        return False
    finally:
        try:
            os.set_blocking(fd, True)
        except OSError:
            pass


def _open_notify_channel() -> tuple[int, int]:
    """Открыть канал уведомления: ``(читающий, пишущий)`` для ребёнка.

    Пишущий конец отдаётся ребёнку в слот ``stdin`` и объявляется блоком
    как ``transport.notify_fd = 0``. Причина именно в этом, а не в
    ``pass_fds``: на Windows ``pass_fds`` не поддерживается, а унаследованный
    handle не становится номером дескриптора в CRT ребёнка — ``os.write(N)``
    там даёт ``EBADF`` (проверено на этой машине). Слоты 0/1/2 — единственные,
    что Windows реально наследует как дескриптор, а на http-ветке stdin
    ничего не несёт: протокола на нём нет. На POSIX тот же слот работает
    тем же способом, поэтому механизм один, а не ветка на ветку.
    """
    return os.pipe()


async def _terminate_child(process: Any) -> None:
    """Завершить дочерний процесс платформы при закрытии сессии.

    Под пределом: закрытие стека вызывается на каждом отказе, и ждать
    завершения процесса без предела опасно — иначе зависнет и сброс, и
    любой вызов, который к нему обратится.
    """
    if process.returncode is not None:
        return
    try:
        process.terminate()
        await asyncio.wait_for(process.wait(), timeout=RESET_TIMEOUT_SEC)
    except (ProcessLookupError, OSError):
        pass
    except (TimeoutError, Exception):  # noqa: BLE001 — закрытие не должно ронять вызов
        try:
            process.kill()
        except (ProcessLookupError, OSError):
            pass


def _confirm_process(reported_pid: int, child_pid: int) -> None:
    """Адрес сообщил именно тот процесс, которого агент запустил.

    Порт мог занять чужой процесс, поднятый до нас, и MCP-рукопожатие на
    таком сервере прошло бы успешно. Единственное доказательство, что
    сервер наш, — совпадение pid.
    """
    if reported_pid != child_pid:
        raise EnterpriseMcpUnavailable(
            f"фактический адрес сообщил процесс {reported_pid}, а агент запустил "
            f"{child_pid}: по этому адресу отвечает не та платформа, которую он "
            f"поднимал"
        )


def _confirm_identity(server_info: Any, expected_profile: str | None) -> None:
    """Сервер назвал свой контур, и он совпал с объявленным.

    Имя приходит в ``serverInfo`` MCP-рукопожатия и несёт контур
    (``enterprise-mcp`` для базы, ``enterprise-mcp:<контур>`` для
    профиля). Расхождение — отказ конфигурации, а не доступности: именно
    оно приводит к тому, что журнал тестового прогона попадает в боевые
    таблицы, и заметить это можно только по содержимому боевого журнала.
    """
    name = str(getattr(server_info, "name", "") or "")
    reported = name.partition(":")[2] or None
    if reported == expected_profile:
        return
    raise ConfigurationError(
        f"контур: агент объявил {expected_profile or 'prod (база)'}, а сервер по "
        f"объявленному адресу назвался {name!r} "
        f"({reported or 'без имени контура'}). Журнал писался бы не туда, куда "
        f"объявлено: проверьте --profile у обоих."
    )


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
        transport: str = TRANSPORT_STDIO,
        bind: str = "127.0.0.1",
        port: int = 0,
        profile: str = "",
    ) -> None:
        self._command = command
        self._args = list(args or [])
        # Смешанные разделители из резолва ${VAR} недопустимы для cwd
        # на других платформах — приводим к нативному виду.
        self._cwd = str(Path(cwd)) if cwd else None
        self._timeout = float(tool_timeout_sec or DEFAULT_TOOL_TIMEOUT_SEC)
        self._server_name = server_name
        # Транспорт и ЗАПРОС адреса, а не сам адрес: фактический порт
        # назначает ОС в момент бинда и сообщает его платформа в канале
        # уведомления. ``port == 0`` — «выдай свободный», и тогда
        # проверять нечего (проверка занятости — только закреплённый порт).
        self._transport = transport
        self._bind = bind
        self._port = int(port)
        # Контур, объявленный агентом (``--profile``). Пусто — база (prod).
        # Нужен для сверки личности сервера на http-ветке.
        self._profile = profile
        # Что платформа сказала о себе: pid и фактический адрес. На stdio
        # их нет — там протокол на stdout, и pid ребёнка клиенту не виден.
        self._platform_pid: int | None = None
        self._endpoint: str | None = None
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
        # Сколько раз вызов ушёл без request_id оборота и request_id был
        # доставлен самостоятельно. Это нормальный путь для фоновой службы
        # (поллер, зеркало, наблюдение), а не аномалия, поэтому он не должен
        # печататься на каждом вызове; счётчик и одноразовая строка дают
        # оператору и факт, и масштаб, не превращая терминал в поток.
        self._generated_request_ids = 0
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
            "transport": self._transport,
            # Запрошенный адрес и фактический — разные вещи: запрошенный
            # виден и до подъёма сессии, фактический приходит от платформы.
            "requested_port": self._port,
            "endpoint": self._endpoint,
            "platform_pid": self._platform_pid,
            # Объявленный путь виден и до подъёма сессии: баннер
            # запуска печатается по ``describe()``, и перенаправление
            # не должно выглядеть включившимся само по себе.
            **describe_redirect(self._stderr, declared=self._stderr_log),
        }

    def presence_line(self, operation_count: int) -> str:
        """Строка вердикта о платформе: операции, контур, pid, транспорт.

        Один формат на оба входа в систему (gateway и CLI). Вторая копия
        этой строки разъехалась бы при первой же правке одного из
        входов, а расхождение печалей в двух терминалах и не найти.
        """
        contour = self._profile or "prod (база)"
        pid = self._platform_pid if self._platform_pid else "неизвестен"
        return (
            f"enterprise-mcp: {operation_count} операций, процесс поднят "
            f"(контур={contour}, pid={pid}, транспорт={self._transport}"
            + (f", адрес={self._endpoint}" if self._endpoint else "")
            + ")"
        )

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
        turn = read_turn_context()
        if turn is None:
            return None
        session_id = turn.session_key
        user_id = turn.user_id
        if not session_id or not user_id:
            return None

        # ``request_id`` — привязка живого вопроса, и владелец у неё один:
        # журнал отвечает из того же хранилища, что и подписчик событий.
        # Отказ хранилища - не повод отказывать в вызове: идентификатор
        # оборота остаётся пустым, и вызов уходит без связи с
        # agent_question_runs.
        request_id = request_id_from(self._db_logging_service, session_id)

        return CallIdentity(
            session_id=session_id,
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
        эта сборка. Отсутствие связи с ``agent_question_runs`` выражается
        самим пустым значением: под такого ``request_id`` в журнале не
        найдётся ни одной строки, и это нормально.
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

        Подставленное значение НЕ ссылается на существующий оборот: в
        журнале под таким ``request_id`` не найдётся ни одной строки, и это
        нормально. Отличать его от оборота не нужно: в журнале идентификатор
        оборота не выдумывается вовсе, отсутствие значения и есть признак.
        """
        if identity is None:
            return None
        if not identity.request_id:
            generated = _new_request_id()
            self._generated_request_ids += 1
            # Служебный вызов и оборот — разные вещи, и подставленный
            # ``request_id`` означает для них разное.
            #
            # У служебного компонента оборота нет и быть не может: чистит
            # журнал, опрашивает очередь, зеркалит сессии. Связи с
            # ``agent_question_runs`` у такого вызова не будет НИКОГДА, и её
            # отсутствие — норма, а не признак. Раньше оба случая попадали в
            # одну строку «связь не появится», и служебный вызов на старте
            # читался как поломка: владелец видел её и искал, что сломалось.
            service_call = identity.user_id in SERVICE_IDENTITIES
            if service_call:
                if self._generated_request_ids == 1:
                    logger.info(
                        "служебный вызов {} без request_id оборота: оборота у "
                        "компонента нет и не бывает, request_id доставляется "
                        "самостоятельный. Это норма, а не признак потери",
                        identity.user_id,
                    )
                logger.debug(
                    "служебный вызов %s: подставлен самостоятельный "
                    "request_id=%s (всего %d за процесс)",
                    identity.user_id, generated, self._generated_request_ids,
                )
            else:
                # Подробная строка — на уровень ``trace``: за цикл опроса её
                # порождают десятки вызовов, и на уровне ``turn`` она забивала
                # терминал. Один раз за процесс на уровне ``info`` остаётся:
                # сам факт должен быть виден, иначе «связь с
                # agent_question_runs не появится» обнаружится post-factum, по
                # отсутствующим строкам итогов.
                if self._generated_request_ids == 1:
                    logger.info(
                        "вызов оборота без request_id: request_id доставляется "
                        "самостоятельно, связь с agent_question_runs не "
                        "появится. Дальше — счётчиком, подробности на уровне trace"
                    )
                logger.debug(
                    "вызов оборота без request_id: подставлен самостоятельный "
                    "request_id=%s (всего %d за процесс)",
                    generated,
                    self._generated_request_ids,
                )
            identity = identity.with_request_id(generated)
        return identity.as_meta()

    def generated_request_ids(self) -> int:
        """Сколько раз за процесс вызов ушёл без ``request_id`` оборота.

        Отдельный метод, а не поле в ``health()``: здоровье процесса и счётчик
        подставленных идентичностей отвечают на разные вопросы, и смешивать их
        в одном снимке — значит читатель не поймёт, что означает ноль в поле
        «вроде не сломалось».
        """
        return self._generated_request_ids

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
            if self._transport == TRANSPORT_HTTP:
                return await self._ensure_http_session()
            return await self._ensure_stdio_session()

    async def _ensure_stdio_session(self) -> Any:
        """Подъём по stdio — путь отката, поведение прежнее."""
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

    def _errlog_fd(self) -> Any:
        """Номер дескриптора файла-приёмника stderr, если он объявлен.

        На http-ветке ``errlog`` у ``stdio_client`` неприменим: процессом
        занимается агент, и stderr он открывает ребёнку сам. Приёмник —
        тот же объявленный файл и тот же ``open_redirect``, поэтому
        перенаправление дописывается в него же при переподъёме, а окно
        зрителя открывается как прежде.
        """
        redirect = self._errlog()
        return redirect.fileno() if redirect is not None else None

    async def _ensure_http_session(self) -> Any:
        """Подъём по streamable-http к дочернему процессу платформы.

        Порядок не переставляется: сначала занятость закреплённого порта
        (до запуска процесса вообще), потом канал уведомления, потом
        ровно одно уведомление, потом сверка pid, потом сессия и сверка
        контура. Каждая сверка обязана случиться **до** того, как
        подключение начнёт считаться состоявшимся.
        """
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        self._stack = AsyncExitStack()
        try:
            # Закреплённый порт занят — не поднимаем процесс вовсе.
            check_platform_port(self._bind, self._port)
            read_fd, write_fd = _open_notify_channel()
            try:
                process = await asyncio.create_subprocess_exec(
                    self._command,
                    *self._args,
                    cwd=self._cwd,
                    env=self._child_env(),
                    stdin=write_fd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=self._errlog_fd(),
                )
            finally:
                os.close(write_fd)
            self._stack.push_async_callback(_terminate_child, process)
            try:
                address = await _read_notification(read_fd, child_pid=process.pid)
            finally:
                os.close(read_fd)
            _confirm_process(address["pid"], process.pid)
            self._platform_pid = address["pid"]
            self._endpoint = f"{address['host']}:{address['port']}"
            if self._port and address["port"] != self._port:
                raise EnterpriseMcpUnavailable(
                    f"платформа заняла порт {address['port']}, а агент просил "
                    f"{self._port}: адрес не тот, который объявлен"
                )
            write_marker(
                self._stderr,
                f"transport=http pid={process.pid} адрес={self._endpoint}",
            )
            read, write, _ = await self._stack.enter_async_context(
                streamablehttp_client(f"http://{self._endpoint}/mcp")
            )
            session = await self._stack.enter_async_context(ClientSession(read, write))
            result = await session.initialize()
            # Внутри try намеренно: сверка обязана закрыть стек, иначе отказ
            # по чужому контуру оставил бы процесс платформы живым. Тип
            # сохраняется — ``ConfigurationError`` перечислен ниже.
            _confirm_identity(getattr(result, "serverInfo", None), self._profile or None)
        except BaseException as exc:
            # Закрытие http-клиента само ходит в сеть, и на упавшем
            # подключении падает второй ошибкой. Подавленная, она затерела
            # бы настоящую причину: оператор увидел бы ConnectError вместо
            # «платформа не поднялась».
            with contextlib.suppress(Exception):
                await self._stack.aclose()
            self._stack = None
            self._platform_pid = None
            self._endpoint = None
            # Отказы с названным решением (порт занят, контур чужой)
            # доходят до вызывающего как есть: переворачивать их в
            # «сервер не поднялся» значило бы потерять и решение, и его
            # тип — выходной код входа в систему.
            if isinstance(exc, (EnterpriseMcpUnavailable, ConfigurationError)):
                raise
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
        # Адрес и pid прошлого подъёма недействительны: переподъём
        # обязан заново прочитать уведомление (см. ``_ensure_http_session``).
        self._platform_pid = None
        self._endpoint = None
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


#: Путь к порогу журнала в конфигурации агента живёт в
#: ``lib/services/agent_settings.py`` — там же, где он наполняет блок
#: настроек. Здесь он переэкспортирован, потому что им пользуются тесты
#: (``tests/test_mcp_platform_declaration.py`` сверяет его с
#: ``config.JOURNAL_MIN_LEVEL_PATH``) и потому что это объявление значения,
#: которое едет платформе.
from lib.services.agent_settings import (  # noqa: E402  (после доктрины)
    JOURNAL_MIN_LEVEL_PATH,
    publish_agent_settings,
)


def _agent_settings_argv(settings: Any, section: Any) -> list[str]:
    """Аргументы запуска с блоком настроек агента — или ничего.

    Корневой каталог платформы уже объявлен в
    ``config.json → gateway.agent.enterprise_mcp.cwd``; объявление пути к
    блоку лежит рядом, в ``platform.json``. Без ``cwd`` блок некуда положить
    и некуда положить его объявление, поэтому аргументов не будет — с записью
    в журнал, а не молча.

    ``cwd`` проверяется на строку: конфигурация прошла схему, где он объявлен
    строкой, и не-строка здесь означает двойник в тесте, а не конфигурацию.
    Приводить такой объект к строке нельзя — получился бы путь в никуда и
    отказ с требованием починить чужой тест.
    """
    cwd = section.get("cwd")
    if not isinstance(cwd, str) or not cwd.strip():
        logger.warning(
            "enterprise-mcp: не объявлен cwd — блок настроек агента не передан"
        )
        return []
    block = publish_agent_settings(
        settings, platform_json=Path(cwd) / "platform.json"
    )
    return block.argv() if block is not None else []


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

    Значения агента уезжают **блоком** (``--agent-settings-file``, см.
    ``lib/services/agent_settings.py``), а не флагом на каждое значение: в
    ``argv`` видны все, а блок сделан с расчётом на секрет. Блок собирается
    из ``config.json`` агента и ОДИН раз при старте. В ``params._meta`` каждого
    вызова он не едет: значение, перечитываемое на каждый вызов, способно
    разъехаться между вызовами одного оборота, а вызывающая сторона получила
    бы право решать, сколько логировать платформа, — а это её собственные
    события (``tool.*``, ``quality.check``), и подменять им политику
    вызывающего нельзя.
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
    # Настройки агента — блоком, одним аргументом. Флаг на каждое значение
    # означал бы, что добавление настройки — правка кода запуска, и держал бы
    # второй путь к значению, который уже снят с этого места.
    args += _agent_settings_argv(settings, section)
    # stderr_log — НЕ настройка платформы и в argv не едет: это
    # вопрос транспорта на стороне агента (куда девать stderr процесса,
    # который агент и поднимает), а не объявление платформы. Поэтому
    # его чтение не ломает контракт «агент не объявляет ничего для
    # платформы», и страж границы остаётся зелёным.
    # Транспорт и запрошенный адрес — из того же раздела, что и команда.
    # Ключи вложенные (``enterprise_mcp.transport.*``): раздел закрыт
    # ``AGENT_SECTIONS``, и объявлять их в корне config.json нельзя —
    # схема nanobot отвергает неизвестный ключ верхнего уровня.
    transport_section = section.get("transport")
    if not isinstance(transport_section, dict):
        transport_section = {}
    transport = str(transport_section.get("mode") or "").strip() or TRANSPORT_STDIO
    if transport not in TRANSPORTS:
        raise ConfigurationError(
            f"gateway.agent.enterprise_mcp.transport.mode={transport!r} не "
            f"объявлен. Допустимы: {', '.join(TRANSPORTS)}."
        )
    bind = loopback_bind(
        str(transport_section.get("bind") or "").strip() or "127.0.0.1"
    )
    port = transport_section.get("port")
    try:
        port = int(port) if port not in (None, "") else 0
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(
            f"gateway.agent.enterprise_mcp.transport.port={port!r} — не целое. "
            f"0 значит «выдай свободный»."
        ) from exc
    stderr_log = str(section.get("stderr_log") or "").strip() or None
    return EnterpriseMcpClient(
        command=str(command),
        args=args,
        cwd=section.get("cwd"),
        stderr_log=stderr_log,
        transport=transport,
        bind=bind,
        port=port,
        # Контур уезжает в блок личности, а не в argv: имя сервера в
        # рукопожатии обязано совпасть с тем, что агент объявил.
        profile=profile if profile and profile != "prod" else "",
        tool_timeout_sec=float(
            section.get("tool_timeout_sec") or DEFAULT_TOOL_TIMEOUT_SEC
        ),
        db_logging_service=db_logging_service,
    )
