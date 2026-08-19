"""
Диагностика целостности мульти-машинного пула воркеров.

Проверяет инвариант ``processing ⇔ claim`` между
``agent_conversation_messages`` и ``agent_worker_claims`` и выводит отчёт
о рассинхронах. Ничего не меняет (read-only), кроме опционального ``--fix``.

Инвариант:
    - ``processing``-задача имеет ровно одну актуальную claim-запись;
    - claim существует только если задача реально в ``processing``;
    - нет claim с истёкшим lease.

Владелец задачи определяется только по ``agent_worker_claims.worker_id`` —
колонка ``worker_id`` в ``agent_conversation_messages`` не используется.

Запуск (из корня проекта):
    # Только отчёт
    python tools/check_worker_pool_integrity.py

    # Отчёт + исправление рассинхронов (вылечить healing-свипом канала)
    python tools/check_worker_pool_integrity.py --fix

    # Отчёт по кастомной таблице/аренде
    python tools/check_worker_pool_integrity.py --messages public.agent_conversation_messages --claims public.agent_worker_claims

Возвращаемый код:
    0 — целостность соблюдена (или рассинхронов нет);
    1 — найдены рассинхроны (read-only режим).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Принудительно UTF-8 для консоли (надо chcp 65001 в PowerShell)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_WORKSPACE = _ROOT / "workspace"
if str(_WORKSPACE) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE))


def _connect():
    from utils.db import run  # переиспользуем глобальный пул (utils.db)

    return run


def main() -> int:
    ap = argparse.ArgumentParser(description="Проверка целостности пула воркеров")
    ap.add_argument(
        "--messages",
        default=None,
        help="Полное имя таблицы сообщений (default: channels.postgres.table_name)",
    )
    ap.add_argument(
        "--claims",
        default=None,
        help="Полное имя таблицы аренд (default: channels.postgres.claims_table)",
    )
    ap.add_argument(
        "--fix",
        action="store_true",
        help="Исправить найденные рассинхроны (по умолчанию только отчёт)",
    )
    args = ap.parse_args()

    from config import SETTINGS

    pg = (SETTINGS.get("channels") or {}).get("postgres") or {}
    schema = pg.get("schema", "public")
    messages = args.messages or (
        pg.get("table_name") or "agent_conversation_messages"
    )
    claims = args.claims or (pg.get("claims_table") or "agent_worker_claims")
    if "." not in messages:
        messages = f"{schema}.{messages}"
    if "." not in claims:
        claims = f"{schema}.{claims}"

    run = _connect()
    issues: list[str] = []

    def check(sql: str) -> list:
        return run(lambda conn: _rows(conn, sql))

    # 1) processing без claim
    issues.extend(
        "processing без claim: " + str(r["id"])
        for r in check(
            f"SELECT id::text AS id FROM {messages} "
            f"WHERE role='user' AND status='processing' AND NOT EXISTS "
            f"(SELECT 1 FROM {claims} c WHERE c.task_id = {messages}.id)"
        )
    )

    # 2) claim со зрелой (не processing) задачей — висячий мусор
    issues.extend(
        f"claim на непроцессируемую задачу {r['task_id']} ({r['worker_id']})"
        for r in check(
            f"SELECT c.task_id::text AS task_id, c.worker_id FROM {claims} c "
            f"WHERE NOT EXISTS (SELECT 1 FROM {messages} m "
            f"WHERE m.id = c.task_id AND m.status='processing')"
        )
    )

    # 3) claim с истёкшим lease (задача всё ещё processing — воркер мёртв)
    issues.extend(
        f"истёкший lease на {r['task_id']} (worker {r['worker_id']})"
        for r in check(
            f"SELECT c.task_id::text, c.worker_id FROM {claims} c "
            f"WHERE c.lease_until < NOW() AND EXISTS "
            f"(SELECT 1 FROM {messages} m "
            f"WHERE m.id = c.task_id AND m.status='processing')"
        )
    )

    # 4) неоднозначность: несколько claim на одну задачу (PK не должен дать)
    issues.extend(
        f"дубль claim на {r['task_id']} (записей {r['cnt']})"
        for r in check(
            f"SELECT task_id::text AS task_id, count(*) AS cnt "
            f"FROM {claims} GROUP BY task_id HAVING count(*) > 1"
        )
    )

    # 5) orphaned assistant-placeholder в processing без user-пары
    issues.extend(
        f"orphaned assistant placeholder {r['id']}"
        for r in check(
            f"SELECT id::text AS id FROM {messages} m "
            f"WHERE m.role='assistant' AND m.status='processing' AND NOT EXISTS "
            f"(SELECT 1 FROM {messages} u "
            f"WHERE u.id = m.reply_to AND u.role='user')"
        )
    )

    if not issues:
        print(f"[OK] Пул воркеров цел: no row mismatches ({messages} x {claims}).")
        return 0

    print(f"[!] Найдено рассинхронов: {len(issues)}")
    for line in issues:
        print(f"  - {line}")

    if args.fix:
        run(lambda conn: _fix(conn, messages, claims))
        print("[OK] Рассинхроны исправлены healing-свипом.")
        return 0

    print("Запусти с --fix для исправления.")
    return 1


def _rows(conn, sql: str) -> list:
    import psycopg2.extras as _extras

    with conn.cursor(cursor_factory=_extras.RealDictCursor) as cur:
        cur.execute(sql)
        return [dict(r) for r in cur.fetchall()]


def _fix(conn, messages: str, claims: str) -> None:
    """Аналог ``_reclaim_and_heal`` канала: вернуть в пул истёкшие lease,
    вылечить processing-без-claim, убрать висячие аренды и orphaned-заглушки."""
    with conn.cursor() as cur:
        # reclaim: истёкшие lease → задача снова в пул (канал сам решает retry)
        cur.execute(
            f"UPDATE {messages} SET status='pending', updated_at=NOW() "
            f"WHERE status='processing' AND id IN "
            f"(SELECT task_id FROM {claims} WHERE lease_until < NOW())"
        )
        cur.execute(f"DELETE FROM {claims} WHERE lease_until < NOW()")
        # heal: processing без claim → error (повтор после backoff)
        cur.execute(
            f"UPDATE {messages} SET status='error', updated_at=NOW() "
            f"WHERE role='user' AND status='processing' AND NOT EXISTS "
            f"(SELECT 1 FROM {claims} c WHERE c.task_id = {messages}.id)"
        )
        # висячие аренды (задача не в processing) → удалить
        cur.execute(
            f"DELETE FROM {claims} c WHERE NOT EXISTS "
            f"(SELECT 1 FROM {messages} m WHERE m.id = c.task_id "
            f"AND m.status='processing')"
        )
        # orphaned assistant-placeholder → failed
        cur.execute(
            f"UPDATE {messages} SET status='failed', updated_at=NOW() "
            f"WHERE role='assistant' AND status='processing' AND NOT EXISTS "
            f"(SELECT 1 FROM {messages} u WHERE u.id = {messages}.reply_to AND u.role='user')"
        )


if __name__ == "__main__":
    raise SystemExit(main())