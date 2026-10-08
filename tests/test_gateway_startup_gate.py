"""Приёмка: каналы стартуют **после** данных и векторов.

Проверяется не «есть вызов ``gate.prepare``», а наблюдаемый порядок
событий в реальном entrypoint'е ``gateway._entrypoint_main``:

    cache.connect → sync.start → cache.publish → vector.preload → channels

``channels.start_all()`` живёт внутри ``GatewayRunner.run_forever``,
поэтому «каналы поехали» = ``run_forever`` вызван. Если подготовка
случится после него — фича не работает, даже если гейт где-то есть.

Спека: ``openspec/specs/runtime/startup-vector-preload-gate``.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import threading
import time
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from lib.services.startup_gate import (
    StartupGate,
    StartupGateError,
    StartupGateReport,
    ThreadSafeSignal,
    VectorPreloadConfig,
)

_ROOT = Path(__file__).resolve().parent.parent

# «Аргумент не передан» — иначе ``None`` нельзя использовать как
# осмысленный результат («store не готов»).
_UNSET: Any = object()


# ---------------------------------------------------------------------------
# Fakes: пишут в общий журнал порядка
# ---------------------------------------------------------------------------


class _Order:
    def __init__(self) -> None:
        self.events: list[str] = []

    def add(self, name: str) -> None:
        self.events.append(name)

    def index(self, name: str) -> int:
        return self.events.index(name)


class _FakeCacheStore:
    def __init__(self, order: _Order) -> None:
        self._order = order

    def connect(self) -> None:
        self._order.add("cache.connect")

    def upsert_records(self, *args: Any, **kwargs: Any) -> None:
        """Sink синхронизации (gateway вешает его как on_new_records)."""
        self._order.add("cache.upsert")

    def get_stats(self) -> dict[str, Any]:
        return {"publish_path": None}

    def publish(self, force: bool = False) -> None:
        self._order.add("cache.publish")

    def preload_errors(self) -> list[dict[str, Any]]:
        return []


class _FakeSyncService:
    def __init__(self, order: _Order) -> None:
        self._order = order
        self._on_sync = None
        self._on_new_records = None
        self.new_records_callbacks: list[Any] = []

    def set_on_new_records_callback(self, cb: Any) -> None:
        self.upsert_records = cb
        self._on_new_records = cb
        self.new_records_callbacks.append(cb)

    def set_on_sync_callback(self, cb: Any) -> None:
        self._on_sync = cb

    def start(self, initial_load: bool = False) -> None:
        self._order.add("sync.start")

    def stop(self) -> None:
        pass

    def release_signal(self) -> None:
        """Сыграть первый цикл синхронизации (initial_load + publish)."""
        assert self._on_sync is not None, "sync-callback не установлен"
        self._on_sync()


class _FakePreload:
    """Пишет в журнал порядка в момент сборки индексов."""

    def __init__(self, order: _Order, result: Any = _UNSET) -> None:
        self._order = order
        # ``None`` — значимый результат («store не готов»), поэтому
        # дефолт отличается от него через _UNSET.
        self._result: Any = (
            [{"index_name": "pocket_entity_index", "vectors": 900}]
            if result is _UNSET
            else result
        )

    async def preload_vector_indexes(self, store: Any) -> Any:
        self._order.add("vector.preload")
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _ctx(
    order: _Order,
    *,
    preload_result: Any = _UNSET,
    policy: str = "warn",
    await_ready: bool = True,
    enabled: bool = True,
) -> Any:
    ctx = MagicMock()
    ctx.cache_store = _FakeCacheStore(order)
    ctx.sync_service = _FakeSyncService(order)
    ctx.db_logging_service = None
    ctx.background_vector_preload = False
    ctx.startup_gate = StartupGate(
        VectorPreloadConfig(
            enabled=enabled,
            await_ready=await_ready,
            on_unavailable=policy,  # type: ignore[arg-type]
        ),
        preload_service=_FakePreload(order, result=preload_result),
        cache_store=ctx.cache_store,
    )
    return ctx


def _signal_ready(ctx: Any) -> None:
    """Сделать так, чтобы данные «приехали» сразу после ``ctx.start()``.

    Настоящий ``ApplicationContext.start()`` запускает
    ``PgDuckDbSyncService.start(initial_load=True)``; в фейке это должен
    сделать ``ctx.start``, иначе сигнал готовности никогда не придёт и
    подготовка (справедливо) будет ждать вечно.
    """
    def _start(*args: Any, **kwargs: Any) -> None:
        ctx.sync_service.start(initial_load=True)
        ctx.sync_service.release_signal()

    ctx.start = MagicMock(side_effect=_start)


def _install_publish_then_signal(ctx: Any, signal: Any) -> None:
    """Поведение worker-треда синхронизации: сначала снапшот, потом сигнал.

    Нужно тестам, которые гоняют подготовку **без** entrypoint'а: там
    callback ещё не навешен. Порядок «publish → signal» в самом gateway
    проверяется отдельно через ``_run_entrypoint``.
    """
    def _cycle() -> None:
        ctx.cache_store.publish()
        signal.set()

    ctx.sync_service.set_on_sync_callback(_cycle)


def _serving_events(order: _Order) -> list[str]:
    """События до финального снапшота shutdown'а.

    ``_entrypoint_main`` в ``finally`` делает ещё один ``publish()`` — он
    не часть порядка запуска, поэтому отбрасываем хвост.
    """
    events = list(order.events)
    if events and events[-1] == "cache.publish":
        events = events[:-1]
    return events


def _run_entrypoint(ctx: Any, order: _Order) -> None:
    """Прогнать ``_entrypoint_main`` с подменёнными тяжёлыми шагами."""
    import gateway as gw

    args = argparse.Namespace(profile="test", smoke=False)
    runner = MagicMock()

    def _run_forever(run_once: Any) -> None:
        # Каналы поднимаются внутри рабочего цикла — фиксируем, что до
        # этого момента процесс дошёл.
        order.add("channels")

    runner.run_forever.side_effect = _run_forever

    # ``_initialize_settings`` в процессе вызывается один раз (lifecycle
    # gate), а conftest уже инициализировал SETTINGS — в тесте подменяем.
    with patch("config._initialize_settings"), patch(
        "lib.core.application_context.ApplicationContext.create",
        return_value=ctx,
    ), patch(
        "lib.lifecycle.gateway_runner.GatewayRunner", return_value=runner
    ), patch.object(
        gw, "_configure_logging"
    ), patch.object(
        gw, "_report_db_pool_startup"
    ), patch.object(
        gw, "_check_websocket_port_available"
    ), patch.object(
        gw, "_project_version", return_value="0.0.0"
    ):
        gw._entrypoint_main(args, _ROOT, _ROOT / "workspace")


# ---------------------------------------------------------------------------
# Порядок
# ---------------------------------------------------------------------------


class TestStartupOrder:
    def test_channels_start_after_vectors(self) -> None:
        """Главный контракт: сборка векторов завершена ДО старта каналов."""
        order = _Order()
        ctx = _ctx(order)
        _signal_ready(ctx)

        _run_entrypoint(ctx, order)

        assert _serving_events(order) == [
            "cache.connect",
            "sync.start",
            "cache.publish",
            "vector.preload",
            "channels",
        ], f"порядок старта нарушен: {order.events}"

    def test_publish_happens_before_the_cache_signal(self) -> None:
        """«Данные готовы» = и в памяти, и снапшот на диске.

        Сигнал ставится до ``publish()`` — тогда фаза «данные готовы»
        означала бы «данные только начали публиковаться».
        """
        order = _Order()
        ctx = _ctx(order)
        _signal_ready(ctx)

        _run_entrypoint(ctx, order)

        assert order.index("cache.publish") < order.index("vector.preload")

    def test_signal_from_worker_thread_wakes_the_waiter(self) -> None:
        """Регрессия «без таймаутов»: сигнал из чужого потока будит loop.

        Наивный ``asyncio.Event.set()`` из worker-треда loop не будит, а
        без таймаута ожидание осталось бы висеть навсегда — уже после
        того, как данные загрузились.
        """
        order = _Order()
        ctx = _ctx(order)

        import gateway as gw

        signal = ThreadSafeSignal()
        finished = threading.Event()
        _install_publish_then_signal(ctx, signal)

        def _prepare() -> None:
            gw._run_startup_preparation(ctx, signal)
            finished.set()

        worker = threading.Thread(target=_prepare, daemon=True)
        worker.start()

        # Имитируем worker-тред PgDuckDbSyncService: снапшот, затем сигнал.
        ctx.sync_service.release_signal()
        worker.join(timeout=10.0)

        assert finished.is_set() is True, (
            "сигнал из другого потока не разбудил ожидание"
        )
        assert order.index("cache.publish") < order.index("vector.preload")

    def test_preparation_blocks_until_signal(self) -> None:
        """Пока сигнала нет, векторы не собираются (и процесс ждёт)."""
        order = _Order()
        ctx = _ctx(order)

        import gateway as gw

        signal = ThreadSafeSignal()
        observed: list[str] = []
        _install_publish_then_signal(ctx, signal)

        async def _drive() -> None:
            task = asyncio.create_task(
                gw._run_startup_preparation_async(ctx, signal)
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            observed.append("returned" if task.done() else "waiting")
            ctx.sync_service.release_signal()
            await task

        asyncio.run(_drive())

        assert observed == ["waiting"]
        assert order.events == ["cache.publish", "vector.preload"]


# ---------------------------------------------------------------------------
# Конфигурационные режимы
# ---------------------------------------------------------------------------


class TestPreparationModes:
    def test_disabled_gate_does_not_preload(self) -> None:
        order = _Order()
        ctx = _ctx(order, enabled=False)

        import gateway as gw

        gw._run_startup_preparation(ctx, ThreadSafeSignal())
        assert order.events == []

    def test_await_ready_false_defers_to_background(self) -> None:
        """Явный отказ от ожидания: каналы стартуют, индексы — в фоне."""
        order = _Order()
        ctx = _ctx(order, await_ready=False)

        import gateway as gw

        gw._run_startup_preparation(ctx, ThreadSafeSignal())
        assert order.events == []  # синхронно ничего не собрано
        assert ctx.background_vector_preload is True

    def test_no_sync_service_skips_preparation(self) -> None:
        """Аудит выключен: готовить нечего, entrypoint не должен висеть."""
        order = _Order()
        ctx = _ctx(order)
        ctx.sync_service = None

        import gateway as gw

        gw._run_startup_preparation(ctx, ThreadSafeSignal())
        assert order.events == []

    def test_disabled_gate_still_starts_channels(self) -> None:
        """Выключенный прогрев не блокирует приём вопросов."""
        order = _Order()
        ctx = _ctx(order, enabled=False)
        _signal_ready(ctx)

        _run_entrypoint(ctx, order)

        assert _serving_events(order)[-1] == "channels"
        assert "vector.preload" not in order.events


# ---------------------------------------------------------------------------
# Политика отказа
# ---------------------------------------------------------------------------


class TestUnavailablePolicy:
    def test_warn_policy_keeps_going(self) -> None:
        """Дефолт: недоступные векторы не блокируют старт каналов."""
        order = _Order()
        ctx = _ctx(order, preload_result=None, policy="warn")
        _signal_ready(ctx)

        _run_entrypoint(ctx, order)

        assert ctx.startup_gate.report is not None
        assert ctx.startup_gate.report.ok is False
        assert _serving_events(order)[-1] == "channels"

    def test_fail_policy_raises_before_channels(self) -> None:
        order = _Order()
        ctx = _ctx(order, preload_result=None, policy="fail")

        import gateway as gw

        signal = ThreadSafeSignal()
        signal.set()

        async def _drive() -> None:
            with pytest.raises(StartupGateError) as exc_info:
                await gw._run_startup_preparation_async(ctx, signal)
            assert isinstance(exc_info.value.report, StartupGateReport)

        asyncio.run(_drive())
        assert order.events == ["vector.preload"]

    def test_fail_policy_blocks_entrypoint(self) -> None:
        """При ``fail`` entrypoint не доходит до ``run_forever``."""
        order = _Order()
        ctx = _ctx(order, preload_result=None, policy="fail")
        _signal_ready(ctx)

        import gateway as gw

        with patch("lib.lifecycle.gateway_runner.GatewayRunner") as runner_cls:
            with pytest.raises(StartupGateError):
                _run_entrypoint(ctx, order)

        runner_cls.return_value.run_forever.assert_not_called()
        assert "channels" not in order.events

    def test_build_error_is_also_fail(self) -> None:
        """Исключение при сборке — такой же отказ, как пустой store."""
        order = _Order()
        ctx = _ctx(
            order,
            preload_result=RuntimeError("faiss exploded"),
            policy="fail",
        )

        import gateway as gw

        signal = ThreadSafeSignal()
        signal.set()

        async def _drive() -> None:
            with pytest.raises(StartupGateError):
                await gw._run_startup_preparation_async(ctx, signal)

        asyncio.run(_drive())
        assert "channels" not in order.events


class TestMainBoundary:
    def test_entrypoint_keeps_pk_aware_upsert_callback(self) -> None:
        """Регрессия: gateway MUST NOT затирать колбэк upsert.

        ``_make_sync_services`` вешает PK-aware обёртку
        (``key_column=sync.key_column_for(table)``). Если caller переустановит
        колбэк на голый ``cache_store.upsert_records``, ``key_column`` снова
        становится ``None``, и таблицы с PK не ``id``
        (``public.agent_predefined_scripts`` → ``name``) уходят в ветку
        пересоздания — а батчи от ``_fetch_incremental`` являются дельтой,
        поэтому несвязанные строки молча теряются.
        """
        order = _Order()
        ctx = _ctx(order)
        _signal_ready(ctx)

        pk_aware_calls: list[tuple[str, list[dict], str | None]] = []

        def _pk_aware_upsert(table: str, records: Any, key_column: Any = None) -> None:
            """То, что ставит composition root (``_upsert_with_pk``)."""
            pk_aware_calls.append((table, list(records), key_column))
            order.add("cache.upsert")

        # Начальное состояние: PK-aware обёртка уже выставлена composition root'ом.
        ctx.sync_service.set_on_new_records_callback(_pk_aware_upsert)
        before = list(ctx.sync_service.new_records_callbacks)

        _run_entrypoint(ctx, order)

        assert ctx.sync_service.new_records_callbacks == before, (
            "entrypoint переустановил колбэк upsert и откатил PK-aware обёртку "
            "на key_column=None"
        )
        assert ctx.sync_service._on_new_records is _pk_aware_upsert

    def test_startup_gate_error_becomes_exit_2(self) -> None:
        """Startup-boundary: ``StartupGateError`` → FATAL + exit 2.

        Не рестарт по backoff: каналы не подняты, повторный цикл лишь
        повторил бы ту же ошибку в лог.
        """
        import gateway as gw

        report = StartupGateReport(
            phase="unavailable",
            ok=False,
            detail="vector index build failed: boom",
            policy="fail",
        )

        def _fake_entrypoint(args: Any, script_dir: Any, ws: Any) -> None:
            raise StartupGateError(report)

        fake_stderr = io.StringIO()
        with patch.object(gw, "_entrypoint_main", _fake_entrypoint), \
             redirect_stderr(fake_stderr):
            rc = gw.main(["--profile=test"])

        assert rc == 2
        err = fake_stderr.getvalue()
        assert "FATAL" in err
        assert "vector index build failed: boom" in err