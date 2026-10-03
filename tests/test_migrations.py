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


class TestV004Bookkeeping:
    """V004 (agent_gateway_logs.user_id) — change fix-history-search-user-isolation.

    Bookkeeping: runner регистрирует V004 в ``public.schema_migrations``
    через ``apply_migration`` (INSERT после выполнения SQL в одной
    транзакции). Сам SQL-файл НЕ должен содержать фиктивный
    ``COMMENT ON SCHEMA public`` (это не регистрация в tracking-таблице).
    """

    def test_v004_discovered_by_runner(self) -> None:
        migs = migrate.discover()
        versions = {m.version for m in migs}
        assert "004" in versions, (
            "V004 не найден runner'ом — миграция не попадёт в --apply"
        )

    def test_v004_name(self) -> None:
        migs = migrate.discover()
        v004 = next(m for m in migs if m.version == "004")
        assert v004.name == "agent_gateway_logs_user_id"

    def test_v004_has_stable_checksum(self) -> None:
        """Checksum не зависит от комментариев/хвостовых пробелов — повторный
        запуск runner'а даст ту же запись в schema_migrations (без drift)."""
        migs = migrate.discover()
        v004 = next(m for m in migs if m.version == "004")
        # Двойной расчёт — должен совпасть.
        again = migrate.compute_checksum(v004.sql)
        assert v004.checksum == again

    def test_v004_no_marker_comment(self) -> None:
        """V004 НЕ содержит ``COMMENT ON SCHEMA public IS 'V004 ...'`` —
        этот маркер вводит в заблуждение: runner регистрирует версию в
        ``public.schema_migrations`` через INSERT, а не через комментарий
        схемы. Комментарий на схеме не является частью migration lifecycle."""
        migs = migrate.discover()
        v004 = next(m for m in migs if m.version == "004")
        # Упрощённо ищем строковый литерал.
        assert "COMMENT ON SCHEMA" not in v004.sql, (
            "V004 всё ещё содержит COMMENT ON SCHEMA — этот комментарий "
            "не регистрирует версию в schema_migrations и путает "
            "миграционный контракт. Runner делает INSERT после выполнения SQL."
        )

    def test_v004_idempotent(self) -> None:
        """DDL внутри V004 идемпотентен: повторный прогон (после успешного
        применения) не должен падать на ALTER/CREATE INDEX.

        Без этого runner при втором --apply мог бы упасть, и тогда
        tracking-запись в schema_migrations не появилась бы.
        """
        migs = migrate.discover()
        v004 = next(m for m in migs if m.version == "004")
        sql = v004.sql.upper()
        assert "ADD COLUMN IF NOT EXISTS" in sql
        assert "CREATE INDEX IF NOT EXISTS" in sql


# --- совместимость runner'а с Greenplum 6.5 ----------------------------------
#
# Реестр миграций — точка, через которую проходит ЛЮБОЕ изменение схемы. Если
# он не запускается на целевой СУБД, не запускается ничего: ни зеркало сессий,
# ни составной ключ реплики, ни последующие починки. Поэтому его исполняемый
# SQL проверяется отдельно от DDL самих миграций.

GP_VERSION = (
    "PostgreSQL 9.4.26 (Greenplum Database 6.5.29 build commit:abc) "
    "on x86_64-pc-linux-gnu, compiled by GCC gcc (GCC) 4.8.5, 64-bit"
)
PG_VERSION = "PostgreSQL 13.22 (Debian 13.22-1.pgdg120+1) on x86_64-pc-linux-gnu"


class _Cursor:
    """Курсор, отвечающий по тексту запроса, — БД в тестах не поднимается."""

    def __init__(self, conn: "_Conn") -> None:
        self._conn = conn
        self.rowcount = 0

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: object = None) -> None:
        self._conn.statements.append((sql, params))
        self.rowcount = 0

    def fetchone(self) -> tuple[object, ...] | None:
        last = self._conn.statements[-1][0]
        if "version()" in last:
            return (self._conn.version,)
        if self._conn.already_stamped:
            return (1,)
        return None

    def fetchall(self) -> list[tuple[object, ...]]:
        return []

    def close(self) -> None:
        self._conn.closed += 1


class _Conn:
    def __init__(self, version: str, *, already_stamped: bool = False) -> None:
        self.version = version
        self.already_stamped = already_stamped
        self.statements: list[tuple[str, object]] = []
        self.commits = 0
        self.closed = 0

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def commit(self) -> None:
        self.commits += 1

    def verbs(self) -> list[str]:
        return [sql.strip().split(None, 1)[0].upper() for sql, _ in self.statements]


def _a_migration() -> migrate.Migration:
    return migrate.Migration("001", "baseline", Path("V001__baseline.sql"), "abc", "SELECT 1;")


class TestTrackingTableOnGreenplum:
    def test_distribution_clause_is_present_on_greenplum(self) -> None:
        conn = _Conn(GP_VERSION)
        migrate.ensure_tracking_table(conn)
        ddl = " ".join(s for s, _ in conn.statements if "CREATE TABLE" in s)
        assert "DISTRIBUTED BY (version)" in ddl, (
            "Greenplum не создаёт таблицу с первичным ключом без ключа "
            "распределения: без этой клаузы реестр миграций не поднимется и "
            "не применится ни одна миграция"
        )

    def test_distribution_clause_is_absent_on_postgres(self) -> None:
        """Обратная сторона того же решения: ``DISTRIBUTED BY`` — синтаксис
        Greenplum, обычный PostgreSQL его не понимает и такую таблицу не
        создаст. Поэтому клауза выбирается по факту движка."""
        conn = _Conn(PG_VERSION)
        migrate.ensure_tracking_table(conn)
        ddl = " ".join(s for s, _ in conn.statements if "CREATE TABLE" in s)
        assert "PRIMARY KEY" in ddl
        assert "DISTRIBUTED BY" not in ddl

    def test_engine_is_detected_from_version_not_guessed(self) -> None:
        assert migrate.is_greenplum(_Conn(GP_VERSION)) is True
        assert migrate.is_greenplum(_Conn(PG_VERSION)) is False


class TestStampHasNoUpsertSyntax:
    def test_runner_sends_no_upsert_syntax(self) -> None:
        """``ON CONFLICT`` появился в PostgreSQL 9.5, ядро Greenplum 6.5 — 9.4.
        На целевой СУБД ``--baseline`` обязан работать, а не падать на синтаксисе."""
        conn = _Conn(GP_VERSION)
        migrate.stamp_migration(conn, _a_migration())
        for sql, _ in conn.statements:
            assert "ON CONFLICT" not in sql.upper(), (
                f"ON CONFLICT не поддерживается ядром Greenplum 6.5: {sql}"
            )

    def test_absent_version_is_checked_then_inserted(self) -> None:
        conn = _Conn(GP_VERSION, already_stamped=False)
        migrate.stamp_migration(conn, _a_migration())
        assert conn.verbs() == ["SELECT", "INSERT"], (
            "проверка и вставка обязаны идти в этой последовательности: вставка "
            "без проверки упала бы на нарушении первичного ключа"
        )
        assert conn.commits == 1

    def test_present_version_is_not_written_again(self) -> None:
        conn = _Conn(GP_VERSION, already_stamped=True)
        migrate.stamp_migration(conn, _a_migration())
        assert conn.verbs() == ["SELECT"], (
            "версия уже зарегистрирована — повторная вставка не нужна"
        )
