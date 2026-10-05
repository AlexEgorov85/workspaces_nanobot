"""HTTP-транспорт платформы со стороны агента.

Закрывает требования change ``2026-10-04-enterprise-mcp-http-transport``
на стороне агента (``runtime/entrypoints``): фактический адрес приходит
каналом уведомления, а не печатью; закреплённый порт проверяется ДО
запуска процесса и в обоих входах в систему; сервер по адресу доказывает,
что он наш; stderr остаётся видимым при смене транспорта.

Живой страж один: порт занимает настоящий сокет, ребёнок поднимает
настоящий сервер через НАСТОЯЩИЙ модуль транспорта платформы
(``servers.enterprise.http_transport``), и уведомление о фактическом
адресе пишет он сам. Двойник проверял бы, что тест зелёный, а именно
здесь ломается транспорт: чтение уведомления, сверка pid, заголовок
``Host`` и разбор JSON-RPC.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import sys
import textwrap
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import pytest

from lib.services.enterprise_mcp_client import (
    TRANSPORT_HTTP,
    EnterpriseMcpClient,
    EnterpriseMcpPortBusy,
    EnterpriseMcpUnavailable,
    _confirm_identity,
    _confirm_process,
    check_platform_port,
    client_from_settings,
    loopback_bind,
    port_busy_message,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
PLATFORM_ROOT = REPO_ROOT / "mcp-platform"


@pytest.fixture(autouse=True)
def _no_viewer_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Зрителей в тестах нет — ни окон, ни процессов зрителя.

    Окно зрителя проверяет ``test_enterprise_mcp_stderr.py``; здесь предмет —
    stderr в файле. На машине с графикой каждое объявление ``stderr_log``
    открывало бы настоящую консоль.

    Подменён только ``spawn_viewer``: это единственный шов, которым модуль
    запускает зрителя. Подменять ``subprocess.Popen`` нельзя — это тот же
    объект, которым клиент ищет держателя порта через ``netstat``.
    """
    from lib.services import enterprise_mcp_stderr as stderr_mod

    monkeypatch.setattr(stderr_mod, "spawn_viewer", lambda path: "зритель (тест)")


# -- ребёнок: настоящий сервер через настоящий модуль транспорта ----------

_CHILD = textwrap.dedent(
    """
    import asyncio, json, os, sys

    sys.path.insert(0, sys.argv[1])
    mode = os.environ.get("FAKE_MODE", "serve")
    port = int(sys.argv[2])
    pid_shift = int(os.environ.get("FAKE_PID_SHIFT", "0"))

    from mcp.server.fastmcp import FastMCP
    from servers.enterprise import http_transport

    server = FastMCP(os.environ.get("FAKE_NAME", "enterprise-mcp"))

    # Имя операции уезжает в discovery ровно таким, каким его объявляет
    # платформа: с capability. FastMCP выводит имя из имени функции, то есть
    # без явного ``name=`` в объявление ушло бы старое плоское ``list_indexes``
    # и проверка состава операций падала бы на несуществующем имени.
    @server.tool(name="vectors.list_indexes")
    def list_indexes() -> list[str]:
        return ["check_entity_index"]

    transport = server._mcp_server
    sys.stderr.write("platform: поднят, pid=%d\\n" % os.getpid())
    sys.stderr.flush()

    def notify(p: int, pid: int) -> None:
        # 0 — дескриптор, объявленный блоком: канал уведомления открыт
        # агентом и передан ребёнку в слот stdin. На http-ветке stdin
        # свободен от JSON-RPC.
        os.write(
            0,
            (json.dumps({"host": "127.0.0.1", "port": p, "pid": pid}, sort_keys=True)
             + "\\n").encode("utf-8"),
        )

    if mode == "silent":
        raise SystemExit(3)
    if mode == "garbage":
        # Человеческая строка вместо адреса: клиент обязан её отвергнуть,
        # а не разобрать как «порт с нуля».
        os.write(0, "LISTENING 127.0.0.1:0\\\\n".encode("utf-8"))
        raise SystemExit(0)
    if mode == "duplicate":
        notify(port, os.getpid() + pid_shift)
        notify(port + 1, os.getpid() + pid_shift)
        raise SystemExit(0)
    if mode == "fake_pid":
        notify(port, os.getpid() + 1)
        raise SystemExit(0)
    if mode == "notify_only":
        notify(port, os.getpid() + pid_shift)
        raise SystemExit(0)

    request = http_transport.TransportRequest(
        mode="http", bind="127.0.0.1", port=port, notify_fd=0
    )
    asyncio.run(http_transport.serve_http(transport, request))
    """
)


