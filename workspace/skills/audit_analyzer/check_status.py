import duckdb

conn = duckdb.connect('cache/audit_cache.duckdb')
possible_statuses = ['закрыто', 'завершено', 'completed', 'finalized']

for status in possible_statuses:
    result = conn.execute(f'''
    SELECT COUNT(*) AS closed_audits
    FROM oarb.audits
    WHERE status LIKE '%{status}%' 
    AND EXTRACT(YEAR FROM actual_date) IN (2023, 2024)
    ''').fetchall()
    print(f'{status}: {result}')