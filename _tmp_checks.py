import json
for f in ['hard-code-test-fix', 'hard-data-pipeline']:
    d = json.load(open(f'benchmarks/results/runs/2026-06-10_13-27-10/detail/{f}.json'))
    print(f'=== {f} ({d["total_score"]:.1%}) ===')
    for s in d.get('steps', []):
        fails = [c for c in s['checks'] if not c['passed'] and c['check'] != 'llm_judge']
        if fails:
            print(f'  Step {s["step"]} (w={s["weight"]}): {s["score"]:.1%}')
            for c in fails:
                detail = c.get('detail', '')
                print(f'    [FAIL] {c["check"]}: {detail[:150]}')
    print()
