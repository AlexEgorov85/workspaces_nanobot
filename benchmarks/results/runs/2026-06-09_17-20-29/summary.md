# Benchmark Report: items
**Date:** 2026-06-09T17:20:50.406457

## Summary

| Metric | Value |
|--------|-------|
| Total Items | 7 |
| Passed | 7 / 7 |
| Pass Rate | 100.0% |
| Average Score | 99.8% |
| Total Score | 698.3% |
| Duration | 21.2s |

## By Difficulty

| Level | Items | Passed | Pass Rate | Avg Score |
|-------|-------|--------|-----------|-----------|
| simple | 7 | 7 | 100.0% | 99.8% |

## Results

| ID | Name | Difficulty | Score | Passed | Iterations | Duration |
|----|------|------------|-------|--------|------------|----------|
| simple-greeting | Greeting | simple | 100.0% | PASS | 1 | 2.2s |
| simple-math | Simple math | simple | 100.0% | PASS | 1 | 1.4s |
| simple-list-files | List files | simple | 99.6% | PASS | 2 | 3.3s |
| simple-create-file | Create empty file | simple | 99.7% | PASS | 2 | 3.2s |
| simple-date | Current date | simple | 100.0% | PASS | 1 | 1.6s |
| simple-read-file | Read a file | simple | 99.3% | PASS | 3 | 5.3s |
| simple-git-status | Git status | simple | 99.6% | PASS | 2 | 3.3s |

### simple-greeting

- **Score:** 100.0% (EXCELLENT)
- **Passed:** True
- **Tools Used:** none
- **Iterations:** 1
- **Duration:** 2.2s
- **Checks:**
  - ✓ **tools**: No tool expectations (score: 1.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 1 iterations within limit of 30 (score: 1.00)
  - ✓ **keywords_include**: All keywords found: ['hello'] (score: 1.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)

### simple-math

- **Score:** 100.0% (EXCELLENT)
- **Passed:** True
- **Tools Used:** none
- **Iterations:** 1
- **Duration:** 1.4s
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
- **Duration:** 3.3s
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
- **Duration:** 3.2s
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
- **Duration:** 1.6s
- **Checks:**
  - ✓ **tools**: No tool expectations (score: 1.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 1 iterations within limit of 30 (score: 1.00)
  - ✓ **keywords_include**: All keywords found: ['2026'] (score: 1.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)

### simple-read-file

- **Score:** 99.3% (EXCELLENT)
- **Passed:** True
- **Tools Used:** find_files, read_file
- **Iterations:** 3
- **Duration:** 5.3s
- **Checks:**
  - ✓ **tools**: All expected tools used: ['read_file'] (score: 1.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 3 iterations within limit of 30 (score: 0.97)
  - ✓ **keywords_include**: All keywords found: ['agent'] (score: 1.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)

### simple-git-status

- **Score:** 99.6% (EXCELLENT)
- **Passed:** True
- **Tools Used:** exec
- **Iterations:** 2
- **Duration:** 3.3s
- **Checks:**
  - ✓ **tools**: All expected tools used: ['exec'] (score: 1.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 2 iterations within limit of 30 (score: 0.98)
  - ✓ **keywords_include**: All keywords found: ['branch'] (score: 1.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)
