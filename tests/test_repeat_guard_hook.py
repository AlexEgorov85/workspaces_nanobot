"""Юнит-тесты ``lib/hooks/repeat_guard_hook.py``.

Что покрываем:
  * канонизация аргументов (порядок ключей на всех уровнях, Path/datetime/
    bytes, NaN, недетерминированный ``str()``);
  * глобальное скользящее окно и его эвикцию;
  * режимы ``off`` / ``warn`` / ``block``;
  * разные инструменты с одинаковыми аргументами;
  * ``exempt_tools`` (точное равенство, отказ от шаблонов);
  * изоляцию между ``session_key`` и сброс на новом обороте;
  * fail-soft при сбое журналирования.

Спека: ``openspec/specs/runtime/anti-loop/spec.md``.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lib.core.project_settings import GatewayRepeatGuardSettings
from lib.hooks.repeat_guard_hook import (
    RepeatGuardBlocked,
    RepeatGuardHook,
    _canonical_arguments,
    _fingerprint_hash,
    _stable_fallback,
)

# ---------------------------------------------------------------------------
# Стабы контекстов nanobot
# ---------------------------------------------------------------------------


def _ctx(session_key: str | None = "s1", iteration: int = 0) -> SimpleNamespace:
    return SimpleNamespace(session_key=session_key, iteration=iteration)


def _call(name: str, arguments: Any) -> SimpleNamespace:
    return SimpleNamespace(name=name, arguments=arguments)


async def _fire(hook: RepeatGuardHook, ctx: Any, name: str, arguments: Any):
    """Один вызов инструмента.

    ``before_iteration`` здесь намеренно НЕ вызывается: тест сам открывает
    оборот одним вызовом. Иначе каждый вызов сбрасывал бы state сессии
    (iteration == 0) и детектор не видел бы ничего.
    """
    return await hook.before_execute_tool(ctx, _call(name, arguments), None, arguments)


# ---------------------------------------------------------------------------
# 3.1 канонизация
# ---------------------------------------------------------------------------


class TestCanonicalArguments:
    def test_canonical(self) -> None:
        """Один и тот же вызов даёт одно представление при любом порядке."""
        a = _canonical_arguments({"b": 2, "a": 1})
        b = _canonical_arguments({"a": 1, "b": 2})
        assert a == b
        assert a is not None and isinstance(a, str)

    def test_nested_key_order_is_normalized(self) -> None:
        """``sort_keys`` действует на всех уровнях, включая dict внутри list."""
        a = _canonical_arguments({"query": [{"b": 2, "a": 1}]})
        b = _canonical_arguments({"query": [{"a": 1, "b": 2}]})
        assert a == b

    def test_different_values_differ(self) -> None:
        assert _canonical_arguments({"q": "alpha"}) != _canonical_arguments({"q": "beta"})

    def test_path_is_deterministic(self) -> None:
        assert _canonical_arguments({"p": Path("/tmp/x")}) == _canonical_arguments(
            {"p": Path("/tmp/x")}
        )

    def test_path_normalisation_is_preserved(self) -> None:
        """``/tmp/x`` и ``/tmp`` — разные аргументы, и это осознанно.

        Схлопывать трейлинг-слеш было бы угадыванием намерения модели; лучше
        пересчитать вызов, чем заблокировать корректный.
        """
        assert _canonical_arguments({"p": Path("/tmp/x")}) != _canonical_arguments(
            {"p": Path("/tmp")}
        )

    def test_datetime_is_deterministic(self) -> None:
        moment = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC)
        assert _canonical_arguments({"t": moment}) == _canonical_arguments({"t": moment})

    def test_bytes_include_length(self) -> None:
        assert _canonical_arguments({"b": b"x"}) != _canonical_arguments({"b": b"xx"})
        assert _canonical_arguments({"b": b"x"}) == _canonical_arguments({"b": b"x"})

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_nan_and_inf_are_skipped(self, bad: float) -> None:
        """``allow_nan=False``: значение без канона — вызов пропускается."""
        assert math.isnan(bad) or math.isinf(bad)
        from lib.hooks.repeat_guard_hook import _SKIP

        assert _canonical_arguments({"v": bad}) is _SKIP

    def test_nondeterministic_str_is_skipped(self) -> None:
        """Объект с адресом в ``repr`` детерминизма не имеет.

        Пропуск одного вызова честнее ложного «не повтора»: защитник не
        должен молча перестать видеть настоящий цикл.
        """
        from lib.hooks.repeat_guard_hook import _SKIP

        class Opaque:
            def __str__(self) -> str:
                return f"opaque at 0x7f00ff00ff{id(self):012x}"

        assert _stable_fallback(Opaque()) is _SKIP
        assert _canonical_arguments({"v": Opaque()}) is _SKIP

    def test_fingerprint_hash_is_stable_and_short(self) -> None:
        first = _fingerprint_hash("read_file", '{"a":1}')
        assert first == _fingerprint_hash("read_file", '{"a":1}')
        assert len(first) == 8
        assert first != _fingerprint_hash("read_file", '{"a":2}')

    def test_hash_is_not_used_for_detection(self) -> None:
        """Коллизия хэша не должна ни блокировать, ни пропускать вызов."""
        settings = GatewayRepeatGuardSettings(mode="warn", window_size=5, max_repeats_in_window=3)
        hook = RepeatGuardHook(settings)
        ctx = _ctx()
        events: list[Any] = []

        class Collector:
            def __init__(self) -> None:
                self.events = events

            @staticmethod
            def is_running() -> bool:
                return True

            def log_event(self, event: Any) -> None:
                events.append(event)

        hook._db = Collector()
        asyncio.run(hook.before_iteration(ctx))  # один оборот, не сбрасываем ниже
        for _ in range(3):
            asyncio.run(_fire(hook, ctx, "read_file", {"p": "same"}))
        assert len(events) == 1
        assert events[0].payload["fingerprint_hash"] == _fingerprint_hash(
            "read_file", '{"p":"same"}'
        )


# ---------------------------------------------------------------------------
# 3.2 / 3.3 окно и эвикция
# ---------------------------------------------------------------------------


class TestSlidingWindow:
    def test_global_window(self) -> None:
        """Перемежающиеся вызовы считаются в общем окне, не по подряд."""
        settings = GatewayRepeatGuardSettings(
            mode="block", window_size=5, max_repeats_in_window=3
        )
        hook = RepeatGuardHook(settings)
        ctx = _ctx(iteration=5)
        asyncio.run(hook.before_iteration(ctx))
        plan = [
            ("read_file", {"f": "A"}),
            ("exec", {"cmd": "X"}),
            ("read_file", {"f": "A"}),
            ("history_search", {"q": "Y"}),
            ("read_file", {"f": "A"}),
        ]
        blocked_at = None
        for i, (name, args) in enumerate(plan, start=1):
            try:
                asyncio.run(
                    hook.before_execute_tool(ctx, _call(name, args), None, args)
                )
            except RepeatGuardBlocked:
                blocked_at = i
                break
        assert blocked_at == 5, "срабатывание ожидалось на 5-м вызове"

    def test_window_eviction(self) -> None:
        """Выпавшие из окна вызовы не учитываются.

        Дискриминирующий — 5-й вызов: ``append`` вытесняет из ``[A, B, C]``
        самый левый A, поэтому в окне ``[B, C, A]`` ровно один A и порог не
        пройден. Без эвикции в окне было бы четыре A и блокировка случилась
        бы на 5-м вызове — тест упал бы.
        """
        settings = GatewayRepeatGuardSettings(
            mode="block", window_size=3, max_repeats_in_window=3
        )
        hook = RepeatGuardHook(settings)
        ctx = _ctx()
        asyncio.run(hook.before_iteration(ctx))

        async def call(name: str, args: Any) -> str:
            try:
                await hook.before_execute_tool(ctx, _call(name, args), None, args)
                return "ok"
            except RepeatGuardBlocked:
                return "blocked"

        plan = [
            ("tool", {"v": "A"}),  # [A]            -> 1
            ("tool", {"v": "A"}),  # [A, A]         -> 2
            ("tool", {"v": "B"}),  # [A, A, B]      -> A: 2
            ("tool", {"v": "C"}),  # [A, B, C]      -> первый A вытеснен
            ("tool", {"v": "A"}),  # [B, C, A]      -> A: 1
            ("tool", {"v": "A"}),  # [C, A, A]      -> A: 2
            ("tool", {"v": "A"}),  # [A, A, A]      -> A: 3, порог
        ]
        results = [asyncio.run(call(n, a)) for n, a in plan]
        assert results == ["ok"] * 6 + ["blocked"], results

    def test_counter_includes_current_call(self) -> None:
        settings = GatewayRepeatGuardSettings(
            mode="block", window_size=10, max_repeats_in_window=3
        )
        hook = RepeatGuardHook(settings)
        ctx = _ctx()
        asyncio.run(hook.before_iteration(ctx))

        async def call(i: int) -> bool:
            try:
                await hook.before_execute_tool(
                    ctx, _call("t", {"v": 1}), None, {"v": 1}
                )
                return False
            except RepeatGuardBlocked:
                return True

        assert [asyncio.run(call(i)) for i in range(4)] == [
            False,
            False,
            True,
            True,
        ]


# ---------------------------------------------------------------------------
# 3.4 режимы
# ---------------------------------------------------------------------------


class _Collector:
    def __init__(self, boom: bool = False) -> None:
        self.events: list[Any] = []
        self._boom = boom

    @staticmethod
    def is_running() -> bool:
        return True

    def log_event(self, event: Any) -> None:
        if self._boom:
            raise RuntimeError("БД недоступна")
        self.events.append(event)


class TestModes:
    def test_modes(self) -> None:
        """``off`` не трогает ничего; ``warn`` пишет один раз; ``block`` падает."""
        args = {"v": "same"}

        # off
        off = RepeatGuardHook(GatewayRepeatGuardSettings(mode="off"))
        off_events = _Collector()
        off._db = off_events
        ctx = _ctx()
        asyncio.run(off.before_iteration(ctx))
        for _ in range(6):
            asyncio.run(off.before_execute_tool(ctx, _call("t", args), None, args))
        assert off_events.events == []
        assert off._state == {}, "в режиме off состояние не должно накапливаться"

        # warn
        warn = RepeatGuardHook(
            GatewayRepeatGuardSettings(mode="warn", window_size=10, max_repeats_in_window=2)
        )
        warn_events = _Collector()
        warn._db = warn_events
        ctx = _ctx()
        asyncio.run(warn.before_iteration(ctx))
        for _ in range(6):
            asyncio.run(warn.before_execute_tool(ctx, _call("t", args), None, args))
        assert len(warn_events.events) == 1, "ровно одно событие на crossing"
        assert warn_events.events[0].event_type == "tool.suppressed"
        assert warn_events.events[0].actor == "RepeatGuardHook"

        # block
        blocked = RepeatGuardHook(
            GatewayRepeatGuardSettings(
                mode="block", window_size=10, max_repeats_in_window=2
            )
        )
        block_events = _Collector()
        blocked._db = block_events
        ctx = _ctx()
        asyncio.run(blocked.before_iteration(ctx))
        asyncio.run(blocked.before_execute_tool(ctx, _call("t", args), None, args))
        with pytest.raises(RepeatGuardBlocked) as exc_info:
            for _ in range(5):
                asyncio.run(
                    blocked.before_execute_tool(ctx, _call("t", args), None, args)
                )
        assert str(exc_info.value).startswith("repeat-guard")
        assert len(block_events.events) == 1
        assert block_events.events[0].event_type == "tool.suppressed"

    def test_block_carries_call_context(self) -> None:
        """Исключение несёт контекст — патчу из него нужен tool-результат."""
        hook = RepeatGuardHook(
            GatewayRepeatGuardSettings(mode="block", max_repeats_in_window=2)
        )
        ctx = _ctx("s7")
        call = _call("t", {"v": 1})
        asyncio.run(hook.before_iteration(ctx))
        asyncio.run(hook.before_execute_tool(ctx, call, "tool-obj", {"v": 1}))
        with pytest.raises(RepeatGuardBlocked) as exc_info:
            asyncio.run(hook.before_execute_tool(ctx, call, "tool-obj", {"v": 1}))
        exc = exc_info.value
        assert exc.tool_call is call
        assert exc.tool == "tool-obj"
        assert exc.params == {"v": 1}
        assert exc.context is ctx

    def test_mode_off_is_default(self) -> None:
        from lib.core.project_settings import GatewaySettings

        assert GatewayRepeatGuardSettings().mode == "off"
        assert GatewaySettings().repeat_guard is None


# ---------------------------------------------------------------------------
# 3.5 разные инструменты
# ---------------------------------------------------------------------------


class TestDistinctTools:
    def test_distinct_tools(self) -> None:
        """Одинаковые аргументы у РАЗНЫХ инструментов — не повтор."""
        settings = GatewayRepeatGuardSettings(
            mode="block", window_size=10, max_repeats_in_window=3
        )
        hook = RepeatGuardHook(settings)
        ctx = _ctx()
        asyncio.run(hook.before_iteration(ctx))
        for name in ("tool_a", "tool_b", "tool_a", "tool_b"):
            asyncio.run(
                hook.before_execute_tool(ctx, _call(name, {"x": 1}), None, {"x": 1})
            )
        # Ни одна пара не достигла порога 3 при чередовании: fingerprint —
        # это (tool_name, canonical), а не только аргументы.
        assert not hook._state.get("s1", {}).get("published")


# ---------------------------------------------------------------------------
# 3.6 exempt_tools
# ---------------------------------------------------------------------------


class TestExemptTools:
    def test_exempt_tools(self) -> None:
        # точное совпадение исключает вызов полностью
        hook = RepeatGuardHook(
            GatewayRepeatGuardSettings(
                mode="block", window_size=10, max_repeats_in_window=2,
                exempt_tools=["my_tool"],
            )
        )
        ctx = _ctx()
        asyncio.run(hook.before_iteration(ctx))
        for _ in range(8):
            asyncio.run(
                hook.before_execute_tool(ctx, _call("my_tool", {"x": 1}), None, {"x": 1})
            )
        assert not hook._state.get("s1", {}).get("published")

        # read_file при exempt_tools=["read_*"] падает на старте настроек
        with pytest.raises(Exception) as exc_info:
            GatewayRepeatGuardSettings(exempt_tools=["read_*"])
        assert "read_*" in str(exc_info.value)

        # exempt_tools=["my_tool"] не отключает проверку других инструментов
        hook2 = RepeatGuardHook(
            GatewayRepeatGuardSettings(
                mode="block", window_size=10, max_repeats_in_window=2,
                exempt_tools=["my_tool"],
            )
        )
        ctx = _ctx()
        asyncio.run(hook2.before_iteration(ctx))
        asyncio.run(
            hook2.before_execute_tool(ctx, _call("read_file", {"x": 1}), None, {"x": 1})
        )
        with pytest.raises(RepeatGuardBlocked):
            asyncio.run(
                hook2.before_execute_tool(
                    ctx, _call("read_file", {"x": 1}), None, {"x": 1}
                )
            )

    @pytest.mark.parametrize("pattern", ["read_*", "read?", "read[ab]", "^read", "read$"])
    def test_patterns_are_rejected(self, pattern: str) -> None:
        with pytest.raises(Exception) as exc_info:
            GatewayRepeatGuardSettings(exempt_tools=[pattern])
        message = str(exc_info.value)
        assert pattern in message
        assert "метасимвол" in message


# ---------------------------------------------------------------------------
# 3.7 изоляция между сессиями
# ---------------------------------------------------------------------------


class TestConcurrentSessions:
    def test_concurrent_sessions(self) -> None:
        settings = GatewayRepeatGuardSettings(
            mode="block", window_size=10, max_repeats_in_window=2
        )
        hook = RepeatGuardHook(settings)
        a, b = _ctx("session-a", 1), _ctx("session-b", 1)
        asyncio.run(hook.before_iteration(a))
        asyncio.run(hook.before_iteration(b))

        args = {"x": 1}
        asyncio.run(hook.before_execute_tool(a, _call("t", args), None, args))
        with pytest.raises(RepeatGuardBlocked):
            asyncio.run(hook.before_execute_tool(a, _call("t", args), None, args))

        # В session-b тот же вызов — первый: изоляция state'а
        asyncio.run(hook.before_execute_tool(b, _call("t", args), None, args))
        bucket_b = hook._state["session-b"]
        assert len(bucket_b["calls"]) == 1
        assert not bucket_b["published"]

    def test_new_turn_resets_only_that_session(self) -> None:
        settings = GatewayRepeatGuardSettings(
            mode="warn", window_size=10, max_repeats_in_window=2
        )
        hook = RepeatGuardHook(settings)
        a, b = _ctx("a", 1), _ctx("b", 1)
        asyncio.run(hook.before_iteration(a))
        asyncio.run(hook.before_iteration(b))
        for ctx in (a, b):
            asyncio.run(hook.before_execute_tool(ctx, _call("t", {"x": 1}), None, {"x": 1}))
            asyncio.run(hook.before_execute_tool(ctx, _call("t", {"x": 1}), None, {"x": 1}))
        assert hook._state["a"]["published"]
        assert hook._state["b"]["published"]

        # Новый оборот сессии a (iteration == 0) — буфер a очищен, b цел.
        new_turn = _ctx("a", 0)
        asyncio.run(hook.before_iteration(new_turn))
        assert "a" not in hook._state
        assert hook._state["b"]["published"], "чужая сессия не должна страдать"


# ---------------------------------------------------------------------------
# 3.8 fail-soft
# ---------------------------------------------------------------------------


class TestFailSoft:
    def test_logging_failure(self) -> None:
        """Сбой журнала не ломает детект и не поднимается наружу."""
        for mode in ("warn", "block"):
            hook = RepeatGuardHook(
                GatewayRepeatGuardSettings(mode=mode, max_repeats_in_window=2)
            )
            hook._db = _Collector(boom=True)
            ctx = _ctx(f"s-{mode}")
            asyncio.run(hook.before_iteration(ctx))
            asyncio.run(hook.before_execute_tool(ctx, _call("t", {"x": 1}), None, {"x": 1}))
            if mode == "warn":
                asyncio.run(
                    hook.before_execute_tool(ctx, _call("t", {"x": 1}), None, {"x": 1})
                )
            else:
                with pytest.raises(RepeatGuardBlocked):
                    asyncio.run(
                        hook.before_execute_tool(
                            ctx, _call("t", {"x": 1}), None, {"x": 1}
                        )
                    )

    def test_detects_without_db_service(self) -> None:
        """``db_logging_service=None`` отключает журнал, но не детектор."""
        hook = RepeatGuardHook(
            GatewayRepeatGuardSettings(mode="block", max_repeats_in_window=2)
        )
        ctx = _ctx()
        asyncio.run(hook.before_iteration(ctx))
        asyncio.run(hook.before_execute_tool(ctx, _call("t", {"x": 1}), None, {"x": 1}))
        with pytest.raises(RepeatGuardBlocked):
            asyncio.run(hook.before_execute_tool(ctx, _call("t", {"x": 1}), None, {"x": 1}))

    def test_missing_arguments_do_not_break(self) -> None:
        hook = RepeatGuardHook(GatewayRepeatGuardSettings(mode="warn"))
        ctx = _ctx()
        asyncio.run(hook.before_iteration(ctx))
        asyncio.run(hook.before_execute_tool(ctx, _call("t", None), None, {}))
        asyncio.run(hook.before_execute_tool(ctx, SimpleNamespace(name=""), None, {}))
