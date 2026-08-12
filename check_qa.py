import re
import psycopg2

dsn = [l.split("=", 1)[1].strip() for l in open(".secrets.env", encoding="utf-8")
       if l.startswith("DATABASE_URL")][0]
conn = psycopg2.connect(dsn, gssencmode="disable")
cur = conn.cursor()
cur.execute("select request_id, session_id, user_id, agent_id, status, left(coalesce(summary,''), 80) from question_runs order by created_at desc limit 10")
print("question_runs:")
for r in cur.fetchall():
    print("  ", r)
cur.execute("select event_type, request_id, left(coalesce(payload->>'content',''), 60), left(coalesce(summary,''),60) from gateway_logs where event_type in ('inbound','outbound_final') order by timestamp desc limit 10")
print("gateway_logs inbound/outbound:")
for r in cur.fetchall():
    print("  ", r)
conn.close()
