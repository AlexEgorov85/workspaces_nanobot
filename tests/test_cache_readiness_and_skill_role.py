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
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_REPO = Path(__file__).resolve().parent.parent
_WS = str(_REPO / "workspace")
if _WS not in sys.path:  # тот же приём, что и в production-коде
    sys.path.insert(0, _WS)


# --------------------------------------------------------------------------
# 4.2 — READY только после загрузки кэша
# --------------------------------------------------------------------------


class _ReadyStore:
    """Провайдер кэша, который загружен и готов отдавать данные."""

    def is_ready(self) -> bool:
        return True

    def search_vector(self, *args: Any, **kwargs: Any) -> Any:
        return []


class _NotReadyStore(_ReadyStore):
    """Провайдер, который ещё не готов отдавать данные."""

    def is_ready(self) -> bool:
        return False


class _PGSessionManagerStub:
    """Заглушка storage. Имя класса обязано содержать ``PG`` — иначе
    ``check_postgres`` считает систему degraded по типу storage."""


class _StubCtx:
    """Минимальный контекст, который читает ``_register_readiness_checks``."""

    def __init__(self, cache_store: Any, session_manager: Any) -> None:
        from lib.services.runtime_health import RuntimeReadiness

        self.cache_store = cache_store
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


def test_loaded_cache_yields_ready(pg_ping_ok: None) -> None:
    """Кэш загружен → READY, а не NOT_READY."""
    ctx = _StubCtx(_ReadyStore(), _PGSessionManagerStub())
    _register(ctx)

    report = ctx.runtime_readiness.check()

    assert _component(report, "duckdb_cache").status == "UP"
    assert report.status == "READY", report.components


def test_cache_absent_blocks_ready(pg_ping_ok: None) -> None:
    """Кэша нет (загрузка не дала провайдера) → required-компонент DOWN."""
    ctx = _StubCtx(None, _PGSessionManagerStub())
    _register(ctx)

    report = ctx.runtime_readiness.check()

    component = _component(report, "duckdb_cache")
    assert component.status == "DOWN"
    assert component.required is True
    assert report.status == "NOT_READY", report.components


def test_unloaded_cache_blocks_ready(pg_ping_ok: None) -> None:
    """Провайдер есть, но ``is_ready()=False`` → тоже DOWN.

    Это ровно то состояние, в котором система оказывается между созданием
    провайдера и завершением загрузки.
    """
    ctx = _StubCtx(_NotReadyStore(), _PGSessionManagerStub())
    _register(ctx)

    report = ctx.runtime_readiness.check()

    assert _component(report, "duckdb_cache").status == "DOWN"
    assert report.status == "NOT_READY", report.components


def test_is_ready_failure_is_reported_not_swallowed(
    pg_ping_ok: None,
) -> None:
    """Исключение из ``is_ready()`` — DOWN с текстом, а не тишина."""
    exploding = SimpleNamespace(is_ready=lambda: (_ for _ in ()).throw(OSError("boom")))
    ctx = _StubCtx(exploding, _PGSessionManagerStub())
    _register(ctx)

    report = ctx.runtime_readiness.check()

    component = _component(report, "duckdb_cache")
    assert component.status == "DOWN"
    assert "boom" in (component.detail or "")


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


def test_skill_entry_point_is_annotated_cache_provider() -> None:
    """Skill-side точка входа объявлена как ``CacheProvider``, не как
    реализация и не как объединение ролей."""
    from lib.core.skill_config import build_cache_provider

    hints = getattr(build_cache_provider, "__annotations__", {})
    assert hints.get("return") == "CacheProvider", hints


def test_skill_entry_point_requests_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Навык получает файл в режиме READ_ONLY — записи у него нет."""
    from lib.core import skill_config
    from lib.services import cache_provider as provider_mod

    seen: dict[str, Any] = {}

    class _ReadOnlyProvider:
        """Ровно те методы, что есть у роли чтения."""

        def is_ready(self) -> bool:
            return True

        def query_sql(self, sql: str) -> list[dict[str, Any]]:
            return []

        def get_schema(self) -> dict[str, str]:
            return {}

        def explain(self, sql: str) -> str:
            return ""

        def search_vector(self, *a: Any, **k: Any) -> list[Any]:
            return []

        def preload_indexes(self) -> None:
            return None

        def close(self) -> None:
            return None

    def _fake_open(*, mode: Any) -> Any:
        seen["mode"] = mode
        return _ReadOnlyProvider()

    monkeypatch.setattr(provider_mod, "open_cache_provider", _fake_open)

    result = skill_config.build_cache_provider("audit_analyzer", _REPO / "workspace")

    from lib.services.cache_provider import CacheAccessMode

    assert seen["mode"] is CacheAccessMode.READ_ONLY
    assert isinstance(result, _ReadOnlyProvider)


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
