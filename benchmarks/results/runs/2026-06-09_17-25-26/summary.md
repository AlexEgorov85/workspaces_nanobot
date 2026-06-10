# Benchmark Report: medium
**Date:** 2026-06-09T17:29:45.293684

## Summary

| Metric | Value |
|--------|-------|
| Total Items | 7 |
| Passed | 5 / 7 |
| Pass Rate | 71.4% |
| Average Score | 87.7% |
| Total Score | 614.0% |
| Duration | 258.9s |

## By Difficulty

| Level | Items | Passed | Pass Rate | Avg Score |
|-------|-------|--------|-----------|-----------|
| simple | 7 | 5 | 71.4% | 87.7% |

## Results

| ID | Name | Difficulty | Score | Passed | Iterations | Duration |
|----|------|------------|-------|--------|------------|----------|
| medium-grep-files | Search Python files | medium | 99.3% | PASS | 3 | 6.8s |
| medium-file-analysis | Analyze Python imports | medium | 99.6% | PASS | 2 | 39.6s |
| medium-skill-discovery | List available skills | medium | 99.3% | PASS | 3 | 8.8s |
| medium-config-analysis | Config analysis | medium | 48.6% | FAIL | 5 | 11.6s |
| medium-project-architecture | Project architecture | medium | 68.6% | FAIL | 56 | 179.4s |
| medium-simple-script | Write and run a script | medium | 99.3% | PASS | 3 | 4.8s |
| medium-find-configs | Find configuration patterns | medium | 99.3% | PASS | 3 | 6.9s |

### medium-grep-files

- **Score:** 99.3% (EXCELLENT)
- **Passed:** True
- **Tools Used:** grep, grep
- **Iterations:** 3
- **Duration:** 6.8s
- **Checks:**
  - ✓ **tools**: All expected tools used: ['grep'] (score: 1.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 3 iterations within limit of 30 (score: 0.97)
  - ✓ **keywords_include**: No keyword requirements (score: 1.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)

### medium-file-analysis

- **Score:** 99.6% (EXCELLENT)
- **Passed:** True
- **Tools Used:** grep
- **Iterations:** 2
- **Duration:** 39.6s
- **Checks:**
  - ✓ **tools**: All expected tools used: ['grep'] (score: 1.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 2 iterations within limit of 30 (score: 0.98)
  - ✓ **keywords_include**: No keyword requirements (score: 1.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)

### medium-skill-discovery

- **Score:** 99.3% (EXCELLENT)
- **Passed:** True
- **Tools Used:** list_dir, read_file
- **Iterations:** 3
- **Duration:** 8.8s
- **Checks:**
  - ✓ **tools**: No tool expectations (score: 1.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 3 iterations within limit of 30 (score: 0.97)
  - ✓ **keywords_include**: All keywords found: ['data-analyzer', 'skill'] (score: 1.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)

### medium-config-analysis

- **Score:** 48.6% (FAIL)
- **Passed:** False
- **Tools Used:** find_files, find_files, find_files, find_files
- **Iterations:** 5
- **Duration:** 11.6s
- **Checks:**
  - ✗ **tools**: Missing tools: ['read_file'] (score: 0.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 5 iterations within limit of 30 (score: 0.93)
  - ✗ **keywords_include**: Missing keywords: ['model', 'provider', 'iteration'] (score: 0.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)

### medium-project-architecture

- **Score:** 68.6% (SATISFACTORY)
- **Passed:** False
- **Tools Used:** find_files, find_files, find_files, find_files, list_dir, list_dir, read_file, find_files, find_files, list_dir, read_file, read_file, read_file, read_file, find_files, find_files, find_files, list_dir, list_dir, list_dir, list_dir, read_file, read_file, read_file, read_file, list_dir, read_file, list_dir, read_file, read_file, find_files, list_dir, read_file, read_file, read_file, read_file, read_file, read_file, read_file, read_file, read_file, read_file, read_file, read_file, list_dir, read_file, read_file, read_file, read_file, read_file, read_file, find_files, , list_dir
- **Iterations:** 56
- **Duration:** 179.4s
- **Checks:**
  - ✓ **tools**: All expected tools used: ['read_file', 'find_files'] (score: 1.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✗ **iterations**: 56 iterations > 30 max (score: 0.54)
  - ✗ **keywords_include**: Missing keywords: ['agent', 'session'] (score: 0.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)

### medium-simple-script

- **Score:** 99.3% (EXCELLENT)
- **Passed:** True
- **Tools Used:** write_file, exec
- **Iterations:** 3
- **Duration:** 4.8s
- **Checks:**
  - ✓ **tools**: All expected tools used: ['exec'] (score: 1.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 3 iterations within limit of 30 (score: 0.97)
  - ✓ **keywords_include**: All keywords found: ['1', '2', '3', '10'] (score: 1.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)

### medium-find-configs

- **Score:** 99.3% (EXCELLENT)
- **Passed:** True
- **Tools Used:** find_files, find_files
- **Iterations:** 3
- **Duration:** 6.9s
- **Checks:**
  - ✓ **tools**: All expected tools used: ['find_files'] (score: 1.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 3 iterations within limit of 30 (score: 0.97)
  - ✓ **keywords_include**: All keywords found: ['.json'] (score: 1.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)
