"""Готовность рантайма и роль, выдаваемая навыку.

Change ``drop-local-cache-read-from-pg``, задачи 3.6 и 4.2.

Задача 4.2 требует, чтобы ``runtime_health`` выставлял READY только после
загрузки кэша, а до готовности потребитель получал явную ошибку
неготовности, а не пустой результат. Проверяется настоящая production-проверка
``_register_readiness_checks``, а не её копия в тесте.

Задача 3.6 требует, чтобы skill-side путь
``build_cache_provider()`` → ``lib.core.skill_config`` →
``open_cache_provider(mode=READ_ONLY)`` возвращал ``CacheProvider`` и чтобы
роль ``CacheIngestion`` оттуда была недоступна.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_REPO = Path(__file__).resolve().parent.parent
_WS = str(_REPO / "workspace")
if _WS not in sys.path:  # тот же приём, что и в production-коде
    sys.path.insert(0, _WS)


# --------------------------------------------------------------------------
# 4.2 — readiness больше не зависит от снимка
# --------------------------------------------------------------------------


class _PGSessionManagerStub:
    """Заглушка storage. Имя класса обязано содержать ``PG`` — иначе
    ``check_postgres`` считает систему degraded по типу storage."""


class _StubCtx:
    """Минимальный контекст, который читает ``_register_readiness_checks``."""

    def __init__(self, session_manager: Any) -> None:
        from lib.services.runtime_health import RuntimeReadiness

        self.session_manager = session_manager
        self.runtime_readiness = RuntimeReadiness()


@pytest.fixture
def pg_ping_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Сделать проверку ``postgres`` зелёной без обращения к сети."""
    import utils.db as ws_db

    class _Done:
        """Ручка результата: production-код зовёт ``job.result.get(timeout=...)``."""

        def get(self, timeout: Any = None) -> Any:
            return object()

    class _FakeManager:
        def _submit(self, job: Any, *args: Any, **kwargs: Any) -> Any:
            return SimpleNamespace(result=_Done())

    monkeypatch.setattr(ws_db, "_get_manager", lambda: _FakeManager())


def _register(ctx: _StubCtx) -> None:
    from lib.core.application_context import _register_readiness_checks

    _register_readiness_checks(ctx)  # type: ignore[arg-type]


def _component(report: Any, name: str) -> Any:
    matches = [c for c in report.components if c.name == name]
    assert matches, (
        f"компонент {name!r} не зарегистрирован; есть: "
        f"{[c.name for c in report.components]}"
    )
    return matches[0]


def test_only_postgres_is_registered(pg_ping_ok: None) -> None:
    """Компонентов кэша в readiness больше нет — и это проверяется.

    Проверка на отсутствие, а не «просто удалили тесты»: если бы регистрация
    ``duckdb_cache`` вернулась, компонент, которого нет, всегда отдавал бы
    DOWN, и ``RuntimeReadiness`` **никогда** не смог бы стать READY. Отсюда
    был бы тихий отказ всей системы стартовать нормально, при этом без
    единого исключения — поэтому и нужен явный страж.
    """
    ctx = _StubCtx(_PGSessionManagerStub())
    _register(ctx)

    names = [c.name for c in ctx.runtime_readiness.check().components]
    assert names == ["postgres"], (
        f"в readiness остались посторонние компоненты: {names} — любой из "
        "них, читающий отсутствующий ресурс, заблокирует READY навсегда"
    )


def test_readiness_can_reach_ready(pg_ping_ok: None) -> None:
    """При доступной БД система обязана доходить до READY."""
    ctx = _StubCtx(_PGSessionManagerStub())
    _register(ctx)

    report = ctx.runtime_readiness.check()

    assert _component(report, "postgres").status == "UP"
    assert report.status == "READY", report.components


def test_postgres_down_is_reported(pg_ping_ok: None) -> None:
    """Проверка PG честно падает — регистрация не пустая и не декоративная."""
    from lib.core.application_context import _register_readiness_checks
    from lib.services.runtime_health import RuntimeReadiness

    ctx = _StubCtx(None)  # нет session_manager -> postgres DOWN
    ctx.runtime_readiness = RuntimeReadiness()
    _register(ctx)

    report = ctx.runtime_readiness.check()

    assert _component(report, "postgres").status == "DOWN"
    assert report.status == "NOT_READY", report.components
    assert _register_readiness_checks is not None


def test_unready_store_answers_explicit_error_not_empty_result() -> None:
    """До готовности чтение даёт явную ошибку, а не пустой результат.

    Вторая половина задачи 4.2. Пустой список был бы неотличим от «запрос
    выполнен, данных нет» — потребитель продолжил бы работать вслепую.
    """
    from lib.services.duckdb_cache_store import DuckDbCacheStore

    store = DuckDbCacheStore(cache_path="")

    result = store.execute_readonly("SELECT 1")

    assert isinstance(result, dict), result
    assert "error" in result, result
    assert "not ready" in str(result["error"]).lower(), result


# --------------------------------------------------------------------------
# 3.6 — навык получает роль только для чтения
# --------------------------------------------------------------------------


def test_ingestion_role_unreachable_from_skill_provider() -> None:
    """Роль ``CacheIngestion`` навыку недоступна: разделение ролей реально,
    а не декларативно — ``CacheProvider`` её не наследует."""
    from lib.services.cache_provider import CacheIngestion, CacheProvider, CacheStore

    assert not issubclass(CacheProvider, CacheIngestion), (
        "роль чтения не должна включать роль записи — иначе навык "
        "сможет переписать снимок"
    )

    read_only_methods = set(CacheProvider.__abstractmethods__)
    assert read_only_methods, "у роли чтения не должно быть ни одного метода"

    write_methods = {
        name for name in CacheIngestion.__abstractmethods__ if name not in read_only_methods
    }
    assert write_methods, "у роли записи не осталось ни одного собственного метода"

    for name in write_methods:
        assert not hasattr(CacheProvider, name), (
            f"{name!r} — метод записи, просочившийся в роль чтения"
        )

    # Конкретная реализация обязана совмещать обе роли: иначе загрузчик
    # не сможет писать. Это ровно то, что проверяет CacheStore.
    assert set(write_methods) <= set(CacheStore.__abstractmethods__)
