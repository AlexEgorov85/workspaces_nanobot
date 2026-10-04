"""Ветка ``streamable-http``: адрес назначает биндящий процесс, объявляет агент.

Модуль существует, чтобы у адреса был **один** разбор и **одна** проверка.
Всё, что этот файл знает про порт, он узнаёт из блока настроек агента
(``runtime/platform-settings``) и больше ниоткуда: ни из ``argv``, ни из
``os.environ``, ни из ``platform.json``. Порт не «берётся по умолчанию» —
запрошенное значение либо пришло блоком, либо процесс не поднимается.

Три вещи, которые этот файл делает и которые нельзя сделать в другом месте:

1. **Отказывает на не-loopback адресе** (:func:`loopback_host`). Случай
   ``bind: 0.0.0.0`` — это не «работает на всякий случай», а публикация
   внутреннего сервера наружу; адрес слушает loopback, и это написано в
   требовании, а не выводится из молчаливого дефолта.
2. **Отказывает на занятом закреплённом порте** (:func:`bind_listener`) до того,
   как процесс начнёт обслуживать. Порт ``0`` — «выдай свободный», и
   конфликт при нём структурно невозможен: назначает его ОС в момент бинда.
3. **Сообщает фактический адрес в унаследованный дескриптор**
   (:func:`notify_address`), а не в stdout. На stdout держится страж
   ``mcp-platform/tests/test_journal_contract_visibility.py::TestServerNeverPrintsToStdout``,
   и добавлять сюда ``servers/enterprise/server.py`` нельзя: ослабился бы
   страж целиком. Дескриптор объявляет агент — он же его и открывает ребёнку.
"""

from __future__ import annotations

import json
import logging
import os
import socket
from dataclasses import dataclass
from typing import Any

from libs.enterprise_common.errors import InfrastructureError
from libs.enterprise_common.settings import Settings, agent_settings_dictionary

#: Настройки реестра, которыми задан транспорт. Читаются только они и только
#: через :meth:`Settings.get` — значение блока имеет одного читателя, и он не
#: этот модуль, а реестр.
TRANSPORT_SETTING = "ENTERPRISE_TRANSPORT_MODE"
BIND_SETTING = "ENTERPRISE_TRANSPORT_BIND"
PORT_SETTING = "ENTERPRISE_TRANSPORT_PORT"
NOTIFY_FD_SETTING = "ENTERPRISE_TRANSPORT_NOTIFY_FD"

#: Имена ключей блока — только для сообщений об отказе: оператор видит файл
#: блока, а не имена настроек реестра.
TRANSPORT_KEY = "transport.mode"
BIND_KEY = "transport.bind"
PORT_KEY = "transport.port"
NOTIFY_FD_KEY = "transport.notify_fd"

MODE_STDIO = "stdio"
MODE_HTTP = "http"
MODES = (MODE_STDIO, MODE_HTTP)

#: Единственные адреса, на которых сервер вправе слушать. ``localhost``
#: разворачивается в ``127.0.0.1``, чтобы имя из конфигурации не доезжало до
#: резолвера ОС: результат резолва — это уже чужое решение о нашем адресе.
#:
#: IPv6-адрес ``::1`` здесь **отвергается наравне** с ``0.0.0.0``: сокет
#: слушателя создаётся в ``AF_INET``, и принять ``::1`` значило бы объявить
#: адрес, который не поднимется. Список узкий не по незнанию, а по
#: соответствию тому, что сервер действительно создаёт.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})
LOOPBACK_BIND = "127.0.0.1"

#: Путь MCP внутри приложения. Объявлен здесь, потому что агент ходит по тому
#: же пути, и второй путь в коде агента был бы вторым ответом на один вопрос.
MCP_PATH = "/mcp"

