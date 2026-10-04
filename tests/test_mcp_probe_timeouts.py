"""Проба не имеет права зависать, а подъём — спамить.

Три дефекта одного происхождения: «тишина» на остановленной платформе выглядит
как «всё работает», и это худший вид отказа, потому что он неотличим от нормы.

1. Проба без предела на весь разговор. ``send_ping()`` был под таймаутом, а
   подъём сессии с ``initialize()`` — нет. Платформа, которая стартует и не
   отвечает, оставляла наблюдение подвешенным навсегда.
2. Сброс сессии без предела. Он зовётся на каждый отказ, и зависшее закрытие
   оборванного процесса останавливало ещё и вызов, который к нему пришёл.
3. Переподключение с частотой опроса. Часто спрашивать дёшево, часто
   поднимать процесс — нет.
"""

from __future__ import annotations

import asyncio

import pytest

from lib.gateway.mcp_health import (
    DEFAULT_INTERVAL_SEC,
    RECONNECT_MIN_INTERVAL_SEC,
    McpHealthMonitor,
)
from lib.services.enterprise_mcp_client import RESET_TIMEOUT_SEC


@pytest.mark.asyncio
class TestProbeCannotHang:
    async def test_probe_gives_up_on_a_server_that_started_but_is_silent(self) -> None:
        """Молчащий на рукопожатии сервер — это отказ, а не ожидание.

        Проверяется на настоящем классе клиента: подменяется только
        ``_ensure_session``, чтобы он уходил в вечный сон вместо подъёма
        процесса. Если таймаута на пробу не будет, тест зависнет — и это
        ровно тот отказ, который он охраняет.
        """
        from lib.services.enterprise_mcp_client import EnterpriseMcpClient

        client = EnterpriseMcpClient(
            command="true", args=[], tool_timeout_sec=0.2
        )

        async def _hang() -> object:
            await asyncio.sleep(3600)
            return object()

        client._ensure_session = _hang  # type: ignore[assignment]  # noqa: SLF001

        started = asyncio.get_running_loop().time()
        with pytest.raises(Exception) as caught:
            await client.probe()
        elapsed = asyncio.get_running_loop().time() - started

        assert "0.2" in str(caught.value), caught.value
        assert elapsed < 3.0, f"проба шла {elapsed:.1f}с вместо таймаута"
        assert client.health()["probe_failures"] == 1

    async def test_reset_does_not_wait_for_a_stuck_process_forever(self) -> None:
        """Сброс не должен держать вызов, который пришёл из-за отказа."""
        from lib.services.enterprise_mcp_client import (
            EnterpriseMcpClient,
            EnterpriseMcpUnavailable,
        )

        client = EnterpriseMcpClient(
            command="true", args=[], tool_timeout_sec=0.2
        )
        original = RESET_TIMEOUT_SEC

        class _StuckStack:
            async def aclose(self) -> None:
                await asyncio.sleep(3600)

        client._stack = _StuckStack()  # type: ignore[assignment]  # noqa: SLF001

        started = asyncio.get_running_loop().time()
        await client._reset()  # type: ignore[attr-defined]  # noqa: SLF001
        elapsed = asyncio.get_running_loop().time() - started

        assert elapsed < original + 2.0, (
            f"сброс шёл {elapsed:.1f}с — он должен уложиться в {original:g}с"
        )
        assert client._stack is None, "подвисший стек остался в клиенте"
        assert issubclass(EnterpriseMcpUnavailable, Exception)


@pytest.mark.asyncio
class TestReconnectIsNotSpammed:
    async def test_default_interval_is_short_enough_to_be_noticed(self) -> None:
        """Полминуты тишины не отличить от нормы — дефолт это убирает."""
        assert DEFAULT_INTERVAL_SEC <= 10.0

    async def test_reconnect_interval_is_slower_than_probing(self) -> None:
        assert RECONNECT_MIN_INTERVAL_SEC > DEFAULT_INTERVAL_SEC

    async def test_down_loop_waits_the_longer_gap(self) -> None:
        """Пока платформа мертва, петля не поднимает её на каждой пробе."""
        sleeps: list[float] = []

        class _Client:
            is_connected = True
            server_name = "enterprise-mcp"

            def __init__(self) -> None:
                self.up = True

            async def probe(self) -> None:
                if not self.up:
                    raise RuntimeError("нет")

        client = _Client()
        monitor = McpHealthMonitor(
            client, interval_sec=1.0, reconnect_interval_sec=5.0
        )

        real_sleep = asyncio.sleep

        async def _fake_sleep(delay: float, *args: object, **kwargs: object) -> None:
            sleeps.append(delay)
            if len(sleeps) >= 3:
                raise asyncio.CancelledError
            await real_sleep(0)

        asyncio.sleep = _fake_sleep  # type: ignore[assignment]
        try:
            await monitor.run()
        except asyncio.CancelledError:
            pass
        finally:
            asyncio.sleep = real_sleep  # type: ignore[assignment]

        assert sleeps[0] == 1.0, "живая платформа опрашивается часто"
        client.up = False
        monitor._status = monitor._status.__class__(  # noqa: SLF001
            up=False, checked_at=1.0, ever_checked=True
        )
        sleeps.clear()
        asyncio.sleep = _fake_sleep  # type: ignore[assignment]
        try:
            await monitor.run()
        except asyncio.CancelledError:
            pass
        finally:
            asyncio.sleep = real_sleep  # type: ignore[assignment]

        assert sleeps and sleeps[0] == 5.0, (
            f"на мёртвой платформе ждём {sleeps[:1]}, а не интервал опроса"
        )
