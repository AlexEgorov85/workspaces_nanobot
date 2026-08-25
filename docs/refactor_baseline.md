# Refactor baseline

**Date:** 2026-08-24
**Branch:** `refactor/skills-tools-cleanup`
**HEAD:** `dd4ecdf` (wip: промежуточные изменения перед стартом рефакторинга)

## pytest (на момент фиксации baseline)

- collected: **1153** (на текущий момент — 1502 collected, см. CHANGELOG [Unreleased])
- skipped: **14** (на текущий момент — 22, добавлены архитектурные и contract-тесты)
- errors: **55** (все в `tests/test_postgres_channel.py` — требовали PostgreSQL; устранены автоuse-фикстурой `test_parallel_modes.py` + форс-реимпортом канала под фейковым `utils.db` в `tests/test_postgres_channel.py`)
- warnings: **1**

## Примечания

- baseline записан до старта изменений по плану `docs/skill-tool-architecture.md`.
- После каждого коммита ветки `refactor/skills-tools-cleanup` фиксировались
  промежуточные baseline в `docs/refactor_baseline_after.md` (на момент слияния
  в `master` итоговое состояние: **1480 passed, 22 skipped**).