#: Размер очереди accept. Значение из ``uvicorn``; объявлено явно, потому что
#: ``listen()`` с заданным бэклогом и без — два разных поведения, а молчаливый
#: выбор дефолта читается как «так и задумано».
LISTEN_BACKLOG = 128

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TransportRequest:
    """Что агент попросил: не адрес, а запрос.

    Attributes:
        mode: ``stdio`` — путь отката, ``http`` — обслуживание по сети.
        bind: запрошенный адрес; loopback или отказ.
        port: ``0`` — «выдай свободный», число — «занми вот этот».
        notify_fd: дескриптор для строки с фактическим адресом, ``None`` —
            сообщать некуда, и в режиме ``http`` это отказ.
    """

    mode: str
    bind: str
    port: int
    notify_fd: int | None


def requested_transport(settings: Settings) -> TransportRequest:
    """Разобрать запрос транспорта из блока настроек агента.

    Args:
        settings: разобранные настройки процесса, включая принятый блок.

    Returns:
        Запрос транспорта. ``stdio`` — путь отката, он же значение при
        отсутствии ключа: блок не доехал или агент его не прислал.

    Raises:
        InfrastructureError: настройка транспорта не объявлена в реестре,
            режим неизвестен, порт вне диапазона, или при ``http`` задан порт
            и не задан дескриптор уведомления. Молчаливая подстановка на любом
            из этих случаев означала бы подъём на адресе, которого никто не
            просил.
    """
    mode = _declared(settings, TRANSPORT_SETTING) or MODE_STDIO
    if mode not in MODES:
        raise InfrastructureError(
            f"{TRANSPORT_KEY}: транспорт {mode!r} не объявлен. Допустимы: "
            f"{', '.join(MODES)}."
        )
    bind = _declared(settings, BIND_SETTING) or LOOPBACK_BIND
    loopback_host(bind)
    port = _port(settings)
    notify_fd = _notify_fd(settings)
    if mode == MODE_STDIO:
        if port:
            raise InfrastructureError(
                f"{PORT_KEY}: порт {port} запрошен при транспорте {MODE_STDIO!r}. "
                f"Порт имеет смысл только при {MODE_HTTP!r}: на stdio адрес "
                f"выбирает клиент. Либо уберите ключ, либо переключите транспорт — "
                f"молчаливо заказанный порт был бы адресом, который никто не "
                f"слушает."
            )
        notify_fd = None
    elif notify_fd is None:
        raise InfrastructureError(
            f"{NOTIFY_FD_KEY}: транспорт {MODE_HTTP!r} выбран, а дескриптор "
            f"уведомления не задан. Фактический адрес назначает ОС в момент "
            f"бинда, и сообщить его больше нечем: stdout платформы занят "
            f"JSON-RPC, stderr — журналом. Агент объявляет дескриптор и открывает "
            f"его ребёнку при подъёме."
        )
    return TransportRequest(mode=mode, bind=bind, port=port, notify_fd=notify_fd)


def loopback_host(bind: str) -> str:
    """Проверить, что адрес слушания — loopback, и вернуть его.

    Args:
        bind: адрес из блока настроек.

    Returns:
        Адрес, пригодный для :func:`socket.socket.bind`.

    Raises:
        InfrastructureError: адрес не loopback, включая ``0.0.0.0``, ``::`` и
            ``::1``. Такой процесс был бы виден за пределами машины, а
            объявлял его агент одной строкой в конфигурации. ``::1``
            отвергается отдельно от ``0.0.0.0`` не по опасности, а по
            неработоспособности: сокет слушателя создаётся в ``AF_INET``.
    """
    host = bind.strip()
    if host not in LOOPBACK_HOSTS:
        raise InfrastructureError(
            f"{BIND_KEY}: адрес {bind!r} отвергнут. Сервер слушает только "
            f"loopback {LOOPBACK_BIND}, и сокет у него в AF_INET: платформа "
            f"держит пул PostgreSQL и открытый снимок, а выставить их в сеть — "
            f"не «работать на всякий случай». Адрес ::1 отвергается по той же "
            f"причине: сервер не создаёт сокет IPv6."
        )
    return LOOPBACK_BIND if host == "localhost" else host


