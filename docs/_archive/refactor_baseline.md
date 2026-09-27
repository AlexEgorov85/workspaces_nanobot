# Refactor baseline

**Date:** 2026-08-24
**Branch:** `refactor/skills-tools-cleanup`
**HEAD:** `dd4ecdf` (wip: промежуточные изменения перед стартом рефакторинга)

## pytest (baseline ветки `refactor/skills-tools-cleanup`)

> Это baseline конкретной ветки рефакторинга, **не** канонический таргет mainline.
> Ориентир для `master` — «1480 passed, 22 skipped» (см. `AGENTS.md` → «Release Process»).
> Числа здесь отражают состояние ветки на момент прогона и могут отличаться от mainline.

- collected: **1153** (на текущий момент — **1816 collected / 1802 passed, 9 skipped**, прогон 2026-08-26)
- skipped: **14** (на текущий момент — **9**, добавлены архитектурные и contract-тесты)
- errors: **55** (на текущий момент — **0**; устранены автоuse-фикстурой `test_parallel_modes.py` + форс-реимпортом канала под фейковым `utils.db` в `tests/test_postgres_channel.py`)
- warnings: **1** (без изменений)

## Примечания

- baseline записан до старта изменений по плану `docs/skill-tool-architecture.md`.
- После каждого коммита ветки `refactor/skills-tools-cleanup` фиксировались
  промежуточные baseline в `docs/refactor_baseline_after.md` (на момент слияния
  в `master` (HEAD `bb844cf`) итоговое состояние: **1802 passed, 9 skipped**,
  прогон 2026-08-26).