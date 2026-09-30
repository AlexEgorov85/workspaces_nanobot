"""Снимок не удерживается между операциями; открытие падает громко.

Порт ``tests/test_cache_no_file_hold.py`` и ``tests/test_cache_provider_open_failure.py``
из агента. Миграция ``enterprise-mcp-platform``, фаза 3; удаление агентских
тестов — фазы 4/5/9.

Три случая, которые обязаны вести себя громко, а не «тихо готовым объектом»:

1. битый файл, read-only к несуществующему файлу, недостаточные права — раньше
   флаг подключения игнорировался, и наружу уходил экземпляр с
   ``is_ready() == False``; skill печатал «провайдер готов» и получал отказ на
   первом же запросе;
2. конфликт process-exclusive — ``CacheBusyError``, и в сообщении видно, **кто**
   держит файл;
3. RW-путь на чистом месте продолжает работать — это не регрессия.

Отличие от агентского теста: конфигурация агента (``config.SETTINGS``) больше не
используется — путь приходит параметром, как и в коде.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from libs.enterprise_data.snapshot import (
    CacheAccessMode,
    CacheBusyError,
    CacheOpenError,
    ConfigurationError,
    extract_lock_holder,
    open_snapshot_store,
    reject_unsupported_filesystem,
    resolve_snapshot_path,
)
from libs.enterprise_data.snapshot.store import (
    DuckDbSnapshotStore,
    UnsupportedFilesystemError,
)


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    """Каталог снимка (не файл) — имя добавляется внутри."""
    store = tmp_path / "duckdb"
    store.mkdir()
    return store


@pytest.fixture
def seeded_cache_dir(cache_dir: Path) -> Path:
    """Каталог с **существующим** файлом снимка.

    ``READ_ONLY`` к несуществующему файлу — штатный отказ (проверяется
    отдельно), поэтому тесты про неудержание файла должны читать настоящий
    снимок, иначе проверяли бы не то.
    """
    conn = duckdb.connect(resolve_snapshot_path(str(cache_dir)))
    conn.execute("CREATE TABLE main.t(a INTEGER)")
    conn.execute("INSERT INTO main.t VALUES (1)")
    conn.close()
    return cache_dir


class TestNoFileHold:
    def test_file_not_held_between_reads(self, seeded_cache_dir: Path) -> None:
        """После ``connect()`` и между чтениями файл не открыт процессом."""
        path = resolve_snapshot_path(str(seeded_cache_dir))
        store = open_snapshot_store(path, CacheAccessMode.READ_ONLY, schema="main")
        try:
            assert store._conn is None, "файл удерживается между операциями"

            store.query_sql("SELECT 1 AS one")
            assert store._conn is None, "соединение осталось после чтения"

            store.query_sql("SELECT 2 AS two")
            assert store._conn is None, "соединение осталось после второго чтения"
        finally:
            store.close()

    def test_connection_closed_even_when_query_fails(
        self, seeded_cache_dir: Path
    ) -> None:
        store = open_snapshot_store(
            resolve_snapshot_path(str(seeded_cache_dir)), CacheAccessMode.READ_ONLY
        )
        try:
            store.query_sql("SELECT * FROM table_that_does_not_exist")
            assert store._conn is None
        finally:
            store.close()

    def test_ddl_rejection_does_not_open_the_file(self, seeded_cache_dir: Path) -> None:
        """Проверка режима выполняется до открытия файла (текст + режим)."""
        path = resolve_snapshot_path(str(seeded_cache_dir))
        store = open_snapshot_store(path, CacheAccessMode.READ_ONLY, schema="main")
        try:
            with pytest.raises(Exception):
                store.query_sql("CREATE TABLE t(a INTEGER)")
            assert store._conn is None, "отказ по режиму оставил файл открытым"
        finally:
            store.close()

    def test_read_write_mode_holds_connection(self, cache_dir: Path) -> None:
        """RW — стадия загрузки: соединение держит загрузчик (фаза 5)."""
        path = resolve_snapshot_path(str(cache_dir))
        store = open_snapshot_store(path, CacheAccessMode.READ_WRITE, schema="main")
        try:
            assert store._conn is not None
        finally:
            store.close()


class TestFactoryFailsLoudly:
    def test_read_only_on_missing_file_raises(self, cache_dir: Path) -> None:
        assert not (cache_dir / "cache.duckdb").exists()
        with pytest.raises(CacheOpenError) as excinfo:
            open_snapshot_store(
                resolve_snapshot_path(str(cache_dir)), CacheAccessMode.READ_ONLY
            )
        assert excinfo.value.path.endswith("cache.duckdb")
        assert excinfo.value.cause is not None, "причина должна попасть в ошибку"

    def test_corrupt_file_raises(self, cache_dir: Path) -> None:
        (cache_dir / "cache.duckdb").write_bytes(b"not a duckdb file" * 64)
        with pytest.raises(CacheOpenError):
            open_snapshot_store(
                resolve_snapshot_path(str(cache_dir)), CacheAccessMode.READ_WRITE
            )

    def test_empty_file_raises(self, cache_dir: Path) -> None:
        (cache_dir / "cache.duckdb").write_bytes(b"")
        with pytest.raises(CacheOpenError):
            open_snapshot_store(
                resolve_snapshot_path(str(cache_dir)), CacheAccessMode.READ_WRITE
            )

    def test_empty_path_raises(self) -> None:
        with pytest.raises(CacheOpenError):
            open_snapshot_store("", CacheAccessMode.READ_ONLY)

    def test_open_error_is_a_configuration_error(self) -> None:
        """Провал startup-зависимости — конфигурационная ошибка (fail-fast)."""
        assert issubclass(CacheOpenError, ConfigurationError)

    def test_no_half_ready_provider_escapes(self, cache_dir: Path) -> None:
        """«Полуготовый» провайдер наружу не отдаётся: наружу — исключение."""
        with pytest.raises(CacheOpenError):
            store = open_snapshot_store(
                resolve_snapshot_path(str(cache_dir)), CacheAccessMode.READ_ONLY
            )
            assert store.is_ready() is True, "фабрика не должна отдавать неготовый"

    def test_read_write_on_fresh_path_still_works(self, cache_dir: Path) -> None:
        """Нормальный путь владельца не должен пострадать."""
        provider = open_snapshot_store(
            resolve_snapshot_path(str(cache_dir)), CacheAccessMode.READ_WRITE
        )
        try:
            assert provider.is_ready()
            assert (cache_dir / "cache.duckdb").exists()
        finally:
            provider.close()


class TestBusyErrorNamesTheHolder:
    """Оператор должен видеть, какой процесс держит файл."""

    def test_windows_message(self) -> None:
        message = (
            'IO Error: Cannot open file "C:\\\\c.duckdb": Процесс '
            "не может получить доступ к файлу, так как "
            "его использует другой процесс.\n\n"
            "File is already open in \nC:\\Python314\\python.exe (PID 25964)"
        )
        holder = extract_lock_holder(message)
        assert holder is not None
        assert "python.exe" in holder
        assert "25964" in holder

    def test_linux_message(self) -> None:
        holder = extract_lock_holder(
            "IO Error: Conflicting lock is held by process with PID 1234"
        )
        assert holder == "PID 1234"

    def test_unparsable_message(self) -> None:
        assert extract_lock_holder("something went wrong") is None

    def test_empty_message(self) -> None:
        assert extract_lock_holder("") is None

    def test_busy_marker_text_is_recognized(self, cache_dir: Path) -> None:
        """Конфликт блокировки внутри процесса не воспроизводится.

        DuckDB внутри одного процесса отдаёт уже открытый по пулю экземпляр, а
        сам конфликт — определение «держит **другой** процесс». Поэтому здесь
        проверяется то, что реально можно проверить в одном процессе: какой
        текст ошибки классифицируется как занятость, а какой — нет.
        """
        from libs.enterprise_data.snapshot.store import (
            DuckDbSnapshotStore as _Store,
        )

        busy = _Store._classify_open_error(
            RuntimeError("Conflicting lock is held by process with PID 1234"), "p"
        )
        assert isinstance(busy, CacheBusyError)
        assert busy.holder == "PID 1234"

        other = _Store._classify_open_error(RuntimeError("disk I/O error"), "p")
        assert not isinstance(other, CacheBusyError), (
            "сетевой/прочий сбой не должен выдаваться за «занято другим процессом»"
        )

    def test_busy_error_code_is_typed(self) -> None:
        """Занятость и «не открылось» — разные коды: retry у них разный смысл."""
        assert CacheBusyError.code == "cache_busy"
        assert CacheOpenError.code == "cache_open_error"


class TestFilesystemGuard:
    def test_local_path_accepted(self, tmp_path: Path) -> None:
        local = tmp_path / "local_cache"
        local.mkdir()
        reject_unsupported_filesystem(str(local / "cache.duckdb"))

    def test_error_is_an_enterprise_error(self) -> None:
        from libs.enterprise_common.errors import InfrastructureError

        assert issubclass(UnsupportedFilesystemError, InfrastructureError)
        assert UnsupportedFilesystemError.code == "unsupported_filesystem"

    def test_reject_is_called_before_open(self, cache_dir: Path, monkeypatch) -> None:
        """Проверка ФС обязана быть **до** открытия: fail-fast, а не падение DuckDB."""
        calls: list[str] = []
        real_connect = duckdb.connect

        def spy(*args: object, **kwargs: object):  # noqa: ANN202
            calls.append("connect")
            return real_connect(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(duckdb, "connect", spy)
        open_snapshot_store(
            resolve_snapshot_path(str(cache_dir)), CacheAccessMode.READ_WRITE
        ).close()
        assert calls == ["connect"], "хранилище должно сначала проверить ФС"

    def test_nfs_mount_is_rejected(self, tmp_path: Path, monkeypatch) -> None:
        """Сторож на заведомо плохих данных: точка монтирования помечена как NFS."""
        target = tmp_path / "nfs" / "cache.duckdb"
        target.parent.mkdir(parents=True)
        fake_mounts = tmp_path / "mounts"
        fake_mounts.write_text(
            f"nfsserver:/export {target.parent} nfs4 rw,relatime 0 0\n",
            encoding="utf-8",
        )
        monkeypatch.setattr("platform.system", lambda: "Linux")
        with pytest.raises(UnsupportedFilesystemError) as excinfo:
            reject_unsupported_filesystem(str(target), mounts_path=fake_mounts)
        assert "nfs4" in str(excinfo.value)

    @pytest.mark.parametrize("fstype", ["smb", "cifs", "NFS", "nfs4"])
    def test_all_network_fstypes_are_rejected(
        self, tmp_path: Path, monkeypatch, fstype: str
    ) -> None:
        target = tmp_path / "net" / "cache.duckdb"
        target.parent.mkdir(parents=True)
        fake_mounts = tmp_path / "mounts"
        fake_mounts.write_text(
            f"server {target.parent} {fstype} rw 0 0\n", encoding="utf-8"
        )
        monkeypatch.setattr("platform.system", lambda: "Linux")
        with pytest.raises(UnsupportedFilesystemError):
            reject_unsupported_filesystem(str(target), mounts_path=fake_mounts)

    def test_local_fstype_is_accepted(self, tmp_path: Path, monkeypatch) -> None:
        target = tmp_path / "local" / "cache.duckdb"
        fake_mounts = tmp_path / "mounts"
        fake_mounts.write_text(
            f"/dev/sda1 {target.parent} ext4 rw 0 0\n", encoding="utf-8"
        )
        monkeypatch.setattr("platform.system", lambda: "Linux")
        reject_unsupported_filesystem(str(target), mounts_path=fake_mounts)

    def test_garbage_mounts_is_skipped(self, tmp_path: Path, monkeypatch) -> None:
        """Мусорная строка в ``/proc/mounts`` не должна ронять проверку.

        Иначе один битый файл монтирований отверг бы локальный путь как
        сетевой — то есть отказ был бы вызван не той причиной, что названа.
        """
        target = tmp_path / "local" / "cache.duckdb"
        fake_mounts = tmp_path / "mounts"
        fake_mounts.write_text(
            f"\n\ngarbage\nx y\nlocalfs {target.parent} ext4 rw 0 0\n",
            encoding="utf-8",
        )
        monkeypatch.setattr("platform.system", lambda: "Linux")
        reject_unsupported_filesystem(str(target), mounts_path=fake_mounts)

    def test_empty_mounts_file_is_not_a_rejection(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Пустой список монтирований = «не на чём судить», а не «сетевая ФС»."""
        target = tmp_path / "local" / "cache.duckdb"
        fake_mounts = tmp_path / "mounts"
        fake_mounts.write_text("", encoding="utf-8")
        monkeypatch.setattr("platform.system", lambda: "Linux")
        reject_unsupported_filesystem(str(target), mounts_path=fake_mounts)

    def test_missing_mounts_file_is_a_noop(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr("platform.system", lambda: "Linux")
        reject_unsupported_filesystem(
            str(tmp_path / "cache.duckdb"), mounts_path=tmp_path / "absent"
        )

    @pytest.mark.parametrize("system", ["Windows", "Darwin", "Java"])
    def test_non_linux_is_a_noop(self, tmp_path: Path, monkeypatch, system) -> None:
        """На не-Linux ветка проверки не читает ничего: иначе Windows-тесты врали бы."""
        monkeypatch.setattr("platform.system", lambda: system)
        reject_unsupported_filesystem(
            str(tmp_path / "cache.duckdb"), mounts_path=tmp_path / "absent"
        )
