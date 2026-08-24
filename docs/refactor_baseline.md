# Refactor baseline

**Date:** 2026-08-24
**Branch:** `refactor/skills-tools-cleanup`
**HEAD:** `dd4ecdf` (wip: промежуточные изменения перед стартом рефакторинга)

## pytest

- passed: **1153**
- skipped: **14**
- errors: **55** (все в `tests/test_postgres_channel.py` — требуют PostgreSQL, отсутствующий в окружении; не связано с планом)
- warnings: **1**

## Примечания

- baseline записан до старта изменений по плану `docs/skill-tool-architecture.md`.
- Ошибки `test_postgres_channel.py` (55 шт.) существовали до рефакторинга и не исправляются в этой ветке.
- После каждого коммита ветки `refactor/skills-tools-cleanup` будет фиксироваться новая baseline-секция в `docs/refactor_baseline_after.md`.