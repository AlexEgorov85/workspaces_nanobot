"""Тесты ``lib/services/runtime_health.py``.

Health / Readiness — operational status. Различает liveness (пульс
процесса) и readiness (готовность к обработке задач с учётом
зависимостей).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


class TestRuntimeHealth:
    """``RuntimeHealth`` — liveness процесса."""

    def test_initial_state_not_alive(self):
        from lib.services.runtime_health import RuntimeHealth

        h = RuntimeHealth()
        assert h.is_alive() is False
        assert h.status() == "DEAD"

    def test_mark_started_makes_alive(self):
        from lib.services.runtime_health import RuntimeHealth

        h = RuntimeHealth()
        h.mark_started()
        assert h.is_alive() is True
        assert h.status() == "ALIVE"

    def test_mark_stopped_makes_dead(self):
        from lib.services.runtime_health import RuntimeHealth

        h = RuntimeHealth()
        h.mark_started()
        h.mark_stopped()
        assert h.is_alive() is False
        assert h.status() == "DEAD"


class TestRuntimeReadiness:
    """``RuntimeReadiness`` — проверка зависимостей."""

    def test_no_checks_is_ready(self):
        from lib.services.runtime_health import RuntimeReadiness

        r = RuntimeReadiness()
        report = r.check()
        assert report.status == "READY"
        assert report.components == ()

    def test_all_up_required_is_ready(self):
        from lib.services.runtime_health import (
            ComponentStatus,
            RuntimeReadiness,
        )

        r = RuntimeReadiness()
        r.register("postgres", lambda: ComponentStatus(
            name="postgres", required=True, status="UP",
        ))
        r.register("duckdb", lambda: ComponentStatus(
            name="duckdb", required=True, status="UP",
        ))
        report = r.check()
        assert report.status == "READY"
        assert len(report.components) == 2

    def test_required_down_is_not_ready(self):
        from lib.services.runtime_health import (
            ComponentStatus,
            RuntimeReadiness,
        )

        r = RuntimeReadiness()
        r.register("postgres", lambda: ComponentStatus(
            name="postgres", required=True, status="DOWN",
            detail="connection refused",
        ))
        report = r.check()
        assert report.status == "NOT_READY"

    def test_optional_down_with_required_up_is_degraded(self):
        from lib.services.runtime_health import (
            ComponentStatus,
            RuntimeReadiness,
        )

        r = RuntimeReadiness()
        r.register("postgres", lambda: ComponentStatus(
            name="postgres", required=True, status="UP",
        ))
        r.register("vector_search", lambda: ComponentStatus(
            name="vector_search", required=False, status="DOWN",
            detail="no faiss index",
        ))
        report = r.check()
        assert report.status == "DEGRADED"

    def test_lambda_returning_none_means_up(self):
        from lib.services.runtime_health import RuntimeReadiness

        r = RuntimeReadiness()
        r.register("postgres", lambda: None)
        report = r.check()
        assert report.status == "READY"
        assert report.components[0].status == "UP"

    def test_check_swallows_exceptions_in_probe(self):
        from lib.services.runtime_health import RuntimeReadiness

        r = RuntimeReadiness()
        r.register("broken", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        report = r.check()
        assert report.status == "NOT_READY"
        assert report.components[0].status == "DOWN"
        assert "boom" in report.components[0].detail

    def test_register_invalid_return_type_raises(self):
        from lib.services.runtime_health import RuntimeReadiness

        r = RuntimeReadiness()
        r.register("bad", lambda: "not a ComponentStatus")
        with pytest.raises(TypeError, match="bad"):
            r.check()

    def test_to_dict_round_trip(self):
        from lib.services.runtime_health import (
            ComponentStatus,
            RuntimeReadiness,
        )

        r = RuntimeReadiness()
        r.register("postgres", lambda: ComponentStatus(
            name="postgres", required=True, status="UP", detail="3 workers",
        ))
        report = r.check()
        d = report.to_dict()
        assert d["status"] == "READY"
        assert d["components"][0]["name"] == "postgres"
        assert d["components"][0]["detail"] == "3 workers"


class TestComputeOverallStatus:
    """``compute_overall_status`` — агрегатор статусов."""

    def test_empty_is_ready(self):
        from lib.services.runtime_health import compute_overall_status

        assert compute_overall_status([]) == "READY"

    def test_only_optional_down_is_degraded(self):
        from lib.services.runtime_health import (
            ComponentStatus,
            compute_overall_status,
        )

        components = [
            ComponentStatus(name="postgres", required=True, status="UP"),
            ComponentStatus(name="vector", required=False, status="DOWN"),
        ]
        assert compute_overall_status(components) == "DEGRADED"

    def test_required_down_takes_priority(self):
        from lib.services.runtime_health import (
            ComponentStatus,
            compute_overall_status,
        )

        components = [
            ComponentStatus(name="postgres", required=True, status="DOWN"),
            ComponentStatus(name="vector", required=False, status="DOWN"),
        ]
        assert compute_overall_status(components) == "NOT_READY"


class TestRegisterReadinessChecks:
    """Реальная проводка ``_register_readiness_checks``.

    Регрессия: гейт по имени класса менеджера сессий
    (``"PG" in cls or "Postgres" in cls``) не срабатывал НИКОГДА —
    ``build_session_manager`` возвращает библиотечный ``SessionManager``,
    не подкласс. Поэтому рабочая система рапортовала NOT_READY. Здоровье
    обязано определяться пингом по пулу, а required-ness — конфигом.
    """

    @staticmethod
    def _ctx(*, channel_on=True, dsn="postgresql://u:p@h:5432/d", storage_mode="file"):
        from lib.services.runtime_health import RuntimeReadiness

        ctx = MagicMock()
        ctx.runtime_readiness = RuntimeReadiness()
        ctx.storage_mode = storage_mode
        ctx.settings = {
            "channels": {"postgres": {"enabled": channel_on, "dsn": dsn}}
        }
        # Класс БЕЗ ``PG``/``Postgres`` в имени — ровно то, что создаёт
        # ``build_session_manager``. Старый гейт считал это деградацией.
        ctx.session_manager = object()
        return ctx

    @staticmethod
    def _patch_pool(monkeypatch, *, mode: str):
        """Подменить ``lib.utils.db._get_manager`` под нужный сценарий.

        Модуль грузится по файловому пути и кладётся в ``sys.modules`` под
        именем ``lib.utils.db`` на время теста: часть существующих тестов
        подменяет ``sys.modules["lib.utils.db"]`` голым ``ModuleType`` и НЕ
        восстанавливает его, поэтому брать модуль из ambient-состояния
        нельзя — он может оказаться чужой заглушкой без ``_get_manager``.

        Контракт повторяет реальный ``DBManager._submit``: возвращается
        ``_JobResult`` (у него ``.get``), а НЕ ``_Job``. Мок на ``_Job``
        воспроизводил бы баг ``_submit(...).result`` вместо контракта.
        ``get`` бросает ошибку воркера либо возвращает ``None`` по таймауту.

        ВАЖНО: ``_submit`` здесь только ЗАПИСЫВАЕТ переданный ``_Job`` и не
        выполняет его. Тест может взять job из ``manager._submit.call_args`` и
        запустить ``job.fn(fake_conn)`` сам — иначе тело пробы остаётся
        непроверенным, а сломанный ``SELECT 1`` прошёл бы незамеченным.

        mode: ``alive`` | ``timeout`` | ``error``.

        Returns:
            ``(get_manager, manager)`` — оба нужны: первым проверяют, что пул
            не дёргали вообще, второй хранит отправленные job'ы.
        """
        import importlib.util
        import sys
        import uuid
        from pathlib import Path

        # ``db.py`` живёт в ``lib/utils/`` рядом со своим пакетом, поэтому
        # корень репозитория уже должен быть в ``sys.path`` (иначе и сам
        # ``lib.utils.db`` не импортировался бы). Отдельного ``sys.path.insert``
        # не делаем: ``workspace/`` больше не участвует в резолве имён.
        _repo = Path(__file__).resolve().parents[1]
        real = importlib.util.module_from_spec(
            importlib.util.spec_from_file_location(
                "utils_db_real_" + uuid.uuid4().hex, _repo / "lib" / "utils" / "db.py"
            )
        )
        real.__spec__.loader.exec_module(real)

        handle = MagicMock()
        if mode == "alive":
            handle.get.return_value = (1,)
        elif mode == "timeout":
            handle.get.return_value = None
        elif mode == "error":
            handle.get.side_effect = ConnectionRefusedError("PG down")
        else:  # pragma: no cover - защита от опечатки в тесте
            raise AssertionError("unknown mode: %r" % mode)

        manager = MagicMock()
        manager._submit.return_value = handle
        get_manager = MagicMock(return_value=manager)

        # production-код делает ``from lib.utils.db import _get_manager, _Job``
        # на момент вызова — значит подменять надо сам sys.modules.
        monkeypatch.setitem(sys.modules, "lib.utils.db", real)
        monkeypatch.setattr(real, "_get_manager", get_manager, raising=False)
        return get_manager, manager

    @staticmethod
    def _submitted_job(manager):
        """Job, отправленный в пул последней проверкой."""
        assert manager._submit.call_count == 1, (
            "ожидалась ровно одна задача в пуле, отправлено %d"
            % manager._submit.call_count
        )
        return manager._submit.call_args[0][0]

    def test_live_pool_is_up_despite_non_pg_manager_name(self, monkeypatch):
        """Живая БД + менеджер без PG в имени = UP (а не DOWN)."""
        from lib.core.application_context import _register_readiness_checks

        self._patch_pool(monkeypatch, mode="alive")
        ctx = self._ctx()
        _register_readiness_checks(ctx)

        report = ctx.runtime_readiness.check()
        assert report.components[0].status == "UP", report.components[0].detail
        assert report.status == "READY"
        # Режим хранилища попадает в detail: без него в стартовом логе не
        # видно, что БД проверена при file-режиме, а это и есть причина
        # поднять вопрос «а точно ли проверяли?».
        assert "storage_mode=file" in report.components[0].detail

    def test_ping_really_executes_select_1(self, monkeypatch):
        """Тело пробы обязано работать, а не только её решение.

        Мок пула не выполняет отправленный job, поэтому без этого теста
        сломанный ``SELECT 1`` (или неверное пользование курсором) прошёл бы
        незамеченным: проверка приняла бы результат заглушки.
        """
        from lib.core.application_context import _register_readiness_checks

        _get_manager, manager = self._patch_pool(monkeypatch, mode="alive")
        ctx = self._ctx()
        _register_readiness_checks(ctx)
        ctx.runtime_readiness.check()

        job = self._submitted_job(manager)
        executed = []

        class _Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, *params):
                executed.append(sql)

            def fetchone(self):
                return (1,)

        class _Conn:
            def cursor(self):
                return _Cursor()

        result = job.fn(_Conn())
        assert executed == ["SELECT 1"], executed
        assert result is not None, "проба обязана вернуть строку, а не None"

    def test_dead_pool_is_down_and_not_ready(self, monkeypatch):
        """Мёртвая БД при включённом канале = DOWN и NOT_READY."""
        from lib.core.application_context import _register_readiness_checks

        self._patch_pool(monkeypatch, mode="timeout")
        ctx = self._ctx()
        _register_readiness_checks(ctx)

        report = ctx.runtime_readiness.check()
        assert report.components[0].status == "DOWN"
        assert report.status == "NOT_READY"

    def test_pg_optional_when_channel_disabled_and_storage_file(self, monkeypatch):
        """Канал выключен + storage=file, но DSN есть и БД лежит.

        БД не участвует в работе агента, поэтому её недоступность —
        DEGRADED, а не NOT_READY. Именно это обещает докстринг
        ``_register_readiness_checks``.
        """
        from lib.core.application_context import _register_readiness_checks

        self._patch_pool(monkeypatch, mode="timeout")
        ctx = self._ctx(
            channel_on=False,
            dsn="postgresql://u:p@h:5432/d",
            storage_mode="file",
        )
        _register_readiness_checks(ctx)

        report = ctx.runtime_readiness.check()
        assert report.components[0].required is False
        assert report.components[0].status == "DOWN"
        assert report.status == "DEGRADED"

    def test_pg_required_when_storage_is_postgres(self, monkeypatch):
        """storage=postgres делает БД required даже при выключенном канале."""
        from lib.core.application_context import _register_readiness_checks

        self._patch_pool(monkeypatch, mode="timeout")
        ctx = self._ctx(channel_on=False, dsn="postgresql://u:p@h:5432/d",
                        storage_mode="postgres")
        _register_readiness_checks(ctx)

        report = ctx.runtime_readiness.check()
        assert report.components[0].required is True
        assert report.status == "NOT_READY"

    def test_absent_pool_not_timed_out_when_pg_unused(self, monkeypatch):
        """БД не нужна и DSN нет — UP без ping'а, а не 2 с таймаут в DOWN."""
        from lib.core.application_context import _register_readiness_checks

        get_manager, manager = self._patch_pool(monkeypatch, mode="timeout")
        ctx = self._ctx(channel_on=False, dsn="", storage_mode="file")
        _register_readiness_checks(ctx)

        report = ctx.runtime_readiness.check()
        assert report.components[0].status == "UP"
        assert report.status == "READY"
        # пул не дёргали вовсе: ни одной задачи в очередь не ушло
        assert get_manager.call_count == 0
        assert manager._submit.call_count == 0
        assert "pg not required" in report.components[0].detail

    def test_worker_error_is_reported_as_down(self, monkeypatch):
        """Ошибка воркера (БД недоступна) = DOWN с её текстом в detail."""
        from lib.core.application_context import _register_readiness_checks

        self._patch_pool(monkeypatch, mode="error")
        ctx = self._ctx()
        _register_readiness_checks(ctx)

        report = ctx.runtime_readiness.check()
        assert report.components[0].status == "DOWN"
        assert "PG down" in report.components[0].detail
        assert report.status == "NOT_READY"

    def test_missing_session_manager_is_down(self, monkeypatch):
        """Нет менеджера сессий = DOWN даже при живой БД, и пула не касаемся.

        Подмена пула здесь не нужна для решения, но проверка, что в него НЕ
        отправили задачу, документирует ранний выход: при живом пуле читатель
        теста иначе решил бы, что DOWN вызван падением пинга.
        """
        from lib.core.application_context import _register_readiness_checks

        _get_manager, manager = self._patch_pool(monkeypatch, mode="alive")
        ctx = self._ctx()
        ctx.session_manager = None
        _register_readiness_checks(ctx)

        report = ctx.runtime_readiness.check()
        assert report.components[0].status == "DOWN"
        assert "no session_manager" in report.components[0].detail
        assert manager._submit.call_count == 0, (
            "менеджера сессий нет — проверка обязана выйти до отправки задачи"
        )


class TestApplicationContextIntegration:
    """ApplicationContext подключает readiness-проверки."""

    def test_application_context_has_runtime_health(self):
        from lib.core.application_context import ApplicationContext
        from lib.services.runtime_health import RuntimeHealth, RuntimeReadiness

        # Проверяем только что поля определены в классе.
        assert "runtime_health" in ApplicationContext.__annotations__
        assert "runtime_readiness" in ApplicationContext.__annotations__
        assert "RuntimeHealth" in dir(__import__(
            "lib.services.runtime_health", fromlist=["RuntimeHealth"]
        ))
        assert "RuntimeReadiness" in dir(__import__(
            "lib.services.runtime_health", fromlist=["RuntimeReadiness"]
        ))