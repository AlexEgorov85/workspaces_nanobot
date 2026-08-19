"""Интеграционные тесты мульти-машинного пула воркеров (гейт «точно устранит»).

Проверяются на РЕАЛЬНОЙ БД (PostgreSQL/Greenplum 6.5) в отдельной тестовой
схеме ``test_worker_pool_<rand>`` (создаётся и удаляется автоматически).

Запуск (opt-in, прод-БД не трогается без явного флага)::

    $env:NANOBOT_INTEGRATION = "1"
    python -m pytest tests/integration/test_worker_pool_concurrency.py -v

Кейсы:
  C1  exclusive claim  — каждый task_id заклеймлен ровно одним воркером;
  C2  failed-терминал  — задача ``failed`` никогда не берётся в работу;
  C3  error-retry      — ``error`` берётся в работу только после backoff;
  C4  invariant/heal   — рассинхрон двух таблиц схлопывается свипом;
  C5  reclaim          — задачи «мёртвого» воркера возвращаются в пул по lease.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.integration

_project_root = Path(__file__).resolve().parent.parent.parent
_workspace_path = str(_project_root / "workspace")
if _workspace_path not in sys.path:
    sys.path.insert(0, _workspace_path)


# ---------------------------------------------------------------------------
# Хелперы БД
# ---------------------------------------------------------------------------


def _resolve_dsn() -> str:
    dsn = os.environ.get("DATABASE_URL") or ""
    if dsn:
        return dsn
    try:
        from config import SETTINGS

        pg = (SETTINGS.get("channels") or {}).get("postgres") or {}
        return pg.get("dsn") or ""
    except Exception:
        return ""


def _connect(dsn: str):
    import psycopg2

    return psycopg2.connect(dsn, gssencmode="disable")


def _exec(dsn: str, sql: str, params: list | None = None, fetch: bool = False):
    import psycopg2.extras

    conn = _connect(dsn)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params or [])
            rows = cur.fetchall() if fetch else None
        conn.commit()
        return rows
    finally:
        conn.close()


_MSG_DDL = """
CREATE TABLE IF NOT EXISTS "{schema}".agent_conversation_messages (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    chat_id TEXT,
    user_id TEXT,
    role TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    media JSONB DEFAULT '[]'::jsonb,
    metadata JSONB DEFAULT '{{}}'::jsonb,
    reply_to UUID,
    buttons JSONB DEFAULT '[]'::jsonb,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id)
)
"""

_CLAIM_DDL = """
CREATE TABLE IF NOT EXISTS "{schema}".agent_worker_claims (
    task_id UUID NOT NULL PRIMARY KEY,
    worker_id TEXT NOT NULL,
    claimed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_until TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


# ---------------------------------------------------------------------------
# Фикстура окружения (opt-in через NANOBOT_INTEGRATION=1)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
def pg_env() -> dict:
    dsn = _resolve_dsn()
    if os.environ.get("NANOBOT_INTEGRATION") != "1":
        pytest.skip("integration: set NANOBOT_INTEGRATION=1 (+ DATABASE_URL)")
    if not dsn:
        pytest.skip("integration: DATABASE_URL не настроен")
    try:
        conn = _connect(dsn)
        conn.close()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"integration: БД недоступна ({exc})")

    schema = f"test_worker_pool_{uuid.uuid4().hex[:8]}"
    _exec(dsn, f'CREATE SCHEMA "{schema}"')
    _exec(dsn, _MSG_DDL.format(schema=schema))
    _exec(dsn, _CLAIM_DDL.format(schema=schema))

    # Направляем глобальный пул utils.db на ту же БД (тестовая схема).
    from utils import db as _db

    _db.configure(dsn)

    yield {"dsn": dsn, "schema": schema}

    try:
        _exec(dsn, f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    except Exception:
        pass


def _insert_task(
    pg: dict,
    task_id: str,
    chat_id: str,
    status: str = "pending",
    updated_age_sec: int = 0,
    retry_count: int | None = None,
) -> None:
    dsn, schema = pg["dsn"], pg["schema"]
    meta = "{}" if retry_count is None else f'{{"retry_count": {retry_count}}}'
    _exec(
        dsn,
        f'INSERT INTO "{schema}".agent_conversation_messages '
        f"(id, chat_id, user_id, role, content, metadata, status, updated_at) "
        f"VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s, NOW() - interval '1 second' * %s)",
        [task_id, chat_id, f"user-{chat_id}", "user", f"task {task_id}",
         meta, status, updated_age_sec],
    )


def _insert_claim(pg: dict, task_id: str, worker_id: str, lease_sec: float) -> None:
    dsn, schema = pg["dsn"], pg["schema"]
    _exec(
        dsn,
        f'INSERT INTO "{schema}".agent_worker_claims '
        f"(task_id, worker_id, lease_until) VALUES (%s,%s, NOW() + interval '1 second' * %s)",
        [task_id, worker_id, lease_sec],
    )


def _user_rows(pg: dict) -> list[dict]:
    dsn, schema = pg["dsn"], pg["schema"]
    return _exec(
        dsn,
        f'SELECT id::text, chat_id, status FROM "{schema}".agent_conversation_messages '
        f"WHERE role = %s ORDER BY id",
        ["user"],
        fetch=True,
    )


def _claim_rows(pg: dict) -> list[dict]:
    dsn, schema = pg["dsn"], pg["schema"]
    return _exec(
        dsn,
        f'SELECT task_id::text, worker_id FROM "{schema}".agent_worker_claims',
        fetch=True,
    )


def _make_channel(pg: dict, worker_id: str, base_dir: Path):
    from lib.channels.postgres_channel import PostgresChannel
    from utils.session_file_store import SessionFileStore

    config = {
        "dsn": pg["dsn"],
        "schema": pg["schema"],
        "table_name": "agent_conversation_messages",
        "claims_table": "agent_worker_claims",
        "poll_interval": 0.05,
        "flush_interval": 0.05,
        "max_concurrent": 8,
        "processing_timeout": 10,
        "max_stuck_retries": 3,
        "error_retry_delay": 5.0,
        "lease_interval": 5.0,
        "worker_id": worker_id,
        "_file_store": SessionFileStore(base_dir, attachments_subdir="attachments"),
    }
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()
    return PostgresChannel(config, bus)


# ---------------------------------------------------------------------------
# C1: exclusive claim
# ---------------------------------------------------------------------------


async def test_c1_exclusive_claim(pg_env: dict, tmp_path: Path) -> None:
    n = 6
    ids = [str(uuid.uuid4()) for _ in range(n)]
    for i, tid in enumerate(ids):
        _insert_task(pg_env, tid, f"chat-{i}")

    w1 = _make_channel(pg_env, "worker-1", tmp_path / "w1")
    w2 = _make_channel(pg_env, "worker-2", tmp_path / "w2")

    # Гоняем обоих воркеров за одними и теми же задачами с ограниченной
    # конкурентностью (не превышая размер глобального пула utils.db), чтобы
    # исключить двойной захват одной задачи двумя воркерами.
    sem = asyncio.Semaphore(2)

    async def attempt(w: object) -> None:
        async with sem:
            await w._poll_once(w.exchange)

    await asyncio.gather(
        *(attempt(w1 if i % 2 == 0 else w2) for i in range(n * 2)),
    )

    processing = [r for r in _user_rows(pg_env) if r["status"] == "processing"]
    assert len(processing) == n, "все задачи должны быть взяты в работу"

    claims = _claim_rows(pg_env)
    assert len(claims) == n, "на каждую задачу — ровно одна аренда"
    assert len({c["task_id"] for c in claims}) == n, "аренды уникальны (нет двойного захвата)"
    assert {c["task_id"] for c in claims} == {r["id"] for r in processing}


# ---------------------------------------------------------------------------
# C2: failed — терминальный статус
# ---------------------------------------------------------------------------


async def test_c2_failed_is_terminal(pg_env: dict, tmp_path: Path) -> None:
    failed_id = str(uuid.uuid4())
    pending_id = str(uuid.uuid4())
    _insert_task(pg_env, failed_id, "chat-f", status="failed")
    _insert_task(pg_env, pending_id, "chat-p")

    w1 = _make_channel(pg_env, "worker-1", tmp_path)
    await w1._poll_once(w1.exchange)

    statuses = {r["id"]: r["status"] for r in _user_rows(pg_env)}
    assert statuses[failed_id] == "failed", "failed не берётся в работу"
    assert statuses[pending_id] == "processing"

    claim_ids = {c["task_id"] for c in _claim_rows(pg_env)}
    assert failed_id not in claim_ids, "для failed не должно быть аренды"


# ---------------------------------------------------------------------------
# C3: error — повторный захват только после backoff
# ---------------------------------------------------------------------------


async def test_c3_error_backoff_and_retry(pg_env: dict, tmp_path: Path) -> None:
    fresh = str(uuid.uuid4())
    stale = str(uuid.uuid4())
    _insert_task(pg_env, fresh, "chat-fr", status="error", updated_age_sec=0)
    _insert_task(pg_env, stale, "chat-st", status="error", updated_age_sec=3600)

    w1 = _make_channel(pg_env, "worker-1", tmp_path)
    await w1._poll_once(w1.exchange)

    statuses = {r["id"]: r["status"] for r in _user_rows(pg_env)}
    assert statuses[fresh] == "error", "свежая error в backoff — не берётся"
    assert statuses[stale] == "processing", "старая error после backoff — берётся"


# ---------------------------------------------------------------------------
# C4: invariant / heal — рассинхрон двух таблиц схлопывается
# ---------------------------------------------------------------------------


async def test_c4_heal_desync(pg_env: dict, tmp_path: Path) -> None:
    # (a) аренда на completed-задачу — мусор
    done = str(uuid.uuid4())
    _insert_task(pg_env, done, "chat-d", status="completed")
    _insert_claim(pg_env, done, "w-x", 60)

    # (b) processing без аренды и с устаревшим updated_at — осиротевшая
    orphan = str(uuid.uuid4())
    _insert_task(pg_env, orphan, "chat-o", status="processing", updated_age_sec=3600)

    w1 = _make_channel(pg_env, "worker-1", tmp_path)
    await w1._reclaim_and_heal()

    claim_ids = {c["task_id"] for c in _claim_rows(pg_env)}
    assert done not in claim_ids, "аренда на completed — удалена свипом"

    statuses = {r["id"]: r["status"] for r in _user_rows(pg_env)}
    assert statuses[orphan] == "error", "processing-без-аренды возвращается в пул"


# ---------------------------------------------------------------------------
# C5: reclaim — «мёртвый» воркер освобождает задачи по истечении lease
# ---------------------------------------------------------------------------


async def test_c5_reclaim_expired_lease(pg_env: dict, tmp_path: Path) -> None:
    tid = str(uuid.uuid4())
    _insert_task(pg_env, tid, "chat-r", status="processing")
    _insert_claim(pg_env, tid, "dead-worker", -10)

    w1 = _make_channel(pg_env, "worker-1", tmp_path)
    await w1._reclaim_and_heal()

    statuses = {r["id"]: r["status"] for r in _user_rows(pg_env)}
    assert statuses[tid] == "pending", "просроченная аренда → задача вернулась в пул"

    assert _claim_rows(pg_env) == [], "аренда удалена после reclaim"

    await w1._poll_once(w1.exchange)
    statuses = {r["id"]: r["status"] for r in _user_rows(pg_env)}
    assert statuses[tid] == "processing", "задача снова взята в работу"
