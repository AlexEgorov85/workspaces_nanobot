# -*- coding: utf-8 -*-
from pathlib import Path
import duckdb

_SKILL_ROOT = Path(__file__).resolve().parents[2]  # scripts/generated/ -> audit_analyzer/
con = duckdb.connect(str(_SKILL_ROOT / "cache" / "audit_cache.duckdb"), read_only=True)
# Названия хранятся в кодировке latin-1 — декодируем из байтов UTF-8, прочитанных как latin-1
rows = con.execute("SELECT id, title, actual_date FROM oarb.audits WHERE actual_date IS NOT NULL ORDER BY id").fetchall()
for r in rows:
    rid, title_bytes, dt = r
    try:
        title = title_bytes.encode('latin-1').decode('utf-8')
    except Exception:
        title = title_bytes
    print(f"{rid}\t{dt}\t{title}")