"""Снимок может не подключиться — и клиент должен узнать почему.

Два контракта проверяются разными тестами.

1. **Процесс поднимается.** Докстринг ``servers.enterprise.server._snapshot``
   объявляет снимок необязательным: сервер обязан работать там, где
   capability ``vectors`` не развёрнут. До правки это выполнялось только
   для незаданного пути — занятый или битый файл поднимал ошибку из
   ``build()``, и процесс падал целиком вместе с ``history_search``,
   ``log_event``, ``llm`` и ``audit``.

2. **Причина доезжает.** ``None`` уносил диагноз, и клиент получал вместо
   него чужую подсказку «проверьте регистрацию в server.py». Теперь
   composition root отдаёт ``UnavailableSnapshot``: та же причина и её код
   (``cache_busy``, ``cache_open_error``) на каждой операции.

Тест не открывает DuckDB намеренно: проверяется контракт сборки, а не
работа хранилища (её покрывает ``test_snapshot_store.py``).
"""

from __future__ import annotations

import json

import pytest

from libs.enterprise_common.errors import InfrastructureError
from libs.enterprise_common.settings import PLATFORM_CONFIG_PATH, Settings
from libs.enterprise_data import snapshot as snapshot_module
from libs.enterprise_data.snapshot import CacheBusyError, CacheOpenError
from libs.enterprise_data.snapshot.unavailable import UnavailableSnapshot
from servers.enterprise import server as enterprise_server


def _settings(**overrides: str) -> Settings:
    return Settings()


def _settings_with_file(config_path) -> Settings:
    """Реестр поверх копии файла: как на развёртывании, но без правки рабочей."""
    return Settings(file_path=config_path)


def _config_without(tmp_path, *keys: str):
    """Копия platform.json без указанных ключей.

    «Снимок не настроен» — это отсутствие ключа в файле. Пустая переменная
    окружения больше не выражает этого: окружение приоритетнее файла, но
    пустое значение просто пропускается, и путь из файла остаётся в силе.
    """
    raw = json.loads(PLATFORM_CONFIG_PATH.read_text(encoding="utf-8"))
    for dotted in keys:
        section, _, key = dotted.partition(".")
        raw.get(section, {}).pop(key, None)
    target = tmp_path / "platform.json"
    target.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return target


@pytest.fixture
def _snapshot_path(monkeypatch, tmp_path):
    """Задать путь снимка.

    Раньше агент передавал его переменной окружения; теперь путь объявляется
    в platform.json, поэтому тест подставляет его в окружение только чтобы
    открытие гарантированно ушло в подменённый ``open_snapshot_store``.
    """
    path = tmp_path / "cache.duckdb"
    monkeypatch.setenv("ENTERPRISE_SNAPSHOT_PATH", str(path))
    return path


def _busy(path: str):
    def _open(*args, **kwargs):
        raise CacheBusyError(path=str(path), holder="loader.py (PID 4242)")

    return _open


def _broken(path: str):
    def _open(*args, **kwargs):
        raise CacheOpenError(str(path), cause=OSError("нечитаемый файл"))

    return _open


class TestServerSurvivesUnusableSnapshot:
    def test_busy_file_does_not_kill_startup(self, monkeypatch, _snapshot_path):
        monkeypatch.setattr(
            snapshot_module, "open_snapshot_store", _busy(_snapshot_path)
        )
        snapshot = enterprise_server._snapshot(_settings())
        assert isinstance(snapshot, UnavailableSnapshot)

    def test_broken_file_does_not_kill_startup(self, monkeypatch, _snapshot_path):
        monkeypatch.setattr(
            snapshot_module, "open_snapshot_store", _broken(_snapshot_path)
        )
        snapshot = enterprise_server._snapshot(_settings())
        assert isinstance(snapshot, UnavailableSnapshot)

    def test_unsupported_filesystem_does_not_kill_startup(
        self, monkeypatch, _snapshot_path
    ):
        def _open(*args, **kwargs):
            raise InfrastructureError("путь на сетевой ФС: file lock не работает")

        monkeypatch.setattr(snapshot_module, "open_snapshot_store", _open)
        assert isinstance(
            enterprise_server._snapshot(_settings()), UnavailableSnapshot
        )

    def test_container_is_built_and_other_capabilities_work(
        self, monkeypatch, _snapshot_path
    ):
        monkeypatch.setattr(
            snapshot_module, "open_snapshot_store", _busy(_snapshot_path)
        )
        container = enterprise_server._build_container(_settings())

        # процесс собрался: capability, не зависящие от снимка, живы
        assert container.get("llm") is not None
        assert container.get("vectors") is not None
        assert container.get("audit") is not None

        with pytest.raises(InfrastructureError):
            container.get("data").snapshot_query("SELECT 1")


