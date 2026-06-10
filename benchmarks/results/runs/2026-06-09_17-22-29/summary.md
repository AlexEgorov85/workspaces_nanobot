# Benchmark Report: medium
**Date:** 2026-06-09T17:24:51.184694

## Summary

| Metric | Value |
|--------|-------|
| Total Items | 7 |
| Passed | 2 / 7 |
| Pass Rate | 28.6% |
| Average Score | 75.2% |
| Total Score | 526.4% |
| Duration | 141.2s |

## By Difficulty

| Level | Items | Passed | Pass Rate | Avg Score |
|-------|-------|--------|-----------|-----------|
| simple | 7 | 2 | 28.6% | 75.2% |

## Results

| ID | Name | Difficulty | Score | Passed | Iterations | Duration |
|----|------|------------|-------|--------|------------|----------|
| medium-grep-files | Search Python files | medium | 67.5% | FAIL | 12 | 30.1s |
| medium-file-analysis | Analyze Python imports | medium | 69.6% | FAIL | 6 | 33.2s |
| medium-skill-discovery | List available skills | medium | 99.6% | PASS | 2 | 7.9s |
| medium-config-analysis | Config analysis | medium | 71.8% | FAIL | 20 | 34.0s |
| medium-project-architecture | Project architecture | medium | 48.9% | FAIL | 4 | 15.1s |
| medium-simple-script | Write and run a script | medium | 99.3% | PASS | 3 | 7.9s |
| medium-find-configs | Find configuration patterns | medium | 69.6% | FAIL | 6 | 12.1s |

### medium-grep-files

- **Score:** 67.5% (SATISFACTORY)
- **Passed:** False
- **Tools Used:** grep, read_file, read_file, read_file, read_file, read_file, read_file, read_file, grep, grep, grep
- **Iterations:** 12
- **Duration:** 30.1s
- **Checks:**
  - ✗ **tools**: Missing tools: ['exec'] (score: 0.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 12 iterations within limit of 30 (score: 0.82)
  - ✓ **keywords_include**: No keyword requirements (score: 1.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)

### medium-file-analysis

- **Score:** 69.6% (SATISFACTORY)
- **Passed:** False
- **Tools Used:** grep, grep, grep, grep, grep
- **Iterations:** 6
- **Duration:** 33.2s
- **Checks:**
  - ✗ **tools**: Missing tools: ['exec'] (score: 0.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 6 iterations within limit of 30 (score: 0.92)
  - ✓ **keywords_include**: No keyword requirements (score: 1.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)

### medium-skill-discovery

- **Score:** 99.6% (EXCELLENT)
- **Passed:** True
- **Tools Used:** read_file
- **Iterations:** 2
- **Duration:** 7.9s
- **Checks:**
  - ✓ **tools**: No tool expectations (score: 1.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 2 iterations within limit of 30 (score: 0.98)
  - ✓ **keywords_include**: All keywords found: ['data-analyzer', 'skill'] (score: 1.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)

### medium-config-analysis

- **Score:** 71.8% (GOOD)
- **Passed:** False
- **Tools Used:** find_files, find_files, read_file, find_files, read_file, find_files, read_file, list_dir, find_files, read_file, read_file, grep, find_files, read_file, grep, grep, grep, read_file, read_file
- **Iterations:** 20
- **Duration:** 34.0s
- **Checks:**
  - ✓ **tools**: All expected tools used: ['read_file'] (score: 1.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 20 iterations within limit of 30 (score: 0.68)
  - ✗ **keywords_include**: Missing keywords: ['model', 'provider', 'iteration'] (score: 0.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)

### medium-project-architecture

- **Score:** 48.9% (FAIL)
- **Passed:** False
- **Tools Used:** list_dir, find_files, list_dir
- **Iterations:** 4
- **Duration:** 15.1s
- **Checks:**
  - ✗ **tools**: Missing tools: ['read_file'] (score: 0.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 4 iterations within limit of 30 (score: 0.95)
  - ✗ **keywords_include**: Missing keywords: ['gateway'] (score: 0.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)

### medium-simple-script

- **Score:** 99.3% (EXCELLENT)
- **Passed:** True
- **Tools Used:** write_file, exec
- **Iterations:** 3
- **Duration:** 7.9s
- **Checks:**
  - ✓ **tools**: All expected tools used: ['exec'] (score: 1.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 3 iterations within limit of 30 (score: 0.97)
  - ✓ **keywords_include**: All keywords found: ['1', '2', '3', '10'] (score: 1.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)

### medium-find-configs

- **Score:** 69.6% (SATISFACTORY)
- **Passed:** False
- **Tools Used:** find_files, read_file, read_file, grep, grep
- **Iterations:** 6
- **Duration:** 12.1s
- **Checks:**
  - ✗ **tools**: Missing tools: ['exec'] (score: 0.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 6 iterations within limit of 30 (score: 0.92)
  - ✓ **keywords_include**: All keywords found: ['.json'] (score: 1.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)