def _child_args(port: int) -> list[str]:
    """Аргументы ребёнка. Интерпретатор — в ``command`` клиента, второй раз
    он в argv означал бы ``python.exe python.exe -c ...``."""

    return ["-c", _CHILD, str(PLATFORM_ROOT), str(port)]


def _client(
    port: int,
    *,
    mode: str = "serve",
    profile: str = "",
    name: str = "enterprise-mcp",
    pid_shift: int = 0,
    stderr_log: str | os.PathLike[str] | None = None,
) -> EnterpriseMcpClient:
    os.environ["FAKE_MODE"] = mode
    os.environ["FAKE_NAME"] = name
    os.environ["FAKE_PID_SHIFT"] = str(pid_shift)
    return EnterpriseMcpClient(
        command=sys.executable,
        args=_child_args(port),
        transport=TRANSPORT_HTTP,
        bind="127.0.0.1",
        port=port,
        profile=profile,
        stderr_log=stderr_log,
    )


def _scenario(client: EnterpriseMcpClient) -> Callable[[Callable], Any]:
    """Прогнать сценарий в одном event loop и обязательно закрыть сессию.

    Один loop обязателен: сессия привязана к тому, на котором создана, а
    процесс платформы снимается при закрытии стека. Сценарий без этого
    оставлял бы дочерний процесс живым после зелёного теста.
    """

    def runner(action: Callable[[EnterpriseMcpClient], Awaitable[Any]]) -> Any:
        async def main() -> Any:
            try:
                return await asyncio.wait_for(action(client), timeout=180)
            finally:
                for name in ("FAKE_MODE", "FAKE_NAME", "FAKE_PID_SHIFT"):
                    os.environ.pop(name, None)
                with contextlib.suppress(Exception):
                    await client.aclose()

        return asyncio.run(main())

    return runner


# -- помощники занятости ---------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _hold_port() -> tuple[socket.socket, int]:
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(8)
    return holder, int(holder.getsockname()[1])


# -- порт: запрос, а не объявление ----------------------------------------