class TestClientGetsTheRealCause:
    """Причина — это данные, а не лог: клиент читает её из ответа."""

    def test_busy_keeps_its_own_code(self, monkeypatch, _snapshot_path):
        monkeypatch.setattr(
            snapshot_module, "open_snapshot_store", _busy(_snapshot_path)
        )
        snapshot = enterprise_server._snapshot(_settings())

        # код исходной ошибки не размывается до общего infrastructure_error:
        # агент различает «процесс держит файл» и «сломалось что-то ещё»
        assert snapshot.code == "cache_busy"
        with pytest.raises(InfrastructureError) as excinfo:
            snapshot.query_sql("SELECT 1")
        assert excinfo.value.code == "cache_busy"
        assert "loader.py (PID 4242)" in str(excinfo.value)

    def test_broken_file_keeps_its_own_code(self, monkeypatch, _snapshot_path):
        monkeypatch.setattr(
            snapshot_module, "open_snapshot_store", _broken(_snapshot_path)
        )
        snapshot = enterprise_server._snapshot(_settings())
        with pytest.raises(InfrastructureError) as excinfo:
            snapshot.get_schema()
        assert excinfo.value.code == "cache_open_error"

    def test_unset_path_says_it_is_not_configured(self, tmp_path):
        config = _config_without(tmp_path, "data.snapshot_path")
        snapshot = enterprise_server._snapshot(_settings_with_file(config))
        assert snapshot.is_ready() is False
        assert "ENTERPRISE_SNAPSHOT_PATH" in snapshot.reason
        with pytest.raises(InfrastructureError) as excinfo:
            snapshot.query_sql("SELECT 1")
        assert "ENTERPRISE_SNAPSHOT_PATH" in str(excinfo.value)

    def test_reason_is_readable_without_calling_an_operation(self, tmp_path):
        """Health не должен падать, чтобы узнать причину."""
        config = _config_without(tmp_path, "data.snapshot_path")
        stats = enterprise_server._snapshot(_settings_with_file(config)).get_stats()
        assert stats["ready"] is False
        assert "ENTERPRISE_SNAPSHOT_PATH" in stats["reason"]

    def test_vector_reads_fail_with_the_reason_not_attribute_error(self, tmp_path):
        """Векторные чтения не входят в ABC — без заглушки был бы AttributeError."""
        config = _config_without(tmp_path, "data.snapshot_path")
        snapshot = enterprise_server._snapshot(_settings_with_file(config))
        for call in (
            lambda: snapshot.fetch_source_vectors("audits_index"),
            lambda: snapshot.vector_source_stats(),
            lambda: snapshot.fetch_chunk_payload("audits_index", 1, 0),
            lambda: snapshot.search_vector("запрос"),
            lambda: snapshot.preload_indexes(),
            lambda: snapshot.replace_records("oarb.audits", []),
        ):
            with pytest.raises(InfrastructureError):
                call()

    def test_available_snapshot_is_still_opened(self, monkeypatch, _snapshot_path):
        """Правка не выключает снимок: успешное открытие возвращается как есть."""
        sentinel = object()
        monkeypatch.setattr(
            snapshot_module, "open_snapshot_store", lambda *a, **k: sentinel
        )
        assert enterprise_server._snapshot(_settings()) is sentinel
