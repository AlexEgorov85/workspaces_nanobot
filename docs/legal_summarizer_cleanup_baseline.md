# Legal Summarizer Cleanup — Baseline

Зафиксирован **перед** началом миграции по `PLAN.md` (50 этапов).

## Baseline

```text
BASE_COMMIT=2008e7d1183d07863b01ed655be0a96ae9277cfe
TEST_TOTAL=373
TEST_PASSED=369
TEST_FAILED=0
TEST_SKIPPED=4
```

Тесты собираются и зелёные. Working tree чистый (`git status --short` — пусто).

## Контекст

- HEAD: `refactor: удалить cached_retrieval.py, provenance_reconstruction.py`
- Последние коммиты — это уже шаги плана (canonical pipeline построен, многие legacy
  модули уже удалены). Это **стартовая точка для финального cleanup**, не начало с нуля.
- Все последующие изменения сравниваются с этим baseline:
  `TEST_TOTAL ≥ 373`, `TEST_PASSED ≥ 369`, `TEST_FAILED == 0`.

## Команды воспроизведения

```bash
git status --short
git log -n 40 --oneline
python -m pytest workspace/skills/legal_summarizer/tests --no-header -q --tb=line
```