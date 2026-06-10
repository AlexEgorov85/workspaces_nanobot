# Benchmark Report: hard
**Date:** 2026-06-10T08:17:16.647895

## Summary

| Metric | Value |
|--------|-------|
| Total Items | 4 |
| Passed | 0 / 4 |
| Pass Rate | 0.0% |
| Average Score | 64.7% |
| Total Score | 258.8% |
| Duration | 111.4s |

## By Difficulty

| Level | Items | Passed | Pass Rate | Avg Score |
|-------|-------|--------|-----------|-----------|
| simple | 4 | 0 | 0.0% | 64.7% |

## Results

| ID | Name | Difficulty | Score | Passed | Iterations | Duration |
|----|------|------------|-------|--------|------------|----------|
| hard-code-test-fix | Написать, протестировать и исп | hard | 45.0% | FAIL | 12 | 33.8s |
| hard-research-report | Исследование и отчёт | hard | 52.7% | FAIL | 9 | 27.3s |
| hard-data-pipeline | Анализ данных | hard | 79.3% | FAIL | 7 | 24.4s |
| hard-git-history | Анализ Git истории | hard | 81.8% | FAIL | 6 | 24.0s |

### hard-code-test-fix

- **Score:** 45.0% (FAIL)
- **Passed:** False
- **Tools Used:** edit_file, exec, read_file, write_file
- **Iterations:** 12
- **Duration:** 33.8s
- **Checks:**
  - ✗ **tools**: Missing tools: ['exec'] (score: 0.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 2 iterations within limit of 30 (score: 0.98)
  - ✓ **keywords_include**: No keyword requirements (score: 1.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)
  - ✗ **file_exists**: File not found: C:\Users\Алексей\.nanobot\workspace\data_store\cache\fibonacci.py (score: 0.00)
  - ✓ **tools**: All expected tools used: ['exec'] (score: 1.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 8 iterations within limit of 30 (score: 0.88)
  - ✗ **keywords_include**: Missing keywords: ['55'] (score: 0.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)
  - ✗ **file_exists**: File not found: C:\Users\Алексей\.nanobot\workspace\data_store\cache\fibonacci_output.txt (score: 0.00)
  - ✗ **tools**: Missing tools: ['exec'] (score: 0.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 2 iterations within limit of 30 (score: 0.98)
  - ✗ **keywords_include**: Missing keywords: ['недопустим'] (score: 0.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)
  - ✓ **llm_judge**: LLM judge not available, skipped (score: 0.50)
- **Steps:**
  - ✗ Step 1: score=0.59, weight=0.3, iterations=2
  - ✗ Step 2: score=0.63, weight=0.3, iterations=8
  - ✗ Step 3: score=0.50, weight=0.4, iterations=2

### hard-research-report

- **Score:** 52.7% (SATISFACTORY)
- **Passed:** False
- **Tools Used:** find_files, read_file, write_file
- **Iterations:** 9
- **Duration:** 27.3s
- **Checks:**
  - ✗ **tools**: Missing tools: ['exec'] (score: 0.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 5 iterations within limit of 30 (score: 0.93)
  - ✓ **keywords_include**: No keyword requirements (score: 1.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)
  - ✓ **tools**: All expected tools used: ['read_file'] (score: 1.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 2 iterations within limit of 30 (score: 0.98)
  - ✗ **keywords_include**: Missing keywords: ['assert'] (score: 0.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)
  - ✗ **tools**: Missing tools: ['exec'] (score: 0.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 2 iterations within limit of 30 (score: 0.98)
  - ✓ **keywords_include**: All keywords found: ['тест', 'файл'] (score: 1.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)
  - ✗ **file_exists**: File not found: C:\Users\Алексей\.nanobot\workspace\data_store\cache\test_summary.md (score: 0.00)
  - ✓ **llm_judge**: LLM judge not available, skipped (score: 0.50)
- **Steps:**
  - ✗ Step 1: score=0.70, weight=0.2, iterations=5
  - ✗ Step 2: score=0.78, weight=0.3, iterations=2
  - ✗ Step 3: score=0.57, weight=0.5, iterations=2

### hard-data-pipeline

- **Score:** 79.3% (GOOD)
- **Passed:** False
- **Tools Used:** exec, find_files, read_file, write_file
- **Iterations:** 7
- **Duration:** 24.4s
- **Checks:**
  - ✓ **tools**: All expected tools used: ['find_files'] (score: 1.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 2 iterations within limit of 30 (score: 0.98)
  - ✓ **keywords_include**: All keywords found: ['.csv'] (score: 1.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)
  - ✓ **tools**: All expected tools used: ['read_file', 'exec'] (score: 1.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 3 iterations within limit of 30 (score: 0.97)
  - ✓ **keywords_include**: All keywords found: ['выручк', 'средн', 'транзакц'] (score: 1.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)
  - ✗ **tools**: Missing tools: ['exec'] (score: 0.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 2 iterations within limit of 30 (score: 0.98)
  - ✓ **keywords_include**: All keywords found: ['выручк', 'продаж'] (score: 1.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)
  - ✗ **file_exists**: File not found: C:\Users\Алексей\.nanobot\workspace\data_store\cache\sales_report.md (score: 0.00)
  - ✓ **llm_judge**: LLM judge not available, skipped (score: 0.50)
- **Steps:**
  - ✓ Step 1: score=1.00, weight=0.2, iterations=2
  - ✓ Step 2: score=0.99, weight=0.4, iterations=3
  - ✗ Step 3: score=0.57, weight=0.4, iterations=2

### hard-git-history

- **Score:** 81.8% (GOOD)
- **Passed:** False
- **Tools Used:** exec
- **Iterations:** 6
- **Duration:** 24.0s
- **Checks:**
  - ✓ **tools**: All expected tools used: ['exec'] (score: 1.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 2 iterations within limit of 30 (score: 0.98)
  - ✓ **keywords_include**: All keywords found: ['коммит'] (score: 1.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)
  - ✓ **tools**: All expected tools used: ['exec'] (score: 1.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 2 iterations within limit of 30 (score: 0.98)
  - ✓ **keywords_include**: All keywords found: ['файл', 'изменен'] (score: 1.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)
  - ✓ **tools**: All expected tools used: ['exec'] (score: 1.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 2 iterations within limit of 30 (score: 0.98)
  - ✗ **keywords_include**: Missing keywords: ['коммит'] (score: 0.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)
  - ✗ **file_exists**: File not found: C:\Users\Алексей\.nanobot\workspace\data_store\cache\git_log.md (score: 0.00)
- **Steps:**
  - ✓ Step 1: score=1.00, weight=0.3, iterations=2
  - ✓ Step 2: score=1.00, weight=0.3, iterations=2
  - ✗ Step 3: score=0.64, weight=0.4, iterations=2
