"""``StartupGate``: контракт порядка «данные → векторы → каналы».

Гейт — единственное место, где решается, можно ли уже принимать
вопросы. Тесты закрывают три вещи:

1. **Порядок и явность.** ``wait_for_cache`` возвращается только по
   сигналу готовности данных, ``load_vectors`` — по завершению сборки
   индексов. Никаких «подождал N секунд и пошёл дальше».
2. **Политика отказа.** ``on_unavailable="warn"`` — отчёт и degraded
   старт; ``on_unavailable="fail"`` — ``StartupGateError``.
3. **Отсутствие таймаутов как конструкции** (guard по исходнику): это
   требование владельца фичи, и вернуться к ``wait_for``/``sleep``
   здесь нельзя молча.

Спека: ``openspec/specs/runtime/startup-vector-preload-gate``.
"""

from __future__ import annotations

import asyncio
import ast
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from lib.services.startup_gate import (
    STARTUP_PRELOAD_EVENT,
    StartupGate,
    StartupGateError,
    StartupGateReport,
    VectorPreloadConfig,
    read_vector_preload_config,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

# Явный «объект не передан» — иначе нельзя протестировать ветку
# «сервиса нет», передав None (None означает «сервис создан, но пуст»).
_UNSET: Any = object()


class _FakeCacheStore:
    """Минимальный duck-type ``CacheProvider`` для гейта."""

    def __init__(self, *, errors: list[dict[str, Any]] | None = None) -> None:
        self.errors = errors or []

    def preload_errors(self) -> list[dict[str, Any]]:
        return list(self.errors)


class _FakePreload:
    """Подменяет ``PreloadService.preload_vector_indexes``."""

    def __init__(self, result: Any = _UNSET, raises: Exception | None = None,
                 delay: float = 0.0) -> None:
        self.result: Any = (
            [{"index_name": "pocket_entity_index", "vectors": 1200}]
            if result is _UNSET
            else result
        )
        self.raises = raises
        self.delay = delay
        self.calls = 0

    async def preload_vector_indexes(self, store: Any) -> Any:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raises is not None:
            raise self.raises
        return self.result


def _gate(
    *,
    preload: Any = _UNSET,
    store: Any = _UNSET,
    enabled: bool = True,
    await_ready: bool = True,
    policy: str = "warn",
    on_report: Any = None,
) -> StartupGate:
    return StartupGate(
        VectorPreloadConfig(
            enabled=enabled,
            await_ready=await_ready,
            on_unavailable=policy,  # type: ignore[arg-type]
        ),
        preload_service=(
            _FakePreload() if preload is _UNSET else preload
        ),
        cache_store=(
            _FakeCacheStore() if store is _UNSET else store
        ),
        on_report=on_report,
    )


# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------


class TestReadVectorPreloadConfig:
    def test_defaults_when_section_absent(self) -> None:
        cfg = read_vector_preload_config({})
        assert cfg.enabled is True
        assert cfg.await_ready is True
        assert cfg.on_unavailable == "warn"

    def test_reads_explicit_values(self) -> None:
        cfg = read_vector_preload_config(
            {
                "gateway": {
                    "startup": {
                        "vector_preload": {
                            "enabled": False,
                            "await_ready": False,
                            "on_unavailable": "fail",
                        }
                    }
                }
            }
        )
        assert cfg.enabled is False
        assert cfg.await_ready is False
        assert cfg.on_unavailable == "fail"

    def test_unknown_policy_falls_back_to_warn(self) -> None:
        """Мусор в конфиге не должен молча включать fail-fast старт."""
        cfg = read_vector_preload_config(
            {
                "gateway": {
                    "startup": {"vector_preload": {"on_unavailable": "block"}}
                }
            }
        )
        assert cfg.on_unavailable == "warn"

    def test_non_dict_section_falls_back_to_defaults(self) -> None:
        cfg = read_vector_preload_config(
            {"gateway": {"startup": {"vector_preload": "yes"}}}
        )
        assert cfg.enabled is True
        assert cfg.await_ready is True

    def test_settings_without_gateway_key(self) -> None:
        assert read_vector_preload_config({"channels": {}}).enabled is True

    def test_project_json_section_is_valid(self) -> None:
        """Секция из project.json проходит и pydantic, и проекцию гейта."""
        from lib.core.project_settings import validate_project_settings

        settings = {
            "gateway": {
                "startup": {
                    "vector_preload": {
                        "enabled": True,
                        "await_ready": True,
                        "on_unavailable": "fail",
                    }
                }
            }
        }
        validated = validate_project_settings(settings)
        assert validated.gateway.startup.vector_preload.on_unavailable == "fail"
        assert read_vector_preload_config(settings).on_unavailable == "fail"

    def test_project_settings_rejects_unknown_policy(self) -> None:
        from config import ConfigurationError
        from lib.core.project_settings import validate_project_settings

        with pytest.raises(ConfigurationError):
            validate_project_settings(
                {
                    "gateway": {
                        "startup": {"vector_preload": {"on_unavailable": "later"}}
                    }
                }
            )


# ---------------------------------------------------------------------------
# Шаг 1: данные
# ---------------------------------------------------------------------------


class TestWaitForCache:
    async def test_waits_for_signal_then_reports_cache_ready(self) -> None:
        """Гейт не возвращается, пока сигнал загрузки данных не выставлен."""
        gate = _gate()
        event = asyncio.Event()

        async def _release_signal() -> None:
            await asyncio.sleep(0)
            event.set()

        asyncio.create_task(_release_signal())
        assert gate.phase == "pending"
        assert gate.is_awaiting() is True

        assert await gate.wait_for_cache(event) is True
        assert gate.phase == "cache_ready"

    async def test_does_not_proceed_while_signal_pending(self) -> None:
        """Пока данных нет, векторы не собираются."""
        preload = _FakePreload()
        gate = _gate(preload=preload)
        event = asyncio.Event()  # никогда не выставляется

        task = asyncio.create_task(gate.wait_for_cache(event))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert task.done() is False
        assert preload.calls == 0  # preload не трогали — данных ещё нет

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_disabled_gate_skips_waiting(self) -> None:
        preload = _FakePreload()
        gate = _gate(preload=preload, enabled=False)

        assert await gate.wait_for_cache(asyncio.Event()) is False
        assert gate.phase == "skipped"
        assert preload.calls == 0

    async def test_no_sync_service_means_nothing_to_wait(self) -> None:
        """Аудит выключен → сервиса синхронизации нет, ждать нечего."""
        gate = _gate()

        assert await gate.wait_for_cache(None) is True
        assert gate.phase == "pending"  # не выдумываем фазу: события не было


# ---------------------------------------------------------------------------
# Шаг 2: векторы
# ---------------------------------------------------------------------------


class TestLoadVectors:
    async def test_ready_reports_vectors_ready(self) -> None:
        gate = _gate()
        report = await gate.load_vectors()

        assert isinstance(report, StartupGateReport)
        assert report.ok is True
        assert report.phase == "vectors_ready"
        assert [i["index_name"] for i in report.loaded] == [
            "pocket_entity_index"
        ]
        assert gate.is_awaiting() is False

    async def test_store_not_ready_is_a_failure_not_empty_cache(self) -> None:
        """``preload`` вернул ``None`` — store не готов, это отказ."""
        gate = _gate(preload=_FakePreload(result=None))

        report = await gate.load_vectors()
        assert report.ok is False
        assert report.phase == "unavailable"
        assert "not ready" in report.detail

    async def test_build_error_is_reported_not_raised(self) -> None:
        gate = _gate(
            preload=_FakePreload(raises=RuntimeError("faiss exploded")),
        )

        report = await gate.load_vectors()
        assert report.ok is False
        assert "faiss exploded" in report.detail
        assert report.errors == [] or report.errors is not None

    async def test_index_errors_are_carried_into_report(self) -> None:
        store = _FakeCacheStore(
            errors=[{"index_name": "audit_entity_index", "error": "empty table"}]
        )
        gate = _gate(store=store)

        report = await gate.load_vectors()
        assert report.errors == store.errors

    async def test_no_cache_store_is_skipped(self) -> None:
        gate = _gate(store=None)
        report = await gate.load_vectors()
        assert report.phase == "skipped"
        assert report.ok is True

    async def test_no_preload_service_is_skipped(self) -> None:
        gate = _gate(preload=None)
        report = await gate.load_vectors()
        assert report.phase == "skipped"
        assert report.ok is True

    async def test_fail_policy_raises_with_report_attached(self) -> None:
        gate = _gate(preload=_FakePreload(result=None), policy="fail")

        with pytest.raises(StartupGateError) as exc_info:
            await gate.load_vectors()

        report = exc_info.value.report
        assert report.ok is False
        assert report.phase == "unavailable"
        assert report.policy == "fail"

    async def test_fail_policy_raises_on_build_error(self) -> None:
        gate = _gate(
            preload=_FakePreload(raises=ValueError("bad dim")),
            policy="fail",
        )
        with pytest.raises(StartupGateError):
            await gate.load_vectors()

    async def test_warn_policy_does_not_raise(self) -> None:
        gate = _gate(preload=_FakePreload(result=None), policy="warn")
        report = await gate.load_vectors()
        assert report.ok is False
        assert report.policy == "warn"

    async def test_prepare_composes_both_steps(self) -> None:
        """``prepare`` = сигнал данных + векторы, в том же порядке."""
        preload = _FakePreload()
        gate = _gate(preload=preload)
        event = asyncio.Event()
        event.set()

        report = await gate.prepare(event)
        assert report.ok is True
        assert preload.calls == 1


# ---------------------------------------------------------------------------
# Наблюдаемость: событие в БД + callback
# ---------------------------------------------------------------------------


class TestObservability:
    async def test_writes_startup_event_with_phase(self) -> None:
        gate = _gate()
        with patch(
            "lib.services.db_logging_service.try_log_event"
        ) as try_log_event:
            await gate.load_vectors()

        assert try_log_event.called
        args, kwargs = try_log_event.call_args
        event = args[1]
        assert event.event_type == STARTUP_PRELOAD_EVENT
        assert event.payload["phase"] == "vectors_ready"
        assert event.payload["ok"] is True
        assert kwargs["producer"] == "StartupGate"

    async def test_failed_startup_is_logged_as_warning(self) -> None:
        gate = _gate(preload=_FakePreload(result=None))
        with patch(
            "lib.services.db_logging_service.try_log_event"
        ) as try_log_event:
            await gate.load_vectors()

        event = try_log_event.call_args[0][1]
        assert event.level == "WARN"
        assert event.payload["ok"] is False

    async def test_on_report_callback_receives_report(self) -> None:
        seen: list[StartupGateReport] = []
        gate = _gate(on_report=seen.append)

        await gate.load_vectors()
        assert len(seen) == 1
        assert seen[0].ok is True

    async def test_broken_callback_does_not_break_preparation(self) -> None:
        def _boom(_report: Any) -> None:
            raise RuntimeError("callback down")

        gate = _gate(on_report=_boom)
        report = await gate.load_vectors()
        assert report.ok is True

    async def test_absent_db_logging_service_is_not_an_error(self) -> None:
        gate = _gate()
        report = await gate.load_vectors()
        assert report.ok is True


# ---------------------------------------------------------------------------
# Guard: никаких таймаутов
# ---------------------------------------------------------------------------


def _dotted_name(node: ast.AST) -> str:
    """``asyncio.wait_for`` → ``asyncio.wait_for``; иначе ``repr``-заглушка."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


# Конструкции, означающие «подожду и пойду дальше» вместо «жду сигнала».
_FORBIDDEN_CALLS = {
    "asyncio.wait_for",
    "asyncio.sleep",
    "asyncio.timeout",
    "time.sleep",
}


def _forbidden_constructs(source: str, *, only_function: str | None = None) -> set[str]:
    """Найти вызовы-таймауты и ``timeout=``-аргументы (по AST, не по тексту).

    Проверка по подстроке дала бы ложное срабатывание на имя метода
    ``wait_for_cache`` — то есть проверяла бы не код, а текст.
    """
    tree = ast.parse(source)
    if only_function is not None:
        func = next(
            (
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == only_function
            ),
            None,
        )
        assert func is not None, f"функция {only_function} не найдена"
        tree = ast.parse(ast.unparse(func))  # тело функции без docstring-модуля
        hits: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = _dotted_name(node.func)
                if name in _FORBIDDEN_CALLS:
                    hits.add(name)
                for kw in node.keywords:
                    if kw.arg and kw.arg.startswith("timeout"):
                        hits.add(f"kwarg:{kw.arg}")
        return hits

    hits = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _dotted_name(node.func)
            if name in _FORBIDDEN_CALLS:
                hits.add(name)
            for kw in node.keywords:
                if kw.arg and kw.arg.startswith("timeout"):
                    hits.add(f"kwarg:{kw.arg}")
    return hits


class TestNoTimeoutsInGate:
    """Порядок держится на сигнале, а не на истечении времени.

    Возврат к ``wait_for``/``sleep`` здесь означал бы «незаметно стартовать
    сломанным» — ровно то, ради чего фича делалась.
    """

    _ROOT = Path(__file__).resolve().parent.parent
    _GATE_SOURCE = _ROOT / "lib" / "services" / "startup_gate.py"

    def test_gate_module_has_no_timeout_constructs(self) -> None:
        hits = _forbidden_constructs(
            self._GATE_SOURCE.read_text(encoding="utf-8")
        )
        assert hits == set(), f"startup_gate.py: найдены таймауты {hits}"

    def test_gateway_preparation_has_no_timeout_constructs(self) -> None:
        hits = _forbidden_constructs(
            (self._ROOT / "gateway.py").read_text(encoding="utf-8"),
            only_function="_run_startup_preparation",
        )
        assert hits == set(), (
            f"_run_startup_preparation: найдены таймауты {hits}"
        )

    def test_guard_actually_catches_a_planted_timeout(self) -> None:
        """Проба детектора: подсаженный ``wait_for`` обязан найтись.

        Без этой проверки guard, который молча ничего не ловит, выглядел
        бы как работающая защита.
        """
        planted = (
            "async def prep(event):\n"
            "    try:\n"
            "        await asyncio.wait_for(event.wait(), timeout=30.0)\n"
            "    except asyncio.TimeoutError:\n"
            "        pass\n"
        )
        hits = _forbidden_constructs(planted)
        assert "asyncio.wait_for" in hits
        assert "kwarg:timeout" in hits

    def test_guard_ignores_documented_wait_for_cache_name(self) -> None:
        """Имя метода ``wait_for_cache`` — не таймаут (проверка по тексту врала)."""
        planted = (
            "async def prep(gate, event):\n"
            "    await gate.wait_for_cache(event)\n"
            "    return await gate.load_vectors()\n"
        )
        assert _forbidden_constructs(planted) == set()