def bind_listener(bind: str, port: int) -> socket.socket:
    """Занять адрес и вернуть слушающий сокет.

    Сокет создаётся здесь, а не внутри ``uvicorn``, по двум причинам. Первая:
    фактический порт известен сразу после ``bind()``, и агент может получить
    адрес до того, как сервер начнёт обслуживать. Вторая: отказ на занятом
    порте случается здесь и называет адрес, а не всплывает позже изнутри
    event loop.

    ``SO_REUSEADDR`` здесь намеренно не ставится. На Windows он разрешает
    занять порт, уже занятый другим сокетом, и требование «занятый закреплённый
    порт — отказ запуска» стало бы ложью именно на той платформе, где проверка
    занятости и делается.

    Args:
        bind: адрес слушания; проверен :func:`loopback_host`.
        port: ``0`` — свободный порт от ОС, число — закреплённый.

    Returns:
        Сокет в режиме ``listen``; фактический порт — ``getsockname()``.

    Raises:
        InfrastructureError: адрес занят или недоступен.
    """
    host = loopback_host(bind)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        sock.listen(LISTEN_BACKLOG)
    except OSError as exc:
        sock.close()
        raise InfrastructureError(
            f"порт {port} на {host} занят или недоступен: {exc.strerror or exc}. "
            f"Порт назначает ОС в момент бинда, поэтому занять закреплённый "
            f"порт должен тот, кто его слушает."
        ) from exc
    sock.setblocking(False)
    return sock


def notify_address(fd: int, *, host: str, port: int) -> None:
    """Сообщить фактический адрес в унаследованный дескриптор.

    Одна строка JSON с ``\\n`` в конце и ничего больше: читатель — агент, и
    любой посторонний вывод в этом канале сдвинул бы разбор на неверную строку.

    Args:
        fd: дескриптор, объявленный агентом в блоке настроек.
        host: адрес слушания.
        port: фактический порт.

    Raises:
        InfrastructureError: дескриптор не открыт агентом. Тогда адрес
            узнать нечем, и подъём молчащим был бы хуже отказа.
    """
    payload = json.dumps(
        {"host": host, "port": port, "pid": os.getpid()}, sort_keys=True
    )
    try:
        os.write(fd, payload.encode("utf-8") + b"\n")
    except OSError as exc:
        raise InfrastructureError(
            f"{NOTIFY_FD_KEY}: в дескриптор {fd} не записать адрес ({exc}). "
            f"Дескриптор объявляет и открывает агент; если он не открыт, "
            f"фактический порт никто не узнает."
        ) from exc


def _declared(settings: Settings, name: str) -> str:
    """Значение настройки блока строкой; не объявлена — внятный отказ.

    Отсутствие значения — пустая строка, и это два разных состояния, сведённых
    здесь намеренно: ключа нет вовсе, и ключ есть, но пуст. Реестр отдаёт в
    обоих случаях пустое значение по типу, а для ``OPTIONAL`` — ещё и сам
    маркер ``OPTIONAL``; пустой ``transport.port`` значит «порт не запрошен»,
    а не «порт такой-то, но с буквой o вместо нуля».

    :meth:`Settings.get` отвечает «не объявлена в реестре» настройку,
    объявленную в спецификации, но не занесённую в реестр. Для порта это
    означало бы одно из двух: подъём на молчаливом дефолте или подъём на
    чужом адресе. Оба хуже отказа, поэтому отказ переписывается так, чтобы в
    нём было видно, что именно объявлять.
    """
    from libs.enterprise_common.settings import OPTIONAL

    try:
        value = settings.get(name)
    except InfrastructureError as exc:
        accepted = ", ".join(sorted(agent_settings_dictionary())) or "(пусто)"
        raise InfrastructureError(
            f"{name}: настройка не объявлена в реестре платформы. Блок "
            f"настроек — единственный канал доставки адреса транспорта "
            f"(runtime/platform-settings), а молчаливый дефолт означал бы "
            f"подъём на адресе, которого никто не просил. Принимаются ключи: "
            f"{accepted}."
        ) from exc
    if value is None or value is OPTIONAL:
        return ""
    return str(value).strip()