class TestPortUniqueness:
    """Порт запрашивает агент, фактический адрес сообщает платформа."""

    def test_requested_port_read_from_settings(self) -> None:
        settings = {
            "enterprise_mcp": {
                "command": sys.executable,
                "args": ["-m", "servers.enterprise.server"],
                "transport": {"mode": "http", "port": 18412, "bind": "127.0.0.1"},
            }
        }
        client = client_from_settings(settings)
        assert client is not None
        assert client._port == 18412
        assert client._transport == TRANSPORT_HTTP
        assert client._bind == "127.0.0.1"

    def test_absent_port_means_ephemeral(self) -> None:
        settings = {
            "enterprise_mcp": {
                "command": sys.executable,
                "args": [],
                "transport": {"mode": "http"},
            }
        }
        client = client_from_settings(settings)
        assert client is not None and client._port == 0

    def test_stdio_stays_the_default_and_rollback_path(self) -> None:
        """Смена значения на stdio возвращает прежнее поведение."""

        settings = {"enterprise_mcp": {"command": sys.executable, "args": []}}
        client = client_from_settings(settings)
        assert client is not None
        assert client._transport == "stdio" and client._port == 0

    def test_unknown_transport_is_a_configuration_error(self) -> None:
        from config import ConfigurationError

        settings = {
            "enterprise_mcp": {
                "command": sys.executable,
                "args": [],
                "transport": {"mode": "grpc"},
            }
        }
        with pytest.raises(ConfigurationError) as failure:
            client_from_settings(settings)
        assert "transport.mode" in str(failure.value)

    def test_no_port_in_profile_overlay(self) -> None:
        """Порт не вычисляется по контуру и не едет ни в argv, ни в env."""

        settings = {
            "profile": "test",
            "enterprise_mcp": {
                "command": sys.executable,
                "args": ["-m", "servers.enterprise.server"],
                "transport": {"mode": "http", "port": 18413},
            },
        }
        client = client_from_settings(settings)
        assert client is not None
        assert client._profile == "test"
        joined = " ".join(str(part) for part in client._args)
        assert "18413" not in joined
        assert "ENTERPRISE" not in joined
        assert "transport" not in joined

    def test_endpoint_read_from_notify_channel(self) -> None:
        """Адрес приходит уведомлением, и он совпадает с запрошенным."""

        port = _free_port()
        client = _client(port)

        async def action(c: EnterpriseMcpClient) -> str:
            await c.list_operations()
            return c._endpoint or ""

        assert _scenario(client)(action) == f"127.0.0.1:{port}"

    def test_endpoint_never_read_from_stdout(self) -> None:
        """stdout ребёнка на http-ветке не несёт адреса.

        Адрес приходит в дескриптор, который открыл агент, а stdout уходит в
        DEVNULL: прочитать его было бы негде. Проверка обратная — канал
        уведомления наполняется, а stdout остаётся пустым, то есть адрес
        шёл не через него.
        """
        from lib.services.enterprise_mcp_client import _open_notify_channel

        port = _free_port()
        read_fd, write_fd = _open_notify_channel()
        try:
            out = asyncio.run(_spawn_and_collect_stdout(port, write_fd))
            assert out == b"", f"ребёнок напечатал в stdout: {out[:200]!r}"
            notice = os.read(read_fd, 4096)
        finally:
            os.close(read_fd)
        payload = json.loads(notice.decode("utf-8").strip())
        assert payload["port"] == port


async def _spawn_and_collect_stdout(port: int, write_fd: int) -> bytes:
    """Поднять ребёнка вживую: его stdout — в память, уведомление — в канал.

    Один event loop на весь обмен: процесс и его stdout принадлежат loop'у,
    на котором созданы.
    """
    os.environ["FAKE_MODE"] = "notify_only"
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            *_child_args(port),
            stdin=write_fd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _err = await proc.communicate()
        return out
    finally:
        os.environ.pop("FAKE_MODE", None)


# -- loopback --------------------------------------------------------------


class TestLoopback:
    """Сервер слушает только loopback, и это проверяет агент."""

    def test_binds_loopback_only(self) -> None:
        assert loopback_bind("127.0.0.1") == "127.0.0.1"
        assert loopback_bind("::1") == "::1"
        # localhost разворачивается здесь, а не резолвером ОС.
        assert loopback_bind("localhost") == "127.0.0.1"
        assert loopback_bind("LOCALHOST") == "127.0.0.1"

    @pytest.mark.parametrize("bind", ["0.0.0.0", "192.168.1.10", "example.com", ""])
    def test_wildcard_bind_is_refused(self, bind: str) -> None:
        from config import ConfigurationError

        with pytest.raises(ConfigurationError) as failure:
            loopback_bind(bind)
        assert "loopback" in str(failure.value)

    def test_wildcard_bind_refused_before_any_process(self) -> None:
        """Отказ по адресу — до подъёма процесса, а не упавшим ребёнком."""

        from config import ConfigurationError

        settings = {
            "enterprise_mcp": {
                "command": sys.executable,
                "args": [],
                "transport": {"mode": "http", "bind": "0.0.0.0", "port": 18412},
            }
        }
        with pytest.raises(ConfigurationError) as failure:
            client_from_settings(settings)
        assert "transport.bind" in str(failure.value)


# -- stderr на http-ветке --------------------------------------------------


