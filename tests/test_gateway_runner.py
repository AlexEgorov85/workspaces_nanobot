from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from lib.lifecycle.gateway_runner import GatewayRunner


def test_clean_shutdown_returns():
    runner = GatewayRunner()
    calls = []

    def run_once():
        calls.append("ok")

    runner.run_forever(run_once)
    assert calls == ["ok"]


def test_exception_triggers_restart_until_clean():
    runner = GatewayRunner(initial_delay=0.0, max_delay=0.0)
    attempts = [0]

    def run_once():
        attempts[0] += 1
        if attempts[0] < 3:
            raise RuntimeError("boom")
        # clean return on 3rd

    runner.run_forever(run_once)
    assert attempts[0] == 3


def test_keyboard_interrupt_exits():
    runner = GatewayRunner()
    calls = []

    def run_once():
        calls.append("once")
        raise KeyboardInterrupt()

    runner.run_forever(run_once)
    assert calls == ["once"]


def test_backoff_increases_then_resets_only_on_clean():
    sleeps = []
    runner = GatewayRunner(initial_delay=1.0, max_delay=4.0, sleep=sleeps.append)
    attempts = [0]

    def run_once():
        attempts[0] += 1
        if attempts[0] == 1:
            raise RuntimeError("a")
        if attempts[0] == 2:
            raise RuntimeError("b")
        # clean

    runner.run_forever(run_once)
    assert sleeps == [1.0, 2.0]


def test_max_delay_capped():
    sleeps = []
    runner = GatewayRunner(initial_delay=1.0, max_delay=2.0, sleep=sleeps.append)
    attempts = [0]

    def run_once():
        attempts[0] += 1
        if attempts[0] < 5:
            raise RuntimeError("loop")

    runner.run_forever(run_once)
    # 1, 2, 2, 2 (max=2)
    assert sleeps == [1.0, 2.0, 2.0, 2.0]
