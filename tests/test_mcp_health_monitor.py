"""Наблюдение за живостью платформы: смена состояния, восстановление, метрики.

Проверяется то, ради чего модуль написан: остановленный процесс обязан стать
виден БЕЗ чьего-либо обращения к нему, а вернувшийся — снова начать отвечать
без чужого оборота. Обе половины проверяются на цикле, а не на разовом
вызове, потому что именно цикл отличает наблюдение от «проверки при старте».
"""

from __future__ import annotations

from typing import Any

import pytest

from lib.gateway.mcp_health import (
    EVENT_DEGRADED,
    EVENT_RECOVERED,
    McpHealthMonitor,
)


class _Client:
    """Клиент, который можно заставить отвечать или молчать."""

    def __init__(self) -> None:
        self.alive = True
        self.probes = 0
        self.is_connected = True
        self.server_name = "enterprise-mcp"

    async def probe(self) -> None:
        self.probes += 1
        if not self.alive:
            self.is_connected = False
            raise RuntimeError("процесс недоступен")


def _monitor(client: _Client, events: list[tuple[str, str, str]] | None = None):
    async def publish(event_type: str, name: str, payload: dict, level: str) -> None:
        if events is not None:
            events.append((event_type, name, level))

    return McpHealthMonitor(client, interval_sec=1.0, publish=publish)


@pytest.mark.asyncio
class TestStateIsObserved:
    async def test_working_platform_is_up(self) -> None:
        client = _Client()
        monitor = _monitor(client)
        status = await monitor.check_once()
        assert status.up is True
        assert status.ever_checked is True
        assert status.error is None
        assert monitor.get_stats()["probes"] == 1

    async def test_dead_platform_is_down_even_without_any_call(self) -> None:
        """Причина существования наблюдателя: о нём никто не звал.

        Сессия клиента после смерти процесса остаётся не-``None``, поэтому
        ``is_connected`` продолжает врать про UP, пока не придёт первая
        неудачная операция.
        """
        client = _Client()
        client.alive = False
        monitor = _monitor(client)
        status = await monitor.check_once()
        assert status.up is False
        assert "недоступен" in (status.error or "")
        assert monitor.get_stats()["consecutive_failures"] == 1


@pytest.mark.asyncio
class TestEventsOnlyOnChange:
    async def test_degradation_publishes_once_not_per_probe(self) -> None:
        events: list[tuple[str, str, str]] = []
        client = _Client()
        monitor = _monitor(client, events)
        await monitor.check_once()          # UP, смены состояния нет
        client.alive = False
        await monitor.check_once()          # UP -> DOWN: событие
        await monitor.check_once()          # DOWN -> DOWN: тишина
        await monitor.check_once()          # DOWN -> DOWN: тишина
        assert [e[0] for e in events] == [EVENT_DEGRADED]
        assert events[0][2] == "WARN"

    async def test_recovery_publishes_once(self) -> None:
        events: list[tuple[str, str, str]] = []
        client = _Client()
        monitor = _monitor(client, events)
        await monitor.check_once()          # UP
        client.alive = False
        await monitor.check_once()          # UP -> DOWN: событие
        client.alive = True
        await monitor.check_once()          # DOWN -> UP: событие
        await monitor.check_once()          # UP -> UP: тишина
        assert [e[0] for e in events] == [EVENT_DEGRADED, EVENT_RECOVERED]
        assert monitor.get_stats()["recoveries"] == 1

    async def test_first_failure_is_reported_even_without_a_prior_success(self) -> None:
        """Платформа, не ответившая ни разу, — тоже недоступность.

        Молчать здесь нельзя: иначе при первом же сбое после старта журнал
        обрывался бы без единой строки.
        """
        events: list[tuple[str, str, str]] = []
        client = _Client()
        client.alive = False
        monitor = _monitor(client, events)
        await monitor.check_once()
        await monitor.check_once()
        assert [e[0] for e in events] == [EVENT_DEGRADED]

    async def test_first_success_is_not_a_recovery(self) -> None:
        """Неизвестное прежнее состояние — не повод слать «восстановление»."""
        events: list[tuple[str, str, str]] = []
        monitor = _monitor(_Client(), events)
        await monitor.check_once()
        assert events == []

    async def test_first_probe_never_publishes_a_transition(self) -> None:
        """Неизвестное прежнее состояние — не повод слать «восстановление»."""
        events: list[tuple[str, str, str]] = []
        monitor = _monitor(_Client(), events)
        await monitor.check_once()
        assert events == []

    async def test_recovery_event_name_is_known_to_the_journal(self) -> None:
        """Неизвестный тип события отвергается платформой вместе с батчем.

        Проверяется против настоящего словаря платформы, а не против её копии
        в тесте: локальная копия разошлась бы молча.
        """
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parents[1] / "mcp-platform"
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from libs.enterprise_common.eventing.types import EVENT_TYPES

        assert EVENT_DEGRADED in EVENT_TYPES
        assert EVENT_RECOVERED in EVENT_TYPES


@pytest.mark.asyncio
class TestLoopSurvivesAndRecovers:
    async def test_loop_keeps_probing_while_platform_is_down(self) -> None:
        client = _Client()
        client.alive = False
        monitor = _monitor(client)
        task = await _start(monitor)
        await _settle()
        assert task.done() is False, "отказ пробы убил цикл"
        assert client.probes >= 2
        await monitor.stop()
        assert task.done() or task.cancelled()

    async def test_platform_returns_without_anyone_asking(self) -> None:
        """Восстановление не ждёт чужого оборота — это и есть лечение."""
        client = _Client()
        client.alive = False
        monitor = _monitor(client)
        task = await _start(monitor)
        await _settle()
        assert monitor.status().up is False
        client.alive = True
        await _settle()
        assert monitor.status().up is True, "вернувшаяся платформа не замечена"
        await monitor.stop()

    async def test_publish_failure_does_not_stop_observation(self) -> None:
        async def broken(event_type: str, name: str, payload: dict, level: str) -> None:
            raise RuntimeError("журнал недоступен")

        client = _Client()
        monitor = McpHealthMonitor(client, interval_sec=1.0, publish=broken)
        client.alive = False
        await monitor.check_once()
        assert monitor.status().up is False
        client.alive = True
        assert (await monitor.check_once()).up is True

    async def test_stats_report_age_of_last_observation(self) -> None:
        client = _Client()
        monitor = _monitor(client)
        assert monitor.status().ever_checked is False
        assert monitor.status().summary() == "не проверялась"
        await monitor.check_once()
        stats = monitor.get_stats()
        assert stats["ever_checked"] is True
        assert stats["age_sec"] is not None and stats["age_sec"] >= 0
        assert stats["running"] is False


async def _start(monitor: McpHealthMonitor) -> Any:
    await monitor.start()
    return monitor._task


async def _settle() -> None:
    """Дать циклу сделать минимум две пробы."""
    import asyncio

    for _ in range(60):
        await asyncio.sleep(0.02)
