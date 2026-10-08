"""Контракт ``ApplicationContext.skip_data_load`` / ``_start_sync_or_skip``.

``gateway.py`` выставляет флаг, когда снапшот ``cache.duckdb`` моложе
``gateway.cache.reuse_ttl_hours`` и данные уже лежат на диске. Тогда
синхронизатор PG → DuckDB MUST NOT стартовать.

Почему «не запускать вовсе», а не «запустить без ``initial_load``»:
poll-цикл с пустым ``_last_sync`` на первом же такте делает полный
``_fetch_all`` — то есть «пропуск» не был бы пропуском, а лишь переставил
бы ту же полную вычитку. Плюс воркеры синка занимали бы пул БД, конкурируя
с каналом вопросов.

Проверяется реальный метод ``ApplicationContext._start_sync_or_skip``:
проверка «синк не стартовал» на подменённом ctx ничего бы не доказывала —
там ``start()`` целиком вообще не вызывается.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from lib.core.application_context import ApplicationContext


class _FakeSync:
    def __init__(self) -> None:
        self.started_with: list[bool] = []

    def start(self, initial_load: bool = False) -> None:
        self.started_with.append(initial_load)

    def stop(self, *args: Any, **kwargs: Any) -> None:
        pass


def _ctx(sync: Any, *, skip: bool = False) -> ApplicationContext:
    """Изолированный экземпляр: проверяется ровно одна ветка ``start()``.

    ``object.__new__`` вместо ``ApplicationContext(...)`` — конструктор
    поднимает пул БД, schema validation и каналы, а здесь нужен только
    ``_start_sync_or_skip``.
    """
    ctx = object.__new__(ApplicationContext)
    ctx.sync_service = sync
    ctx.skip_data_load = skip
    ctx._shutdown = MagicMock()
    return ctx


class TestStartSyncOrSkip:
    def test_sync_started_normally_by_default(self) -> None:
        """Без флага поведение прежнее: ``start(initial_load=True)``."""
        sync = _FakeSync()
        ctx = _ctx(sync)

        ctx._start_sync_or_skip()

        assert sync.started_with == [True]
        ctx._shutdown.register.assert_called_once_with("sync_service", sync)

    def test_sync_not_started_when_flag_set(self) -> None:
        """Флаг выставлен → синк не стартует и не идёт в shutdown."""
        sync = _FakeSync()
        ctx = _ctx(sync, skip=True)

        ctx._start_sync_or_skip()

        assert sync.started_with == [], "синк не должен стартовать при skip_data_load"
        assert ctx._shutdown.register.call_count == 0, (
            "незапущенный сервис не должен регистрироваться в shutdown"
        )

    def test_no_sync_service_is_noop(self) -> None:
        """Аудит выключен — ветка не должна ничего делать и не падать."""
        ctx = _ctx(None, skip=False)
        ctx._start_sync_or_skip()
        assert ctx._shutdown.register.call_count == 0

    def test_no_sync_service_is_noop_even_with_flag(self) -> None:
        ctx = _ctx(None, skip=True)
        ctx._start_sync_or_skip()
        assert ctx._shutdown.register.call_count == 0

    def test_start_failure_does_not_propagate(self) -> None:
        """Падение старта синка не должно ронять весь startup."""
        sync = MagicMock()
        sync.start.side_effect = RuntimeError("boom")
        ctx = _ctx(sync)

        ctx._start_sync_or_skip()  # не должно бросить

    def test_flag_default_is_false(self) -> None:
        """Новый атрибут не ломает прежний путь: дефолт — грузим данные."""
        ctx = _ctx(_FakeSync())
        assert ctx.skip_data_load is False


@pytest.mark.parametrize("skip", [True, False])
def test_start_calls_the_decision_method(
    skip: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``start()`` обязан delegate'ить в ``_start_sync_or_skip``.

    Иначе флаг объявлен, задокументирован и тихо не учитывается — самый
    неприятный вид дефекта: настройка есть, эффекта нет.
    """
    calls: list[int] = []
    monkeypatch.setattr(
        ApplicationContext, "_start_sync_or_skip", lambda self: calls.append(1)
    )
    # ОСТОРОЖНО: реальный ``start()`` применяет переопределения шаблонов к
    # ЖИВОМУ каталогу ``workspace/overrides`` и глобально меняет loader
    # Jinja. Из-за этого ``tests/test_consolidator_locale.py::
    # test_missing_dir_is_noop`` падал в полном прогоне (в одиночку —
    # проходил), хотя его код не менялся. Тест обязан быть чистым по
    # глобальному состоянию, иначе он чинит чужой тест задом наперёд.
    monkeypatch.setattr(
        "lib.services.consolidator_locale.apply_template_overrides",
        lambda *a, **k: False,
    )
    ctx = _ctx(_FakeSync(), skip=skip)
    ctx._started = False
    ctx.runtime_health = None
    ctx.bus = MagicMock()
    ctx.db_logging_service = None
    ctx.session_cold_sync_service = None
    ctx.runtime_events_subscriber = None
    ctx.runtime_readiness = None

    # Остальные тяжёлые шаги ``start()`` заглушаем: проверяется только факт
    # вызова метода решения.
    monkeypatch.setattr(
        "lib.core.application_context._start_db_pool", lambda: None
    )
    monkeypatch.setattr(
        ApplicationContext, "_validate_runtime_schema", lambda self: None
    )

    ctx.start()

    assert calls == [1], "start() не вызвал _start_sync_or_skip"