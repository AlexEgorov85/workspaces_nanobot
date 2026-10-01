"""apply_test_profile_tables.py — создать 5 runtime-таблиц профиля test.

Имена таблиц берутся из настроек (``config.runtime_table(role, "test")``),
поэтому переименование таблицы в ``profiles/test.jsonc`` не оставляет
инструмент применяющим DDL от старого имени: отсутствующий файл — ошибка
с именем роли и путём.

Применяет create-скрипты из ``sql/<domain>/create_public_<table>_test.sql``
через psycopg2 (DDL разбивается на отдельные statement'ы по ';').

Запуск::

    set DATABASE_URL=postgresql://postgres:1@localhost:5432/postgres
    python tools/apply_test_profile_tables.py

Идемпотентно: CREATE TABLE / CREATE INDEX используют IF NOT EXISTS;
ALTER в DDL журнала идемпотентен.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import psycopg2

DSN_ENV = "DATABASE_URL"

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import runtime_table  # noqa: E402

#: DDL лежит в ``sql/<domain>/create_<schema>_<table>.sql``, то есть имя файла
#: выводится из имени таблицы. Переименование таблицы в профиле без
#: переименования DDL должно падать здесь и называть файл, а не молча
#: применять старую схему. Список, а не словарь: каталог ``sql/session``
#: отдаёт две разные таблицы.
_DDL: tuple[tuple[str, str], ...] = (
    ("sql/channels", "conversation_messages"),
    ("sql/session", "session_meta"),
    ("sql/session", "session_messages"),
    ("sql/logs", "question_runs"),
    ("sql/logs", "gateway_logs"),
)


def ddl_files() -> list[Path]:
    out: list[Path] = []
    for directory, role in _DDL:
        table = runtime_table(role, "test")
        path = ROOT / directory / f"create_public_{table}.sql"
        if not path.exists():
            raise FileNotFoundError(
                f"DDL для роли {role!r} не найден: {path}\n"
                f"Имя таблицы взято из профиля test ({table!r}); "
                f"переименуйте файл DDL вместе с таблицей."
            )
        out.append(path)
    return out


FILES = ddl_files()


def split_statements(sql: str) -> list[str]:
    out: list[str] = []
    buf: list[str] = []
    for line in sql.splitlines():
        if line.lstrip().startswith("--"):
            continue
        buf.append(line)
        if line.rstrip().endswith(";"):
            stmt = "\n".join(buf).strip()
            if stmt and stmt != ";":
                out.append(stmt)
            buf = []
    tail = "\n".join(buf).strip()
    if tail and tail != ";":
        out.append(tail)
    return out


def main() -> int:
    dsn = os.environ.get(DSN_ENV)
    if not dsn:
        print(f"{DSN_ENV} не задан", file=sys.stderr)
        return 2
    for f in FILES:
        if not f.exists():
            print(f"missing: {f}", file=sys.stderr)
            return 2

    with psycopg2.connect(dsn) as conn:
        for f in FILES:
            text = f.read_text(encoding="utf-8")
            stmts = split_statements(text)
            print(f"== {f.relative_to(ROOT)}: {len(stmts)} statements")
            with conn.cursor() as cur:
                for i, stmt in enumerate(stmts, 1):
                    head = stmt.splitlines()[0][:60]
                    try:
                        cur.execute(stmt)
                    except Exception as e:
                        print(
                            f"  FAIL stmt #{i} ({head!r}): {type(e).__name__}: {str(e)[:160]}",
                            file=sys.stderr,
                        )
                        conn.rollback()
                        return 1
                    print(f"  ok #{i}: {head}")
        conn.commit()
    print("ALL_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
