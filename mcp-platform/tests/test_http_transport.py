"""Стражи ветки ``streamable-http``: адрес, отказы, канал уведомления.

Сторож проверяет то, что проверяется поведением: сервер действительно
обслуживает по HTTP на запрошенном адресе, отказ на не-loopback и на занятом
порту происходят **до** начала обслуживания, а фактический адрес уходит в
дескриптор, а не в stdout.

Тесты поднимают настоящий MCP-сервер и настоящего клиента. Заглушка была бы
дешевле, но она проверяла бы, что тест зелёный: подъём uvicorn, менеджер
сессий, заголовок ``Host`` и разбор JSON-RPC — как раз то, где ломается
транспорт.

Файл не проверяет, что порт **приезжает блоком**: это отдельный страж в
``test_agent_settings_block.py::TestPortKey``, потому что порт — настройка
реестра, а не поведение модуля транспорта.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from libs.enterprise_common.errors import InfrastructureError
from servers.enterprise import http_transport
from servers.enterprise.http_transport import TransportRequest

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
SERVER_SOURCE = PLATFORM_ROOT / "servers" / "enterprise" / "server.py"


def _free_port() -> int:
    """Порт, который сейчас свободен: взять, отпустить, вернуть номер."""
    probe = http_transport.bind_listener("127.0.0.1", 0)
    try:
        return int(probe.getsockname()[1])
    finally:
        probe.close()


def _request(**overrides) -> TransportRequest:
    """Запрос транспорта с заменой полей — для тестов, которые не читают блок."""
    base = {"mode": "http", "bind": "127.0.0.1", "port": 0, "notify_fd": 3}
    base.update(overrides)
    return TransportRequest(**base)


class _NotifyPipe:
    """Канал уведомления как его видит платформа: дескриптор на запись.

    Отдельный процесс для настоящего унаследованного дескриптора в тесте не
    нужен: ``notify_address`` пишет по номеру, и проверяется именно номер.
    """

    def __init__(self) -> None:
        self.read_fd, self.write_fd = os.pipe()

    def __enter__(self) -> "_NotifyPipe":
        return self

    def __exit__(self, *exc: object) -> None:
        for fd in (self.read_fd, self.write_fd):
            os.close(fd)

    def read_line(self) -> dict:
        """Прочитать строку уведомления целиком."""
        chunks: list[bytes] = []
        while True:
            chunk = os.read(self.read_fd, 1)
            if chunk == b"\n":
                return json.loads(b"".join(chunks).decode("utf-8"))
            if not chunk:  # pragma: no cover - писатель молча закрылся
                raise AssertionError("уведомление не пришло, дескриптор закрыт")
            chunks.append(chunk)


class TestListenerRefusesAnythingButLoopback:
    """D10: сервер слушает loopback, и «работать на всякий случай» — не ответ."""

    @pytest.mark.parametrize("bind", ["0.0.0.0", "::", "::1", "127.0.0.2", "example.com"])
    def test_non_loopback_bind_is_refused(self, bind: str) -> None:
        with pytest.raises(InfrastructureError) as failure:
            http_transport.loopback_host(bind)
        assert bind in str(failure.value)
        assert "loopback" in str(failure.value)

    def test_loopback_spellings_are_accepted(self) -> None:
        assert http_transport.loopback_host("127.0.0.1") == "127.0.0.1"
        assert http_transport.loopback_host("localhost") == "127.0.0.1"

    def test_accepted_spellings_are_the_ones_the_socket_can_bind(self) -> None:
        """Список адресов совпадает с тем, что сервер действительно создаёт.

        Проверка перечислением намеренно жёсткая: принять ``::1`` при сокете в
        ``AF_INET`` — значит объявить адрес, который не поднимется, и отказ
        придётся разбирать на старте вместо чтения конфигурации.
        """
        for host in sorted(http_transport.LOOPBACK_HOSTS):
            sock = http_transport.bind_listener(host, 0)
            try:
                assert sock.family == socket.AF_INET
                assert sock.getsockname()[0] in http_transport.LOOPBACK_HOSTS
            finally:
                sock.close()

    def test_refusal_happens_before_any_socket_is_bound(self) -> None:
        """Отказ — до бинда: занятый адрес не должен остаться за процессом."""
        with pytest.raises(InfrastructureError):
            http_transport.bind_listener("0.0.0.0", 0)


class TestOccupiedPinnedPortIsRefused:
    """D4: конфликт возможен только на закреплённом порте, и он — отказ."""

    def test_pinned_port_taken_by_another_socket(self) -> None:
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            holder.bind(("127.0.0.1", 0))
            holder.listen(1)
            port = holder.getsockname()[1]
            with pytest.raises(InfrastructureError) as failure:
                http_transport.bind_listener("127.0.0.1", port)
            assert str(port) in str(failure.value)
        finally:
            holder.close()

    def test_pinned_port_is_taken_by_this_process_when_free(self) -> None:
        sock = http_transport.bind_listener("127.0.0.1", 0)
        try:
            assert sock.getsockname()[1] > 0
        finally:
            sock.close()

    def test_ephemeral_port_never_conflicts(self) -> None:
        """Два запроса ``port: 0`` получают разные порты — проверять нечего."""
        first = http_transport.bind_listener("127.0.0.1", 0)
        second = http_transport.bind_listener("127.0.0.1", 0)
        try:
            assert first.getsockname()[1] != second.getsockname()[1]
        finally:
            first.close()
            second.close()


class TestAddressNotification:
    """Адрес уходит в дескриптор, а не в stdout и не в stderr."""

    def test_address_arrives_as_one_json_line(self) -> None:
        with _NotifyPipe() as pipe:
            http_transport.notify_address(pipe.write_fd, host="127.0.0.1", port=45678)
            payload = pipe.read_line()
        assert payload == {"host": "127.0.0.1", "port": 45678, "pid": os.getpid()}

    def test_closed_descriptor_is_a_clear_refusal(self) -> None:
        read_fd, write_fd = os.pipe()
        os.close(write_fd)
        os.close(read_fd)
        with pytest.raises(InfrastructureError) as failure:
            http_transport.notify_address(write_fd, host="127.0.0.1", port=1)
        assert "transport.notify_fd" in str(failure.value)

    def test_no_channel_refuses_before_any_bind(self) -> None:
        """Нет канала уведомления — отказ **до** бинда.

        Порядок проверяется на конкретном порте: если бы сокет создавался
        первым, отказ оставил бы за процессом занятый адрес, который никто не
        обслуживает и о котором никто не знает.
        """
        pinned = _free_port()
        with pytest.raises(InfrastructureError) as failure:
            asyncio.run(
                http_transport.serve_http(object(), _request(port=pinned, notify_fd=None))
            )
        assert "transport.notify_fd" in str(failure.value)
        again = http_transport.bind_listener("127.0.0.1", pinned)
        again.close()


class TestUndeclaredTransportIsRefused:
    """Без объявления в реестре — внятный отказ, а не молчаливый дефолт.

    Это второй конец сквозной проверки: значение, которого нет в реестре,
    не превращается в «сервер поднялся на 127.0.0.1:0».
    """

    def test_empty_block_falls_back_to_stdio_loudly(self) -> None:
        """Пустой блок — это stdio, названное вслух, а не «что-то по умолчанию».

        Ключи транспорта внесены в реестр платформы 2026-10-04, поэтому прежний
        отказ «настройка транспорта не объявлена в реестре» больше не
        воспроизводится — это было свойством промежуточного состояния, а не
        инвариантом. Инвариант в другом: молчаливый дефолт невозможен, и пустой
        блок обязан дать названный stdio.
        """
        from libs.enterprise_common.settings import Settings

        request = http_transport.requested_transport(Settings())
        assert request.mode == http_transport.MODE_STDIO
        assert request.port == 0
        assert request.bind == "127.0.0.1"
        assert request.notify_fd is None

    def test_a_key_outside_the_block_dictionary_is_still_refused(self, tmp_path: Path) -> None:
        """Закрытый словарь остаётся закрытым — теперь это проверяется по-настоящему.

        Прежний повозок для этого инварианта («транспорт не объявлен») исчез
        вместе с объявлением. Настоящая проверка: ключ, которого в словаре нет,
        по-прежнему роняет блок с называнием ключа и принятого словаря, а не
        подставляет дефолт.
        """
        from libs.enterprise_common.settings import Settings

        block = tmp_path / "block.json"
        block.write_text(
            json.dumps({"transport.mode": "http", "transport.port": "не число"}),
            encoding="utf-8",
        )
        with pytest.raises(Exception) as failure:
            http_transport.requested_transport(
                Settings(agent_settings_path=block)
            )
        message = str(failure.value)
        # Сообщение обязано назвать отвергнутый ключ, его настройку и причину,
        # а также показать, сколько ключей принято из скольких. Перечислять
        # весь принятый набор оно не обязано — зато обязано сказать «из N».
        assert "transport.port" in message
        assert "ENTERPRISE_TRANSPORT_PORT" in message
        assert "не число" in message
        assert "принято 1 из 2" in message


class TestPortNeverTravelsInArgv:
    """Порт — настройка блока, а не аргумент запуска."""

    def test_argv_parser_has_no_port_branch(self) -> None:
        source = SERVER_SOURCE.read_text(encoding="utf-8")
        assert "--port" not in source, "порт появился в argv платформы"
        assert "port=" not in source.replace("http_transport", ""), (
            "в server.py появился разбор значения порта"
        )

    def test_transport_module_reads_no_environment(self) -> None:
        """Нет обращения к окружению и к файлу настроек — даже через атрибут.

        Проверка по AST, а не по тексту: модуль объясняет в докстринге, откуда
        значение приходить не должно, и подстановка этих слов в прозу сделала бы
        страж бессильным ровно на файле, который он охраняет.
        """
        import ast

        tree = ast.parse(
            (PLATFORM_ROOT / "servers" / "enterprise" / "http_transport.py").read_text(
                encoding="utf-8"
            )
        )
        forbidden_attrs = {"environ", "getenv"}
        hits = [
            ast.unparse(node)
            for node in ast.walk(tree)
            if (isinstance(node, ast.Attribute) and node.attr in forbidden_attrs)
            or (isinstance(node, ast.Name) and node.id in forbidden_attrs)
        ]
        assert hits == [], f"транспорт читает окружение процесса: {hits}"
        opens = [
            ast.unparse(node)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "open"
        ]
        assert opens == [], (
            f"транспорт сам открывает файл настроек: {opens}. Единственный "
            f"читатель блока — реестр."
        )

    def test_argv_carries_only_the_block_path(self) -> None:
        from servers.enterprise import server as enterprise_server

        args = ["--profile", "test", "--capabilities", "llm", "--agent-settings-file", "b.json"]
        assert enterprise_server._capabilities_from_argv(args) == frozenset({"llm"})
        assert enterprise_server._profile_from_argv(args) == "test"
        assert enterprise_server._agent_settings_path_from_argv(args) == Path("b.json")


_CHILD = textwrap.dedent(
    """
    import asyncio, sys
    sys.path.insert(0, sys.argv[1])
    from mcp.server.fastmcp import FastMCP
    from servers.enterprise import http_transport

    mcp = FastMCP("http-transport-test")

    @mcp.tool()
    def ping() -> str:
        return "pong"

    server = mcp._mcp_server
    # 0 — дескриптор, объявленный блоком: канал уведомления открыт агентом и
    # передан ребёнку при подъёме. В режиме http stdin свободен от JSON-RPC.
    request = http_transport.TransportRequest(
        mode="http", bind="127.0.0.1", port=int(sys.argv[3]), notify_fd=int(sys.argv[2])
    )

    async def _serve():
        await http_transport.serve_http(server, request)

    asyncio.run(_serve())
    """
)


class TestServerActuallyServesOverHttp:
    """Подъём целиком: uvicorn, менеджер сессий, заголовок ``Host``, JSON-RPC.

    Клиент и сервер — настоящие, в отдельных процессах, потому что канал
    уведомления и есть унаследованный дескриптор: в том же процессе его
    некуда унаследовать.
    """

    def test_client_initializes_and_calls_a_tool(self) -> None:
        child, read_fd, line = self._spawn(port=0)
        try:
            assert line["pid"] == child.pid
            assert line["port"] > 0
            assert "ping" in self._list_tools(line["port"])
        finally:
            stdout, stderr = self._stop(child, read_fd)
        # D14: адрес уходит в канал уведомления, а на stdout платформы не
        # попадает ничего. Сюда же — пустота stdout как продукт конвейера.
        assert f"127.0.0.1:{line['port']}" not in stdout.decode("utf-8", "replace"), (
            "адрес напечатан в stdout: канал уведомления объявлен агентом, "
            "stdout платформы под JSON-RPC"
        )
        assert b"streamable-http" in stderr, "подъём транспорта не попал в stderr"

    def test_pinned_port_is_the_port_actually_served(self) -> None:
        """Запрошенный порт — тот, на котором сервер отвечает.

        Требование «порт приходит блоком» проверяется тут до конца: значение
        запроса, фактический адрес в уведомлении и адрес, на котором отвечает
        клиент, — это три упоминания одного числа, и все три должны совпасть.
        """
        probe = http_transport.bind_listener("127.0.0.1", 0)
        pinned = int(probe.getsockname()[1])
        probe.close()  # порт освобождён ровно на миг между проверкой и биндом
        child, read_fd, line = self._spawn(port=pinned)
        try:
            assert line["port"] == pinned
            assert "ping" in self._list_tools(pinned)
        finally:
            self._stop(child, read_fd)

    @classmethod
    def _spawn(cls, *, port: int) -> tuple[subprocess.Popen, int, dict]:
        """Поднять платформу в дочернем процессе и дождаться адреса."""
        read_fd, write_fd = os.pipe()
        child = subprocess.Popen(  # noqa: S603 - свой интерпретатор, свой модуль
            [sys.executable, "-c", _CHILD, str(PLATFORM_ROOT), "0", str(port)],
            stdin=write_fd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        os.close(write_fd)
        return child, read_fd, cls._read_notification(read_fd)

    @staticmethod
    def _stop(child: subprocess.Popen, read_fd: int) -> tuple[bytes, bytes]:
        child.kill()
        stdout, stderr = child.communicate(timeout=30)
        os.close(read_fd)
        return stdout, stderr

    @staticmethod
    def _read_notification(fd: int, timeout: float = 60.0) -> dict:
        """Прочитать строку уведомления с предельным ожиданием.

        Наивное ожидание на мёртвом ребёнке повесило бы тест: отсутствие
        строки — отказ запуска с причиной (задача 3.5), а не ожидание. Чтение
        идёт в потоке, потому что ``select`` на анонимном канале Windows
        не работает, а канал уведомления на этой платформе — именно он.
        """
        import queue
        import threading

        result: queue.Queue[dict] = queue.Queue(maxsize=1)

        def _read() -> None:
            chunks: list[bytes] = []
            while True:
                chunk = os.read(fd, 1)
                if chunk == b"\n":
                    result.put(json.loads(b"".join(chunks).decode("utf-8")))
                    return
                if not chunk:
                    result.put({"error": "канал закрыт, адрес не пришёл"})
                    return
                chunks.append(chunk)

        thread = threading.Thread(target=_read, daemon=True)
        thread.start()
        try:
            payload = result.get(timeout=timeout)
        except queue.Empty:  # pragma: no cover - ветка отладки
            raise AssertionError(f"адрес не пришёл за {timeout} с") from None
        if "error" in payload:
            raise AssertionError(payload["error"])
        return payload

    @staticmethod
    def _list_tools(port: int) -> list[str]:
        import asyncio

        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async def _call() -> list[str]:
            async with streamablehttp_client(f"http://127.0.0.1:{port}/mcp") as streams:
                read_stream, write_stream = streams[:2]
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    return [tool.name for tool in listed.tools]

        return asyncio.run(asyncio.wait_for(_call(), timeout=60))