class TestStderrOnHttpPath:
    """stderr ребёнка уходит в объявленный файл и на http-ветке.

    Существующий страж покрывает только stdio-путь: ``errlog`` — параметр
    ``stdio_client``, и в http-ветке он неприменим вовсе. Файл открывает
    агент, поэтому проверять надо именно здесь.
    """

    def test_stderr_lands_in_declared_file(self, tmp_path: Path) -> None:
        log = tmp_path / "platform.log"
        client = _client(_free_port(), mode="notify_only", stderr_log=str(log))

        async def action(c: EnterpriseMcpClient) -> None:
            with pytest.raises(EnterpriseMcpUnavailable):
                await c.list_operations()

        _scenario(client)(action)
        text = log.read_text(encoding="utf-8")
        assert "platform: поднят" in text
        assert "transport=http" in text  # метка подъёма от агента

    def test_declared_path_wins_over_agent_stderr(
        self, tmp_path: Path, capfd
    ) -> None:
        log = tmp_path / "platform.log"
        client = _client(_free_port(), mode="notify_only", stderr_log=str(log))

        async def action(c: EnterpriseMcpClient) -> None:
            with pytest.raises(EnterpriseMcpUnavailable):
                await c.list_operations()

        _scenario(client)(action)
        captured = capfd.readouterr()
        assert "platform: поднят" not in captured.err
        assert "platform: поднят" in log.read_text(encoding="utf-8")

    def test_same_file_serves_reconnect(self, tmp_path: Path) -> None:
        """Переподключение дописывает в тот же файл, а не затирает его."""

        log = tmp_path / "platform.log"
        client = _client(_free_port(), mode="notify_only", stderr_log=str(log))

        async def action(c: EnterpriseMcpClient) -> None:
            with pytest.raises(EnterpriseMcpUnavailable):
                await c.list_operations()
            await c._reset()
            with pytest.raises(EnterpriseMcpUnavailable):
                await c.list_operations()

        _scenario(client)(action)
        assert log.read_text(encoding="utf-8").count("platform: поднят") == 2

    def test_report_names_the_declared_path(self, tmp_path: Path) -> None:
        """Баннер говорит, куда ушёл stderr: иначе «окно не открылось»
        читается как «смотреть не на что»."""

        log = tmp_path / "platform.log"
        client = _client(_free_port(), mode="notify_only", stderr_log=str(log))

        async def action(c: EnterpriseMcpClient) -> None:
            with contextlib.suppress(EnterpriseMcpUnavailable):
                await c.list_operations()

        _scenario(client)(action)
        assert str(log.name) in client.stderr_report()


# -- занятость закреплённого порта ----------------------------------------


class TestPortExclusivity:
    """Проверка только при закреплённом порте и только на http."""

    def test_ephemeral_port_skips_occupancy_check(self, monkeypatch) -> None:
        """При порте 0 проверки нет: иначе ложный отказ по адресу, который
        платформа не занимает."""

        def forbidden(*_args, **_kwargs):
            raise AssertionError("проверка занятости выполнена при порте 0")

        monkeypatch.setattr(
            "lib.services.enterprise_mcp_client.find_listener_pid", forbidden
        )
        check_platform_port("127.0.0.1", 0)

    def test_free_port_passes_the_check(self) -> None:
        port = _free_port()
        check_platform_port("127.0.0.1", port)

    def test_occupied_port_refuses_startup_with_pid(self) -> None:
        holder, port = _hold_port()
        try:
            with pytest.raises(EnterpriseMcpPortBusy) as failure:
                check_platform_port("127.0.0.1", port)
            assert failure.value.holder_pid
            assert str(failure.value.holder_pid) in str(failure.value)
        finally:
            holder.close()

    def test_occupied_port_never_spawns_a_second_process(self, monkeypatch) -> None:
        """Проверка ДО подъёма: процесс платформы не появляется вовсе."""

        holder, port = _hold_port()
        client = _client(port)
        spawned: list[Any] = []
        real_spawn = asyncio.create_subprocess_exec

        async def spy(*args: Any, **kwargs: Any) -> Any:
            spawned.append(args)
            return await real_spawn(*args, **kwargs)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
        try:

            async def action(c: EnterpriseMcpClient) -> None:
                with pytest.raises(EnterpriseMcpPortBusy):
                    await c.list_operations()

            _scenario(client)(action)
            assert not spawned, "процесс платформы поднялся при занятом порте"
        finally:
            holder.close()

    def test_free_port_starts_process(self) -> None:
        """Свободный закреплённый порт: процесс поднимается и отвечает."""

        port = _free_port()
        client = _client(port)

        async def action(c: EnterpriseMcpClient) -> str:
            await c.list_operations()
            return c.presence_line(1)

        line = _scenario(client)(action)
        assert "процесс поднят" in line
        assert f"адрес=127.0.0.1:{port}" in line

    def test_unknown_holder_is_stated_as_unknown(self) -> None:
        text = port_busy_message("127.0.0.1", 18412, None)
        assert "НЕ ОПРЕДЕЛЁН" in text
        assert "остался висеть" not in text

    def test_live_holder_is_not_called_a_stale_process(self) -> None:
        text = port_busy_message("127.0.0.1", 18412, 4242)
        assert "4242" in text
        assert "остался висеть" not in text


