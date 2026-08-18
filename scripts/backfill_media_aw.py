#!/usr/bin/env python3
"""Backfill legacy-медиа в AW-формат для колонки ``media``.

Исторические строки в ``agent_conversation_messages.media`` хранят
вложения в старом формате ``{"filename": ..., "data": "data:..."}``.
AW (audit_point_new) читает ``file_id``/``mime_type``/``file_size``, поэтому
у старых ответов нет превью/кнопки «Скачать». Скрипт однократно приводит
такие записи к новому формату ``{"filename", "file_id", "mime_type",
"file_size"}`` через общий кодек ``utils.media.normalize_storage_entry``.

Безопасность:
  * идемпотентен — переписывает только dict-элементы с ``data`` и без
    ``file_id``; всё остальное (path, http, уже новые) не трогает;
  * работает батчами по ключу ``id`` (без OFFSET — корректно для
    Greenplum-распределённой таблицы);
  * ``--dry-run`` — только подсчёт, без записи.

Использование:
    python scripts/backfill_media_aw.py --dsn "postgresql://..."
    python scripts/backfill_media_aw.py --dsn ... --dry-run
    python scripts/backfill_media_aw.py --dsn ... --schema my --table t
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_WORKSPACE = _REPO / "workspace"
for _p in (str(_REPO), str(_WORKSPACE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from utils.db import configure, execute, fetch, start, shutdown  # noqa: E402
from utils.media import normalize_storage_entry  # noqa: E402


def _decode_media(raw: object) -> list:
    """media приходит из psycopg2 как list или как JSON-строка."""
    if raw is None:
        return []
    if isinstance(raw, str):
        return json.loads(raw) if raw else []
    if isinstance(raw, list):
        return raw
    return []


def _normalize_row(media_raw: object) -> tuple[list | None, int]:
    """Вернуть (новая_media_или_None_если_без_изменений, сколько элементов изменено)."""
    media = _decode_media(media_raw)
    changed = 0
    new_media = []
    for entry in media:
        orig = entry if isinstance(entry, dict) else str(entry) if entry else None
        norm = normalize_storage_entry(entry)
        if isinstance(orig, dict) and isinstance(norm, dict) and (
            norm.get("file_id") != orig.get("file_id")
            or "file_id" in norm and "file_id" not in orig
        ):
            changed += 1
        new_media.append(norm)
    if changed == 0:
        return None, 0
    return new_media, changed


def run(opts: argparse.Namespace) -> int:
    fq_table = f"{opts.schema}.{opts.table}"
    batch = max(1, int(opts.batch_size))

    scanned = 0
    rows_changed = 0
    entries_changed = 0
    errors = 0
    last_id: object = None

    while True:
        if last_id is None:
            rows = fetch(
                f"SELECT id, media FROM {fq_table} ORDER BY id LIMIT {batch}",
            )
        else:
            rows = fetch(
                f"SELECT id, media FROM {fq_table} "
                f"WHERE id > %s ORDER BY id LIMIT {batch}",
                last_id,
            )
        if not rows:
            break
        last_id = rows[-1]["id"]
        scanned += len(rows)

        for row in rows:
            row_id = row["id"]
            try:
                new_media, n_changed = _normalize_row(row["media"])
            except Exception as e:  # повреждённый JSON/тип в строке
                errors += 1
                print(f"  [skip] row {row_id}: {e}")
                continue
            if new_media is None:
                continue
            entries_changed += n_changed
            rows_changed += 1
            if not opts.dry_run:
                execute(
                    f"UPDATE {fq_table} SET media = %s::jsonb, updated_at = NOW() "
                    f"WHERE id = %s",
                    json.dumps(new_media, ensure_ascii=False),
                    row_id,
                )

    print(f"Scanned rows      : {scanned}")
    print(f"Rows changed      : {rows_changed}")
    print(f"Media entries fixed: {entries_changed}")
    print(f"Errors / skipped  : {errors}")
    if opts.dry_run:
        print("DRY-RUN: writes skipped (use without --dry-run to apply).")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dsn", default="", help="Postgres/Greenplum connection DSN")
    ap.add_argument("--schema", default="public")
    ap.add_argument("--table", default="agent_conversation_messages")
    ap.add_argument("--batch-size", default=500, type=int)
    ap.add_argument("--dry-run", action="store_true", help="только подсчёт, без записи")
    opts = ap.parse_args()

    if not opts.dsn:
        print("ERROR: --dsn or env required", file=sys.stderr)
        return 2

    configure(opts.dsn)
    start()
    try:
        return run(opts)
    finally:
        shutdown()


if __name__ == "__main__":
    raise SystemExit(main())