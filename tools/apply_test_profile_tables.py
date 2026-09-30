"""apply_test_profile_tables.py — создать 5 runtime-таблиц профиля test.

Эти таблицы перечислены в profiles/test.jsonc:
    channels.postgres.table_name     = public.agent_conversation_messages_test
    channels.postgres.messages_table = public.agent_session_messages_test
    channels.postgres.meta_table     = public.agent_session_meta_test
    logging.db.table_name            = public.agent_gateway_logs_test
    logging.db.question_runs_table   = public.agent_question_runs_test

Применяет 5 create-скриптов из sql/<domain>/create_public_agent_*_test.sql
через psycopg2 (DDL разбивается на отдельные statement'ы по ';').

Запуск::

    set DATABASE_URL=postgresql://postgres:1@localhost:5432/postgres
    python tools/apply_test_profile_tables.py

Идемпотентно: CREATE TABLE / CREATE INDEX используют IF NOT EXISTS;
ALTER в create_public_agent_gateway_logs_test идемпотентен.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import psycopg2

DSN_ENV = "DATABASE_URL"

ROOT = Path(__file__).resolve().parents[1]

FILES = [
    ROOT / "sql/channels/create_public_agent_conversation_messages_test.sql",
    ROOT / "sql/session/create_public_agent_session_meta_test.sql",
    ROOT / "sql/session/create_public_agent_session_messages_test.sql",
    ROOT / "sql/logs/create_public_agent_question_runs_test.sql",
    ROOT / "sql/logs/create_public_agent_gateway_logs_test.sql",
]


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