# -- подсказка по устранению и поиск PID ----------------------------------


class TestPlatformPortCheck:
    """Проверка обязана работать на обеих ОС."""

    def test_linux_hint_does_not_mention_taskkill(self) -> None:
        text = port_busy_message("127.0.0.1", 18412, 4242, posix=True)
        assert "taskkill" not in text.lower()
        assert "kill 4242" in text

    def test_windows_hint_names_taskkill(self) -> None:
        text = port_busy_message("127.0.0.1", 18412, 4242, posix=False)
        assert "taskkill /PID 4242 /F" in text

    def test_unknown_holder_hint_is_platform_neutral(self) -> None:
        for posix in (True, False):
            text = port_busy_message("127.0.0.1", 18412, None, posix=posix)
            assert "taskkill" not in text.lower()
            assert "netstat" in text and "ss -ltnp" in text

    def test_linux_finds_listener_pid_by_proc(self, monkeypatch) -> None:
        """Ветка Linux: inode слушающего сокета -> владелец в /proc/<pid>/fd.

        Ветка написана по коду и на этой машине (Windows) не проверена, поэтому
        проверяется она по содержимому поддельного /proc, а не запуском.
        """

        class _FakeProc:
            NET_TCP = (
                "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when"
                " retrnsmt   uid  timeout inode\n"
                "   0: 0100007F:47EC 00000000:0000 0A 00000000:00000000 00:00000000"
                " 00000000     0        0 555 1 0 100 0 -1 4194304\n"
                "   1: 0100007F:47EC 0100007F:1F90 01 00000000:00000000 00:00000000"
                " 00000000     0        0 556 1 0 100 0 -1 4194304\n"
            )
            HANDLES = {
                "/proc/1/fd/3": "/dev/null",
                "/proc/99/fd/4": "socket:[777]",
                "/proc/4242/fd/7": "socket:[555]",
            }

            @staticmethod
            def read_text(self: Path, *args, **kwargs) -> str:
                # Подмена стоит на классе, поэтому первым аргументом приходит
                # сам Path, а не отдельный путь. Статический метод обязателен:
                # связанный метод отдавал бы в ``self`` подделку, а не путь.
                text = str(self).replace("\\", "/")
                if text.endswith("net/tcp"):
                    return _FakeProc.NET_TCP
                if text.endswith("net/tcp6"):
                    return _FakeProc.NET_TCP.splitlines()[0] + "\n"
                raise OSError("нет такого файла")

            PIDS = ("1", "99", "4242")

            @staticmethod
            def iterdir(self: Path) -> list[Path]:
                text = str(self).replace("\\", "/")
                if text.endswith("/fd"):
                    prefix = text.rsplit("/fd", 1)[0] + "/fd/"
                    return [
                        Path(key) for key in _FakeProc.HANDLES if key.startswith(prefix)
                    ]
                return [Path("/proc") / pid for pid in _FakeProc.PIDS]

        fake = _FakeProc()
        monkeypatch.setattr("sys.platform", "linux")
        monkeypatch.setattr(Path, "read_text", fake.read_text)
        monkeypatch.setattr(Path, "iterdir", fake.iterdir)
        monkeypatch.setattr(
            os,
            "readlink",
            lambda path: fake.HANDLES[str(path).replace("\\", "/")],
        )
        from lib.services.enterprise_mcp_client import find_listener_pid

        assert find_listener_pid("127.0.0.1", 18412) == 4242

    def test_linux_inodes_ignore_non_listening_state(self, monkeypatch) -> None:
        """``st == 0A`` — только LISTEN; соединение в состоянии 01 — не наш."""

        header = (
            "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when"
            " retrnsmt   uid  timeout inode\n"
        )
        established = (
            "   1: 0100007F:47EC 0100007F:1F90 01 00000000:00000000 00:00000000"
            " 00000000     0        0 556 1 0 100 0 -1 4194304\n"
        )
        from lib.services.enterprise_mcp_client import _linux_listen_inodes

        def reader(path: Path, *args, **kwargs) -> str:
            if str(path).replace("\\", "/").endswith("net/tcp6"):
                return header
            return header + established

        monkeypatch.setattr(Path, "read_text", reader)
        assert _linux_listen_inodes(18412) == set()


