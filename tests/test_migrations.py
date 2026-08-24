"""Unit-тесты ``tools/migrate.py`` (без БД: discovery/чекsums/статусы)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import migrate  # noqa: E402


@pytest.fixture()
def mig_dir(tmp_path: Path) -> Path:
    d = tmp_path / "migrations"
    d.mkdir()
    return d


class TestDiscover:
    def test_sorted_by_version(self, mig_dir: Path) -> None:
        (mig_dir / "V002__second.sql").write_text("SELECT 2;", encoding="utf-8")
        (mig_dir / "V001__first.sql").write_text("SELECT 1;", encoding="utf-8")
        migs = migrate.discover(mig_dir)
        assert [m.version for m in migs] == ["001", "002"]
        assert migs[0].name == "first"

    def test_duplicate_version_rejected(self, mig_dir: Path) -> None:
        (mig_dir / "V001__a.sql").write_text("SELECT 1;", encoding="utf-8")
        (mig_dir / "V001__b.sql").write_text("SELECT 2;", encoding="utf-8")
        with pytest.raises(SystemExit, match="Дубликат"):
            migrate.discover(mig_dir)

    def test_non_matching_files_ignored(self, mig_dir: Path) -> None:
        (mig_dir / "schema_migrations.sql").write_text("SELECT 1;", encoding="utf-8")
        (mig_dir / "README.md").write_text("x", encoding="utf-8")
        assert migrate.discover(mig_dir) == []


class TestChecksum:
    def test_stable_and_whitespace_insensitive(self) -> None:
        a = migrate.compute_checksum("SELECT 1;\n\nSELECT 2;")
        b = migrate.compute_checksum("-- comment\nSELECT 1;\nSELECT 2;   ")
        c = migrate.compute_checksum("select 2;\nselect 1;")
        assert a == b
        assert a != c

    def test_real_change_detected(self) -> None:
        a = migrate.compute_checksum("CREATE TABLE t (id int);")
        b = migrate.compute_checksum("CREATE TABLE t (id bigint);")
        assert a != b


class TestStatusLogic:
    def test_status_exit_codes(self, capsys) -> None:
        migs = [
            migrate.Migration("001", "a", Path("-"), "c1", ""),
            migrate.Migration("002", "b", Path("-"), "c2", ""),
        ]
        applied = {"001": ("a", "c1"), "002": ("b", "OLD")}
        assert migrate.cmd_status(migs, applied) == 1
        out = capsys.readouterr().out
        assert "DRIFT!" in out and "PENDING" not in out.replace("ORPHAN?", "")

        applied_ok = {"001": ("a", "c1"), "002": ("b", "c2")}
        assert migrate.cmd_status(migs, applied_ok) == 0


class TestRealMigrationsDir:
    def test_project_migrations_discoverable(self) -> None:
        migs = migrate.discover()
        assert len(migs) >= 1
        assert migs[0].version == "001"
        assert migs[0].name == "baseline"
