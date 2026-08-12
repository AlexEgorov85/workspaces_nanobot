from __future__ import annotations

from unittest.mock import MagicMock

from lib.lifecycle.shutdown_coordinator import ShutdownCoordinator


class TestShutdownOrder:
    def test_lifo_order(self):
        order = []
        coord = ShutdownCoordinator()
        for name in ["a", "b", "c"]:
            coord.register(name, lambda n=name: order.append(n))
        coord.shutdown_all()
        assert order == ["c", "b", "a"]

    def test_errors_isolated(self):
        coord = ShutdownCoordinator()
        coord.register("first", lambda: None)
        coord.register("boom", MagicMock(side_effect=RuntimeError("x")))
        coord.register("third", lambda: order.append("third"))
        order = []
        coord.shutdown_all()
        assert order == ["third"]  # продолжает выполнять после ошибки

    def test_clear(self):
        coord = ShutdownCoordinator()
        coord.register("a", lambda: None)
        coord.clear()
        coord.shutdown_all()  # ничего не должно делать


class TestResolveStopFn:
    def test_callable(self):
        from lib.lifecycle.shutdown_coordinator import _resolve_stop_fn

        called = []
        fn = _resolve_stop_fn(lambda: called.append("x"))
        fn()
        assert called == ["x"]

    def test_method_close(self):
        from lib.lifecycle.shutdown_coordinator import _resolve_stop_fn

        obj = MagicMock()
        fn = _resolve_stop_fn(obj)
        fn()
        obj.close.assert_called_once()

    def test_method_stop(self):
        from lib.lifecycle.shutdown_coordinator import _resolve_stop_fn
        obj = MagicMock(spec=["stop"])
        fn = _resolve_stop_fn(obj)
        fn()
        obj.stop.assert_called_once()

    def test_raises_when_no_method(self):
        from lib.lifecycle.shutdown_coordinator import _resolve_stop_fn

        with __import__("pytest").raises(TypeError):
            _resolve_stop_fn(object())
