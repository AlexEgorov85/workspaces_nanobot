# Benchmark Report: items
**Date:** 2026-06-09T17:19:23.061830

## Summary

| Metric | Value |
|--------|-------|
| Total Items | 7 |
| Passed | 2 / 7 |
| Pass Rate | 28.6% |
| Average Score | 76.5% |
| Total Score | 535.7% |
| Duration | 22.7s |

## By Difficulty

| Level | Items | Passed | Pass Rate | Avg Score |
|-------|-------|--------|-----------|-----------|
| simple | 7 | 2 | 28.6% | 76.5% |

## Results

| ID | Name | Difficulty | Score | Passed | Iterations | Duration |
|----|------|------------|-------|--------|------------|----------|
| simple-greeting | Greeting | simple | 78.6% | FAIL | 1 | 2.5s |
| simple-math | Simple math | simple | 100.0% | PASS | 1 | 1.5s |
| simple-list-files | List files | simple | 71.1% | FAIL | 2 | 4.2s |
| simple-create-file | Create empty file | simple | 58.5% | FAIL | 2 | 3.1s |
| simple-date | Current date | simple | 100.0% | PASS | 1 | 1.3s |
| simple-read-file | Read a file | simple | 49.3% | FAIL | 3 | 6.0s |
| simple-git-status | Git status | simple | 78.2% | FAIL | 2 | 3.1s |

### simple-greeting

- **Score:** 78.6% (GOOD)
- **Passed:** False
- **Tools Used:** none
- **Iterations:** 1
- **Duration:** 2.5s
- **Checks:**
  - ✓ **tools**: No tool expectations (score: 1.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 1 iterations within limit of 30 (score: 1.00)
  - ✗ **keywords_include**: Missing keywords: ['hi'] (score: 0.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)

### simple-math

- **Score:** 100.0% (EXCELLENT)
- **Passed:** True
- **Tools Used:** none
- **Iterations:** 1
- **Duration:** 1.5s
- **Checks:**
  - ✓ **tools**: No tool expectations (score: 1.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 1 iterations within limit of 30 (score: 1.00)
  - ✓ **keywords_include**: All keywords found: ['4'] (score: 1.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)

### simple-list-files

- **Score:** 71.1% (GOOD)
- **Passed:** False
- **Tools Used:** list_dir
- **Iterations:** 2
- **Duration:** 4.2s
- **Checks:**
  - ✗ **tools**: Missing tools: ['exec', 'glob'] (score: 0.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 2 iterations within limit of 30 (score: 0.98)
  - ✓ **keywords_include**: No keyword requirements (score: 1.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)

### simple-create-file

- **Score:** 58.5% (SATISFACTORY)
- **Passed:** False
- **Tools Used:** write_file
- **Iterations:** 2
- **Duration:** 3.1s
- **Checks:**
  - ✗ **tools**: Missing tools: ['exec'] (score: 0.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 2 iterations within limit of 30 (score: 0.98)
  - ✓ **keywords_include**: No keyword requirements (score: 1.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)
  - ✗ **file_exists**: File not found: C:\Users\Алексей\.nanobot\workspace\hello_benchmark.txt (score: 0.00)

### simple-date

- **Score:** 100.0% (EXCELLENT)
- **Passed:** True
- **Tools Used:** none
- **Iterations:** 1
- **Duration:** 1.3s
- **Checks:**
  - ✓ **tools**: No tool expectations (score: 1.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 1 iterations within limit of 30 (score: 1.00)
  - ✓ **keywords_include**: All keywords found: ['2026'] (score: 1.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)

### simple-read-file

- **Score:** 49.3% (FAIL)
- **Passed:** False
- **Tools Used:** find_files, read_file
- **Iterations:** 3
- **Duration:** 6.0s
- **Checks:**
  - ✗ **tools**: Missing tools: ['read', 'exec'] (score: 0.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 3 iterations within limit of 30 (score: 0.97)
  - ✗ **keywords_include**: Missing keywords: ['instruction'] (score: 0.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)

### simple-git-status

- **Score:** 78.2% (GOOD)
- **Passed:** False
- **Tools Used:** exec
- **Iterations:** 2
- **Duration:** 3.1s
- **Checks:**
  - ✓ **tools**: All expected tools used: ['exec'] (score: 1.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 2 iterations within limit of 30 (score: 0.98)
  - ✗ **keywords_include**: Missing keywords: ['benchmark'] (score: 0.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)
