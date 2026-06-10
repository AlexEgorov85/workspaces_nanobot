# Benchmark Report: simple
**Date:** 2026-06-10T08:22:26.228686

## Summary

| Metric | Value |
|--------|-------|
| Total Items | 7 |
| Passed | 0 / 7 |
| Pass Rate | 0.0% |
| Average Score | 67.0% |
| Total Score | 469.3% |
| Duration | 4.8s |

## By Difficulty

| Level | Items | Passed | Pass Rate | Avg Score |
|-------|-------|--------|-----------|-----------|
| simple | 7 | 0 | 0.0% | 67.0% |

## Results

| ID | Name | Difficulty | Score | Passed | Iterations | Duration |
|----|------|------------|-------|--------|------------|----------|
| simple-greeting | Приветствие | simple | 64.3% | FAIL | 1 | 2.1s |
| simple-math | Простая математика | simple | 78.6% | FAIL | 1 | 0.2s |
| simple-list-files | Список файлов | simple | 71.4% | FAIL | 1 | 0.2s |
| simple-create-file | Создать файл | simple | 76.5% | FAIL | 1 | 0.2s |
| simple-date | Текущая дата | simple | 78.6% | FAIL | 1 | 0.2s |
| simple-read-file | Чтение файла | simple | 50.0% | FAIL | 1 | 0.2s |
| simple-git-status | Git статус | simple | 50.0% | FAIL | 1 | 0.2s |

### simple-greeting

- **Score:** 64.3% (SATISFACTORY)
- **Passed:** False
- **Tools Used:** none
- **Iterations:** 1
- **Duration:** 2.1s
- **Checks:**
  - ✓ **tools**: No tool expectations (score: 1.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 1 iterations within limit of 30 (score: 1.00)
  - ✗ **keywords_include**: Missing keywords: ['привет'] (score: 0.00)
  - ✗ **keywords_exclude**: Forbidden keywords found: ['error'] (score: 0.00)

### simple-math

- **Score:** 78.6% (GOOD)
- **Passed:** False
- **Tools Used:** none
- **Iterations:** 1
- **Duration:** 0.2s
- **Checks:**
  - ✓ **tools**: No tool expectations (score: 1.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 1 iterations within limit of 30 (score: 1.00)
  - ✗ **keywords_include**: Missing keywords: ['4'] (score: 0.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)

### simple-list-files

- **Score:** 71.4% (GOOD)
- **Passed:** False
- **Tools Used:** none
- **Iterations:** 1
- **Duration:** 0.2s
- **Checks:**
  - ✗ **tools**: Missing tools: ['list_dir'] (score: 0.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 1 iterations within limit of 30 (score: 1.00)
  - ✓ **keywords_include**: No keyword requirements (score: 1.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)

### simple-create-file

- **Score:** 76.5% (GOOD)
- **Passed:** False
- **Tools Used:** none
- **Iterations:** 1
- **Duration:** 0.2s
- **Checks:**
  - ✗ **tools**: Missing tools: ['write_file'] (score: 0.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 1 iterations within limit of 30 (score: 1.00)
  - ✓ **keywords_include**: No keyword requirements (score: 1.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)
  - ✓ **file_exists**: File exists: C:\Users\Алексей\.nanobot\workspace\data_store\cache\hello_benchmark.txt (score: 1.00)

### simple-date

- **Score:** 78.6% (GOOD)
- **Passed:** False
- **Tools Used:** none
- **Iterations:** 1
- **Duration:** 0.2s
- **Checks:**
  - ✓ **tools**: No tool expectations (score: 1.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 1 iterations within limit of 30 (score: 1.00)
  - ✗ **keywords_include**: Missing keywords: ['2026'] (score: 0.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)

### simple-read-file

- **Score:** 50.0% (SATISFACTORY)
- **Passed:** False
- **Tools Used:** none
- **Iterations:** 1
- **Duration:** 0.2s
- **Checks:**
  - ✗ **tools**: Missing tools: ['read_file'] (score: 0.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 1 iterations within limit of 30 (score: 1.00)
  - ✗ **keywords_include**: Missing keywords: ['агент'] (score: 0.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)

### simple-git-status

- **Score:** 50.0% (SATISFACTORY)
- **Passed:** False
- **Tools Used:** none
- **Iterations:** 1
- **Duration:** 0.2s
- **Checks:**
  - ✗ **tools**: Missing tools: ['exec'] (score: 0.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 1 iterations within limit of 30 (score: 1.00)
  - ✗ **keywords_include**: Missing keywords: ['ветк'] (score: 0.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)
