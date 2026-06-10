# Benchmark Report: simple
**Date:** 2026-06-10T08:13:25.538887

## Summary

| Metric | Value |
|--------|-------|
| Total Items | 7 |
| Passed | 7 / 7 |
| Pass Rate | 100.0% |
| Average Score | 99.8% |
| Total Score | 698.6% |
| Duration | 34.5s |

## By Difficulty

| Level | Items | Passed | Pass Rate | Avg Score |
|-------|-------|--------|-----------|-----------|
| simple | 7 | 7 | 100.0% | 99.8% |

## Results

| ID | Name | Difficulty | Score | Passed | Iterations | Duration |
|----|------|------------|-------|--------|------------|----------|
| simple-greeting | Приветствие | simple | 100.0% | PASS | 1 | 3.0s |
| simple-math | Простая математика | simple | 100.0% | PASS | 1 | 2.0s |
| simple-list-files | Список файлов | simple | 99.6% | PASS | 2 | 14.2s |
| simple-create-file | Создать файл | simple | 99.7% | PASS | 2 | 4.4s |
| simple-date | Текущая дата | simple | 100.0% | PASS | 1 | 1.7s |
| simple-read-file | Чтение файла | simple | 99.6% | PASS | 2 | 4.3s |
| simple-git-status | Git статус | simple | 99.6% | PASS | 2 | 4.0s |

### simple-greeting

- **Score:** 100.0% (EXCELLENT)
- **Passed:** True
- **Tools Used:** none
- **Iterations:** 1
- **Duration:** 3.0s
- **Checks:**
  - ✓ **tools**: No tool expectations (score: 1.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 1 iterations within limit of 30 (score: 1.00)
  - ✓ **keywords_include**: All keywords found: ['привет'] (score: 1.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)

### simple-math

- **Score:** 100.0% (EXCELLENT)
- **Passed:** True
- **Tools Used:** none
- **Iterations:** 1
- **Duration:** 2.0s
- **Checks:**
  - ✓ **tools**: No tool expectations (score: 1.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 1 iterations within limit of 30 (score: 1.00)
  - ✓ **keywords_include**: All keywords found: ['4'] (score: 1.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)

### simple-list-files

- **Score:** 99.6% (EXCELLENT)
- **Passed:** True
- **Tools Used:** list_dir
- **Iterations:** 2
- **Duration:** 14.2s
- **Checks:**
  - ✓ **tools**: All expected tools used: ['list_dir'] (score: 1.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 2 iterations within limit of 30 (score: 0.98)
  - ✓ **keywords_include**: No keyword requirements (score: 1.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)

### simple-create-file

- **Score:** 99.7% (EXCELLENT)
- **Passed:** True
- **Tools Used:** write_file
- **Iterations:** 2
- **Duration:** 4.4s
- **Checks:**
  - ✓ **tools**: All expected tools used: ['write_file'] (score: 1.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 2 iterations within limit of 30 (score: 0.98)
  - ✓ **keywords_include**: No keyword requirements (score: 1.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)
  - ✓ **file_exists**: File exists: C:\Users\Алексей\.nanobot\workspace\data_store\cache\hello_benchmark.txt (score: 1.00)

### simple-date

- **Score:** 100.0% (EXCELLENT)
- **Passed:** True
- **Tools Used:** none
- **Iterations:** 1
- **Duration:** 1.7s
- **Checks:**
  - ✓ **tools**: No tool expectations (score: 1.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 1 iterations within limit of 30 (score: 1.00)
  - ✓ **keywords_include**: All keywords found: ['2026'] (score: 1.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)

### simple-read-file

- **Score:** 99.6% (EXCELLENT)
- **Passed:** True
- **Tools Used:** read_file
- **Iterations:** 2
- **Duration:** 4.3s
- **Checks:**
  - ✓ **tools**: All expected tools used: ['read_file'] (score: 1.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 2 iterations within limit of 30 (score: 0.98)
  - ✓ **keywords_include**: All keywords found: ['агент'] (score: 1.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)

### simple-git-status

- **Score:** 99.6% (EXCELLENT)
- **Passed:** True
- **Tools Used:** exec
- **Iterations:** 2
- **Duration:** 4.0s
- **Checks:**
  - ✓ **tools**: All expected tools used: ['exec'] (score: 1.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 2 iterations within limit of 30 (score: 0.98)
  - ✓ **keywords_include**: All keywords found: ['ветк'] (score: 1.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)
