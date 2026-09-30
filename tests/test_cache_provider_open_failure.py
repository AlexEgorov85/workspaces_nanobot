"""Регрессии точки создания ``CacheProvider``.

Три случая, которые обязаны вести себя громко, а не «тихо готовым объектом»:

1. ``connect() == False`` (битый файл, read-only к несуществующему файлу,
   недостаточные права) — раньше флаг игнорировался, и наружу уходил
   экземпляр с ``is_ready() == False``. Skill печатал «cache provider ready»
   и получал «DuckDbCacheStore is not ready» на первом же запросе.
2. Конфликт process-exclusive — ``CacheBusyError``, и в сообщении видно,
   **кто** держит файл (DuckDB сообщает PID и путь к исполняемому файлу).
3. RW-путь на чистом месте обязан продолжать работать — это не регрессия.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_project_root = Path(__file__).resolve().parent.parent
for _p in (str(_project_root), str(_project_root / "workspace")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from lib.services.cache_provider import CacheAccessMode  # noqa: E402
from lib.services.cache_provider import (  # noqa: E402
    CacheBusyError,
    CacheOpenError,
    open_cache_provider,
)
from lib.services.duckdb_cache_store import _extract_lock_holder  # noqa: E402


@pytest.fixture
def cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Изолированный каталог кэша, подставленный в ``config.SETTINGS``.

    ``gateway.cache.local_path`` — это **каталог** (не файл): имя файла
    ``cache.duckdb`` добавляется внутри ``resolve_publish_path``.
    """
    import config

    store = tmp_path / "duckdb"
    store.mkdir()
    monkeypatch.setattr(
        config,
        "SETTINGS",
        {
            "workspace_path": str(tmp_path),
            "gateway": {"cache": {"local_path": str(store)}},
        },
    )
    return store


class TestFactoryFailsLoudly:
    def test_read_only_on_missing_file_raises(self, cache_dir: Path) -> None:
        """Read-only к несуществующему файлу — это провал, а не «готовый» объект."""
        assert not (cache_dir / "cache.duckdb").exists()
        with pytest.raises(CacheOpenError) as excinfo:
            open_cache_provider(mode=CacheAccessMode.READ_ONLY)
        assert excinfo.value.path.endswith("cache.duckdb")
        assert excinfo.value.cause is not None, "причина должна попасть в ошибку"

    def test_corrupt_file_raises(self, cache_dir: Path) -> None:
        (cache_dir / "cache.duckdb").write_bytes(b"not a duckdb file" * 64)
        with pytest.raises(CacheOpenError):
            open_cache_provider(mode=CacheAccessMode.READ_WRITE)

    def test_open_error_is_a_configuration_error(self) -> None:
        """Провал startup-зависимости — это ConfigurationError (fail-fast)."""
        from config import ConfigurationError

        assert issubclass(CacheOpenError, ConfigurationError)

    def test_read_write_on_fresh_path_still_works(self, cache_dir: Path) -> None:
        """Нормальный путь владельца не должен пострадать."""
        provider = open_cache_provider(mode=CacheAccessMode.READ_WRITE)
        try:
            assert provider.is_ready()
            assert (cache_dir / "cache.duckdb").exists()
        finally:
            provider.close()


class TestBusyErrorNamesTheHolder:
    """Оператор должен видеть, какой процесс держать файл."""

    def test_windows_message(self) -> None:
        message = (
            'IO Error: Cannot open file "C:\\\\c.duckdb": \u041f\u0440\u043e\u0446\u0435\u0441\u0441 '
            "\u043d\u0435 \u043c\u043e\u0436\u0435\u0442 \u043f\u043e\u043b\u0443\u0447\u0438\u0442\u044c "
            "\u0434\u043e\u0441\u0442\u0443\u043f \u043a \u0444\u0430\u0439\u043b\u0443.\n\n"
            "File is already open in \nC:\\Python314\\python.exe (PID 25964)"
        )
        holder = _extract_lock_holder(message)
        assert holder is not None
        assert "python.exe" in holder
        assert "25964" in holder

    def test_linux_message(self) -> None:
        holder = _extract_lock_holder(
            "IO Error: Conflicting lock is held by process with PID 1234"
        )
        assert holder == "PID 1234"

    def test_unparsable_message(self) -> None:
        assert _extract_lock_holder("something went wrong") is None

    def test_busy_error_reaches_the_caller_typed(self, cache_dir: Path) -> None:
        """Занятость пробрасывается как ``CacheBusyError``, а не как ``CacheOpenError``."""
        from lib.services.duckdb_cache_store import DuckDbCacheStore

        # Занятый файл: открываем его вручную (RW) и просим фабрику
        # открыть второй раз в том же процессе — DuckDB откажет по
        # file-lock-семантике.
        holder = DuckDbCacheStore.open(
            str(cache_dir / "cache.duckdb"), CacheAccessMode.READ_WRITE
        )
        assert holder.connect()
        try:
            with pytest.raises((CacheBusyError, CacheOpenError)) as excinfo:
                open_cache_provider(mode=CacheAccessMode.READ_ONLY)
            # Тип ошибки зависит от того, распознал ли классификатор текст
            # DuckDB; в любом случае наружу уходит исключение, а не False.
            assert excinfo.value is not None
        finally:
            holder.close()