# -- личность сервера ------------------------------------------------------


class _ServerInfo:
    def __init__(self, name: str) -> None:
        self.name = name
        self.version = "0.0.0"


class TestServerIdentity:
    """Ответ по адресу доказывает, что сервер наш."""

    def test_matching_profile_is_accepted(self) -> None:
        _confirm_identity(_ServerInfo("enterprise-mcp:test"), "test")

    def test_base_profile_is_accepted_for_plain_name(self) -> None:
        _confirm_identity(_ServerInfo("enterprise-mcp"), None)

    def test_foreign_profile_is_refused_as_configuration_error(self) -> None:
        from config import ConfigurationError

        with pytest.raises(ConfigurationError) as failure:
            _confirm_identity(_ServerInfo("enterprise-mcp:prod"), "test")
        text = str(failure.value)
        assert "test" in text and "prod" in text

    def test_refusal_reason_is_configuration_not_unavailable(self) -> None:
        from config import ConfigurationError

        with pytest.raises(ConfigurationError):
            _confirm_identity(_ServerInfo("enterprise-mcp:other"), "test")
        # Повтор такой отказ не исправит — это не «сервер не поднялся».
        assert not issubclass(ConfigurationError, EnterpriseMcpUnavailable)

    @pytest.mark.parametrize("name", ["", "enterprise-mcp"])
    def test_missing_identity_is_not_accepted(self, name: str) -> None:
        """Контур не назван — догадками продолжать работу нельзя."""

        from config import ConfigurationError

        with pytest.raises(ConfigurationError):
            _confirm_identity(_ServerInfo(name), "test")

    def test_pid_mismatch_is_refused_before_connecting(self) -> None:
        with pytest.raises(EnterpriseMcpUnavailable) as failure:
            _confirm_process(999, 4242)
        assert "999" in str(failure.value) and "4242" in str(failure.value)

    def test_foreign_profile_does_not_leave_the_process_alive(
        self, monkeypatch
    ) -> None:
        """Отказ по контуру закрывает процесс, а не оставляет его жить.

        Сверка личности идёт после ``initialize()``, и если бы она стояла вне
        ``try``, поднятый сервер остался бы висеть после отказа — молчаливый
        владелец пула и порта. Проверка именно на закрытие стека.
        """
        from config import ConfigurationError
        from lib.services import enterprise_mcp_client as client_mod

        terminated: list[int] = []
        real_terminate = client_mod._terminate_child

        async def spy(process: Any) -> None:
            terminated.append(process.pid)
            await real_terminate(process)

        monkeypatch.setattr(client_mod, "_terminate_child", spy)
        port = _free_port()
        client = _client(port, profile="test", name="enterprise-mcp:prod")

        async def action(c: EnterpriseMcpClient) -> None:
            with pytest.raises(ConfigurationError):
                await c.list_operations()

        _scenario(client)(action)
        assert terminated, "процесс платформы остался жив после отказа по контуру"

    def test_foreign_profile_on_live_server_refuses_handshake(self) -> None:
        """Сервер назвался чужим контуром — отказ конфигурации на живом подъёме."""

        from config import ConfigurationError

        port = _free_port()
        client = _client(port, profile="test", name="enterprise-mcp:prod")

        async def action(c: EnterpriseMcpClient) -> None:
            with pytest.raises(ConfigurationError):
                await c.list_operations()

        _scenario(client)(action)


