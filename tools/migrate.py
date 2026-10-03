"""migrate.py — runner миграций схемы (sql/migrations/V*.sql).

Порядок версий определяется номером в имени файла: ``V001__name.sql``.
Каждая применённая версия записывается в ``public.schema_migrations``
с SHA256-контрольной суммой содержимого; повторное применение
пропускается, изменение уже применённого файла — ошибка (drift).

Использование::

    python tools/migrate.py --status            # таблица состояния
    python tools/migrate.py --dry-run           # показать SQL ожидающих
    python tools/migrate.py --apply             # применить ожидающие по порядку
    python tools/migrate.py --apply --target 3  # применять до V003 включительно
    python tools/migrate.py --baseline          # штамповать все версии как применённые
    python tools/migrate.py --verify            # сверить checksums применённых

DSN: переменная окружения ``DATABASE_URL`` или ключ
``channels.postgres.dsn`` из project.json.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from config import get_setting  # noqa: E402

MIGRATIONS_DIR = _ROOT / "sql" / "migrations"
NAME_RE = re.compile(r"^V(\d+)__(\w+)\.sql$")
TRACKING_TABLE = "public.schema_migrations"


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    path: Path
    checksum: str
    sql: str


def compute_checksum(sql: str) -> str:
    """SHA256 нормализованного SQL (без комментариев-строк и хвостовых пробелов)."""
    lines = [
        ln.rstrip() for ln in sql.splitlines() if ln.strip() and not ln.strip().startswith("--")
    ]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def discover(directory: Path | None = None) -> list[Migration]:
    """Найти и отсортировать файлы миграций; дубликаты номеров — ошибка."""
    d = directory or MIGRATIONS_DIR
    found: dict[str, Migration] = {}
    for f in sorted(d.glob("V*__*.sql")):
        m = NAME_RE.match(f.name)
        if not m:
            continue
        version, name = m.group(1), m.group(2)
        if version in found:
            raise SystemExit(f"Дубликат номера миграции V{version}: {f.name}")
        sql = f.read_text(encoding="utf-8")
        found[version] = Migration(version, name, f, compute_checksum(sql), sql)
    return [found[v] for v in sorted(found)]


def resolve_dsn() -> str:
    """DATABASE_URL из окружения или channels.postgres.dsn из конфига."""
    dsn = os.environ.get("DATABASE_URL") or get_setting(
        "channels", "postgres", "dsn", default=""
    )
    if not dsn:
        raise SystemExit(
            "DSN не найден: задайте DATABASE_URL или channels.postgres.dsn"
        )
    return str(dsn)


def is_greenplum(conn) -> bool:  # noqa: ANN001 — psycopg2 connection
    """Greenplum называет себя в ``version()``.

    Ответ важен не для красоты, а для DDL ниже: Greenplum не создаёт таблицу с
    первичным ключом, если не задано ``DISTRIBUTED BY``, а обычный PostgreSQL
    такую клаузу не понимает вовсе. Клауза выбирается по факту движка, а не
    по предположению о том, где запущен runner.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT version()")
        row = cur.fetchone()
    return bool(row) and "greenplum" in str(row[0]).lower()


def ensure_tracking_table(conn) -> None:  # noqa: ANN001 — psycopg2 connection
    # ``version`` — первичный ключ, поэтому на Greenplum он же обязан быть и
    # ключом распределения: без ``DISTRIBUTED BY`` такую таблицу движок не
    # создаст вовсе, и реестр миграций, а с ним и любое применение, не
    # поднимется.
    distributed = "DISTRIBUTED BY (version)" if is_greenplum(conn) else ""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TRACKING_TABLE} (
                version     text PRIMARY KEY,
                name        text NOT NULL,
                checksum    text NOT NULL,
                applied_at  timestamptz NOT NULL DEFAULT now(),
                applied_by  text NOT NULL DEFAULT current_user,
                duration_ms integer
            )
            {distributed}
            """
        )


def fetch_applied(conn) -> dict[str, tuple[str, str]]:  # noqa: ANN001
    """version → (name, checksum) уже применённых."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT version, name, checksum FROM {TRACKING_TABLE} ORDER BY version"
        )
        return {row[0]: (row[1], row[2]) for row in cur.fetchall()}


