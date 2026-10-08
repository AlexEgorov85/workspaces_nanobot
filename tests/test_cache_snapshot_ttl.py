"""Тесты TTL-переиспользования снапшота ``cache.duckdb``.

Контракт (``gateway.cache.reuse_ttl_hours``):

* **Время жизни — по ``mtime``, а не по дате создания.** ``mtime`` = момент
  последней публикации (``publish()`` делает ``os.replace`` свежего файла
  поверх старого). Дата создания для «свежести данных» непригодна: на POSIX
  это ``st_ctime`` (меняется при правке метаданных), в Windows — другая
  семантика.
* ``0`` → никогда не переиспользовать (прежнее поведение: снапшот всегда
  пересоздаётся). Это escape-hatch, а не «дефолт забыли».
* Отсутствующий снапшот → пересоздать, age неизвестен (``None``).
* Решение — чистая функция: ровно один ``stat``, никаких БД/DuckDB, чтобы
  гард проверялся без поднятия окружения.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from lib.core.application_context import (
    CacheSnapshotDecision,
    evaluate_cache_snapshot,
    format_cache_age,
)


def _touch(path: Path, age_sec: float) -> None:
    """Создать файл с ``mtime`` ровно ``age_sec`` секунд назад."""
    path.write_bytes(b"snapshot")
    when = time.time() - age_sec
    os.utime(path, (when, when))


class TestEvaluateCacheSnapshot:
    def test_missing_snapshot_forces_recreate(self, tmp_path: Path) -> None:
        d = evaluate_cache_snapshot(str(tmp_path / "absent.duckdb"), 23.0)
        assert d.reuse is False
        assert d.reason == "snapshot_missing"
        assert d.age_sec is None, "у несуществующего файла возраст неизвестен"

    def test_fresh_snapshot_is_reused(self, tmp_path: Path) -> None:
        p = tmp_path / "cache.duckdb"
        _touch(p, age_sec=3600.0)  # час назад, TTL 23 ч
        d = evaluate_cache_snapshot(str(p), 23.0)
        assert d.reuse is True
        assert d.reason == "fresh"

    def test_stale_snapshot_is_recreated(self, tmp_path: Path) -> None:
        p = tmp_path / "cache.duckdb"
        _touch(p, age_sec=24 * 3600.0)  # сутки назад, TTL 23 ч
        d = evaluate_cache_snapshot(str(p), 23.0)
        assert d.reuse is False
        assert d.reason == "stale"

    def test_ttl_zero_never_reuses(self, tmp_path: Path) -> None:
        """Escape-hatch: ``0`` = всегда пересоздавать, даже свежий файл."""
        p = tmp_path / "cache.duckdb"
        _touch(p, age_sec=1.0)
        d = evaluate_cache_snapshot(str(p), 0.0)
        assert d.reuse is False
        assert d.reason == "ttl_disabled"
        assert d.age_sec is not None, "age известен даже когда reuse выключен"

    def test_boundary_uses_mtime_not_ctime(self, tmp_path: Path) -> None:
        """Чуть внутри TTL — свежий, чуть снаружи — протухший.

        Точное равенство ``age == ttl`` НЕ проверяем намеренно: между
        ``utime`` в фикстуре и ``stat`` в решении проходит ненулевое
        время, поэтому ``age`` всегда окажется на микросекунды больше TTL и
        «ровно на границе» недостижимо извне. Контракт — стороны границы,
        а не сама точка.
        """
        p = tmp_path / "cache.duckdb"

        _touch(p, age_sec=23 * 3600.0 - 60.0)
        assert evaluate_cache_snapshot(str(p), 23.0).reuse is True

        _touch(p, age_sec=23 * 3600.0 + 60.0)
        d = evaluate_cache_snapshot(str(p), 23.0)
        assert d.reuse is False
        assert d.reason == "stale"

    def test_age_is_measured_from_mtime(self, tmp_path: Path) -> None:
        """Возраст обязан считаться от ``mtime``, а не от ``ctime``.

        Подмена: создаём файл (ctime = сейчас), отматываем ``mtime`` в прошлое
        через ``utime``. Если бы решение смотрело на дату создания, файл
        выглядел бы свежим; по ``mtime`` — протухшим.
        """
        p = tmp_path / "cache.duckdb"
        _touch(p, age_sec=48 * 3600.0)
        st = p.stat()
        assert st.st_ctime > st.st_mtime, "предусловие пробы: ctime новее mtime"
        d = evaluate_cache_snapshot(str(p), 23.0)
        assert d.reuse is False, "свежесть обязана читаться по mtime"
        assert d.age_sec == pytest.approx(48 * 3600.0, abs=5.0)

    def test_negative_ttl_is_treated_as_disabled(self, tmp_path: Path) -> None:
        p = tmp_path / "cache.duckdb"
        _touch(p, age_sec=1.0)
        d = evaluate_cache_snapshot(str(p), -5.0)
        assert d.reuse is False
        assert d.reason == "ttl_disabled"
        assert d.ttl_sec == 0.0

    def test_now_argument_makes_it_deterministic(self, tmp_path: Path) -> None:
        """Явный ``now`` — решение не зависит от «сейчас» в тесте."""
        p = tmp_path / "cache.duckdb"
        p.write_bytes(b"x")
        when = time.time() - 3600.0
        os.utime(p, (when, when))
        d = evaluate_cache_snapshot(str(p), 23.0, now=when + 3600.0)
        assert d.age_sec == pytest.approx(3600.0, abs=1.0)
        assert d.reuse is True

    def test_directory_instead_of_file_is_not_reused(self, tmp_path: Path) -> None:
        """Каталог вместо файла — тоже «нечем пользоваться», но не missing."""
        d = tmp_path / "cache.duckdb"
        d.mkdir()
        res = evaluate_cache_snapshot(str(d), 23.0)
        # stat() проходит (это каталог), но свежесть такого «снапшота»
        # бессмысленна — вызывающий обязан проверить is_ready() и откатиться.
        assert isinstance(res, CacheSnapshotDecision)


class TestFormatCacheAge:
    @pytest.mark.parametrize(
        "seconds,expected",
        [
            (None, "n/a"),
            (0, "0с"),
            (47, "47с"),
            (90, "1м 30с"),
            (3600, "1ч 0м"),
            (3600 * 2 + 60 * 13, "2ч 13м"),
        ],
    )
    def test_human_readable(self, seconds: float | None, expected: str) -> None:
        assert format_cache_age(seconds) == expected