# -- контракт канала уведомления ------------------------------------------


class TestNotifyContract:
    """Отсутствие, нечитаемость и повтор — отказ, а не ожидание."""

    def test_missing_notification_refuses_startup(self) -> None:
        client = _client(_free_port(), mode="silent")

        async def action(c: EnterpriseMcpClient) -> None:
            with pytest.raises(EnterpriseMcpUnavailable) as failure:
                await c.list_operations()
            text = str(failure.value)
            assert "не сообщил" in text or "закрыла канал" in text

    def test_dead_child_does_not_hang_startup(self) -> None:
        """Мёртвый ребёнок: чтение уведомления возвращается, а не висит."""

        client = _client(_free_port(), mode="silent")
        started = time.monotonic()

        async def action(c: EnterpriseMcpClient) -> None:
            with pytest.raises(EnterpriseMcpUnavailable):
                await c.list_operations()

        _scenario(client)(action)
        assert time.monotonic() - started < 120

    def test_duplicate_notification_refuses_startup(self) -> None:
        client = _client(_free_port(), mode="duplicate")

        async def action(c: EnterpriseMcpClient) -> None:
            with pytest.raises(EnterpriseMcpUnavailable) as failure:
                await c.list_operations()
            assert "второе уведомление" in str(failure.value)

        _scenario(client)(action)

    def test_unreadable_notification_refuses_startup(self) -> None:
        client = _client(_free_port(), mode="garbage")

        async def action(c: EnterpriseMcpClient) -> None:
            with pytest.raises(EnterpriseMcpUnavailable):
                await c.list_operations()

        _scenario(client)(action)

    def test_foreign_pid_on_notification_refuses_startup(self) -> None:
        """Адрес сообщил не тот процесс, которого агент запустил."""

        client = _client(_free_port(), mode="fake_pid")

        async def action(c: EnterpriseMcpClient) -> None:
            with pytest.raises(EnterpriseMcpUnavailable) as failure:
                await c.list_operations()
            assert "не та платформа" in str(failure.value)

        _scenario(client)(action)

    def test_channel_is_a_dedicated_descriptor_not_stdout(self) -> None:
        """Канал открывает агент: пара дескрипторов, а не stdout ребёнка."""

        from lib.services.enterprise_mcp_client import _open_notify_channel

        read_fd, write_fd = _open_notify_channel()
        try:
            os.write(write_fd, b"{}\n")
            assert os.read(read_fd, 3) == b"{}\n"
        finally:
            os.close(read_fd)
            os.close(write_fd)


# -- восстановление --------------------------------------------------------