def apply_migration(conn, mig: Migration, force: bool = False) -> bool:  # noqa: ANN001
    """Применить одну мигранцию в транзакции; True если применилась."""
    cur = conn.cursor()
    try:
        started = time.monotonic()
        cur.execute(mig.sql)
        cur.execute(
            f"INSERT INTO {TRACKING_TABLE} (version, name, checksum, duration_ms) "
            "VALUES (%s, %s, %s, %s)",
            (
                mig.version,
                mig.name,
                mig.checksum,
                int((time.monotonic() - started) * 1000),
            ),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def stamp_migration(conn, mig: Migration) -> None:  # noqa: ANN001
    cur = conn.cursor()
    try:
        # Не ON CONFLICT DO NOTHING: конструкция появилась в PostgreSQL 9.5, а
        # ядро Greenplum 6.5 — 9.4. Проверка и вставка идут в одной транзакции,
        # что для одиночного запуска даёт ту же гарантию. Гонку двух
        # одновременных ``--baseline`` в честном случае не сглаживаем: она
        # обязана упасть на нарушении первичного ключа, потому что выигравшая
        # из двух штамповок всё равно оставила бы базу в состоянии, которого
        # никто не планировал.
        cur.execute(
            f"SELECT 1 FROM {TRACKING_TABLE} WHERE version = %s", (mig.version,)
        )
        if cur.fetchone() is None:
            cur.execute(
                f"INSERT INTO {TRACKING_TABLE} (version, name, checksum, duration_ms) "
                "VALUES (%s, %s, %s, 0)",
                (mig.version, mig.name, mig.checksum),
            )
        conn.commit()
    finally:
        cur.close()


def cmd_status(migs: list[Migration], applied: dict[str, tuple[str, str]]) -> int:
    print(f"{'версия':8} {'статус':12} {'имя':24} checksum")
    for m in migs:
        if m.version not in applied:
            status = "PENDING"
        elif applied[m.version][1] != m.checksum:
            status = "DRIFT!"
        else:
            status = "applied"
        print(f"V{m.version:<7} {status:12} {m.name:24} {m.checksum[:12]}")
    extra = set(applied) - {m.version for m in migs}
    for v in sorted(extra):
        print(f"V{v:<7} {'ORPHAN?':12} {applied[v][0]:24} (нет файла)")
    return 1 if any(
        m.version in applied and applied[m.version][1] != m.checksum for m in migs
    ) else 0


def cmd_apply(
    conn,
    migs: list[Migration],
    applied: dict[str, tuple[str, str]],
    target: str | None,
    force: bool,
) -> int:
    pending = [m for m in migs if m.version not in applied]
    if target:
        pending = [m for m in pending if m.version <= target]
    drifted = [
        m
        for m in migs
        if m.version in applied and applied[m.version][1] != m.checksum
    ]
    if drifted and not force:
        for m in drifted:
            print(f"DRIFT: V{m.version} ({m.name}) изменён после применения")
        print("Измените БД вручную или используйте --force")
        return 1
    if not pending:
        print("Нет ожидающих миграций.")
        return 0
    for m in pending:
        t0 = time.monotonic()
        apply_migration(conn, m, force=force)
        print(f"OK   V{m.version} {m.name} ({int((time.monotonic() - t0) * 1000)} ms)")
    return 0


def cmd_verify(migs: list[Migration], applied: dict[str, tuple[str, str]]) -> int:
    bad = [m for m in migs if m.version in applied and applied[m.version][1] != m.checksum]
    if bad:
        for m in bad:
            print(f"MISMATCH V{m.version}: file={m.checksum[:12]} db={applied[m.version][1][:12]}")
        return 1
    print(f"OK: {len(applied)} применённых, расхождений нет.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--status", action="store_true", help="таблица состояния")
    group.add_argument("--dry-run", action="store_true", help="SQL ожидающих миграций")
    group.add_argument("--apply", action="store_true", help="применить ожидающие")
    group.add_argument("--verify", action="store_true", help="сверить checksums")
    group.add_argument("--baseline", action="store_true", help="штамповать без выполнения")
    parser.add_argument("--target", help="применять/штамповать до этой версии включительно")
    parser.add_argument("--force", action="store_true", help="применять при drift")
    args = parser.parse_args(argv)

    migs = discover()
    if args.dry_run:
        for m in migs:
            print(f"-- ===== V{m.version} {m.name} =====")
            print(m.sql)
        return 0

    import psycopg2

    conn = psycopg2.connect(resolve_dsn())
    try:
        ensure_tracking_table(conn)
        conn.commit()
        applied = fetch_applied(conn)
        if args.status:
            return cmd_status(migs, applied)
        if args.verify:
            return cmd_verify(migs, applied)
        if args.baseline:
            targets = [m for m in migs if not args.target or m.version <= args.target]
            for m in targets:
                stamp_migration(conn, m)
                print(f"STAMPED V{m.version} {m.name}")
            return 0
        if args.apply:
            return cmd_apply(conn, migs, applied, args.target, args.force)
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