def _port(settings: Settings) -> int:
    """Запрошенный порт; отсутствие ключа — ``0`` («выдай свободный»).

    Отсутствие ключа здесь — объявленное значение по умолчанию, а не вычисление
    кодом: оно названо в требовании «Ключ ``port`` необязателен при ``http``»
    и в самом реестре (``OPTIONAL``).
    """
    raw = _declared(settings, PORT_SETTING)
    if not raw:
        return 0
    try:
        port = int(raw)
    except ValueError as exc:
        raise InfrastructureError(
            f"{PORT_KEY}: {raw!r} — не число. 0 значит «выдай свободный», "
            f"число — «занми вот этот»."
        ) from exc
    if not 0 <= port <= 65535:
        raise InfrastructureError(
            f"{PORT_KEY}: {port} вне диапазона 0-65535."
        )
    return port


def _notify_fd(settings: Settings) -> int | None:
    """Дескриптор уведомления; отсутствие ключа — ``None``."""
    raw = _declared(settings, NOTIFY_FD_SETTING)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise InfrastructureError(
            f"{NOTIFY_FD_KEY}: {raw!r} — не целое. Это номер дескриптора, "
            f"который агент открывает ребёнку при подъёме."
        ) from exc


def _security_settings(host: str, port: int) -> Any:
    """Защита от DNS-rebinding на конкретный loopback-адрес.

    Сервер и так слушает только loopback, но ``Host`` в заголовке приходит от
    клиента, и без проверки любой процесс на машине мог бы обратиться к нему
    под именем чужого хоста. Разрешён ровно тот адрес, на котором сервер
    слушает, — в том числе при порте, выданном ОС.
    """
    from mcp.server.transport_security import TransportSecuritySettings

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[f"{host}:{port}", f"{host}:*"],
        allowed_origins=[],
    )


async def serve_http(server: Any, request: TransportRequest) -> None:
    """Обслуживать MCP по streamable-http на запрошенном адресе.

    Args:
        server: собранный MCP-сервер платформы.
        request: запрос из блока настроек.

    Raises:
        InfrastructureError: адрес занят либо в унаследованный дескриптор не
            записать фактический адрес. Второе проверяется **до** бинда:
            занять порт и не суметь сообщить адрес — значит поднять процесс,
            которым никто не сможет воспользоваться.
    """
    import uvicorn
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    host = loopback_host(request.bind)
    if request.notify_fd is None:
        raise InfrastructureError(
            f"{NOTIFY_FD_KEY}: транспорт {MODE_HTTP!r} без дескриптора "
            f"уведомления не поднимается. Проверка до бинда: занять порт и не "
            f"суметь сообщить адрес — значит оставить за процессом адрес, "
            f"который никто не знает."
        )
    sock = bind_listener(host, request.port)
    port = int(sock.getsockname()[1])
    logger.info(
        "транспорт: streamable-http, адрес %s:%s, канал уведомления: дескриптор %s",
        host,
        port,
        request.notify_fd,
    )
    # ``stateless`` и ``json_response`` — по одной причине: у процесса один
    # потребитель, а состояние сессии MCP на сервере всё равно живёт в этом же
    # процессе, в его пуле и индексах. Сессия в HTTP была бы вторым местом,
    # где оно могло бы появиться.
    manager = StreamableHTTPSessionManager(
        app=server,
        event_store=None,
        json_response=True,
        stateless=True,
        security_settings=_security_settings(host, port),
    )

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await manager.handle_request(scope, receive, send)

    # ``lifespan="off"`` обязателен: жизненный цикл сессионного менеджера
    # открывает ``manager.run()``, а uvicorn со своим lifespan отправил бы в
    # ``handle_request`` scope, который тот не обслуживает.
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        lifespan="off",
        access_log=False,
        log_config=None,
    )
    async with manager.run():
        notify_address(request.notify_fd, host=host, port=port)
        await uvicorn.Server(config).serve(sockets=[sock])