class TestRecovery:
    """Переподъём заново читает адрес и заново проверяет закреплённый порт."""

    def test_recovery_rereads_endpoint(self) -> None:
        """Переподъём берёт адрес из НОВОГО уведомления, а не прежний."""

        first = _free_port()
        client = _client(first)

        async def action(c: EnterpriseMcpClient) -> tuple[str | None, int | None, str | None, int | None]:
            await c.list_operations()
            before = (c._endpoint, c._platform_pid)
            await c._reset()
            # Прежний адрес и pid не переживают сброс: подъём заново.
            assert c._endpoint is None and c._platform_pid is None
            second = _free_port()
            c._port = second
            c._args[-1] = str(second)
            os.environ["FAKE_MODE"] = "serve"
            await c.list_operations()
            return (*before, c._endpoint, c._platform_pid)

        first_endpoint, first_pid, second_endpoint, second_pid = _scenario(client)(action)
        assert first_endpoint == f"127.0.0.1:{first}"
        assert second_endpoint is not None and second_endpoint.startswith("127.0.0.1:")
        assert first_endpoint != second_endpoint
        assert first_pid != second_pid

    def test_recovery_refuses_taken_pinned_port(self) -> None:
        """Прежний адрес после переподъёма невалиден: порт уже чужой."""

        port = _free_port()
        client = _client(port)

        async def action(c: EnterpriseMcpClient) -> None:
            await c.list_operations()
            await c._reset()
            holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            holder.bind(("127.0.0.1", port))
            holder.listen(8)
            try:
                with pytest.raises(EnterpriseMcpPortBusy) as failure:
                    await c.list_operations()
                assert failure.value.holder_pid
            finally:
                holder.close()

        _scenario(client)(action)

    def test_reset_clears_previous_endpoint(self) -> None:
        client = _client(_free_port())

        async def action(c: EnterpriseMcpClient) -> None:
            await c.list_operations()
            assert c._endpoint is not None
            await c._reset()
            assert c._endpoint is None and c._platform_pid is None

        _scenario(client)(action)


# -- сквозной путь и вердикт ----------------------------------------------


class TestHttpServing:
    """Настоящий сервер, настоящая сессия, настоящая операция."""

    def test_client_initializes_and_calls_a_tool(self) -> None:
        port = _free_port()
        client = _client(port)

        async def action(c: EnterpriseMcpClient) -> list[str]:
            return await c.list_operations()

        operations = _scenario(client)(action)
        assert "vectors.list_indexes" in operations

    def test_ephemeral_port_announced_by_platform(self) -> None:
        """Порт 0: адрес приходит от ОС, и клиент берёт именно его."""

        client = _client(0)

        async def action(c: EnterpriseMcpClient) -> str:
            await c.list_operations()
            return c._endpoint or ""

        endpoint = _scenario(client)(action)
        assert endpoint.startswith("127.0.0.1:")
        assert endpoint != "127.0.0.1:0"

    def test_presence_line_names_contour_pid_and_transport(self) -> None:
        port = _free_port()
        client = _client(port, profile="test", name="enterprise-mcp:test")

        async def action(c: EnterpriseMcpClient) -> str:
            await c.list_operations()
            return c.presence_line(1)

        line = _scenario(client)(action)
        assert "процесс поднят" in line
        assert "контур=test" in line
        assert "транспорт=http" in line
        assert "pid=неизвестен" not in line
        assert f"адрес=127.0.0.1:{port}" in line

    def test_both_entrypoints_share_one_verdict_format(self) -> None:
        """Одна строка на оба входа: вторая копия разъехалась бы."""

        gateway = (REPO_ROOT / "gateway.py").read_text(encoding="utf-8")
        cli = (REPO_ROOT / "cli_agent.py").read_text(encoding="utf-8")
        assert gateway.count("client.presence_line(") == 1
        assert cli.count("client.presence_line(") == 1

    def test_cli_refuses_occupied_platform_port(self, capfd) -> None:
        """CLI отказывает до входа в REPL, назвав PID держателя."""

        import cli_agent

        holder, port = _hold_port()

        class _Ctx:
            enterprise_mcp = _client(port)

            def __getattr__(self, name: str) -> Any:
                return MagicStub()

        try:
            with pytest.raises(BaseException):  # noqa: B017 - тип отказа не важен
                asyncio.run(cli_agent._connect_enterprise_mcp(_Ctx()))
        finally:
            holder.close()
        out = capfd.readouterr()
        assert "ПОРТ ЗАНЯТ" in out.out
        assert "держатель" in out.out


class MagicStub:
    """Заглушка конфигурации для входа, которому платформа не нужна."""

    def __getattr__(self, name: str) -> Any:
        return self

    def __getitem__(self, name: str) -> Any:
        return self
