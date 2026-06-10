# Benchmark Report: medium
**Date:** 2026-06-10T08:14:59.429294

## Summary

| Metric | Value |
|--------|-------|
| Total Items | 7 |
| Passed | 6 / 7 |
| Pass Rate | 85.7% |
| Average Score | 95.2% |
| Total Score | 666.4% |
| Duration | 82.9s |

## By Difficulty

| Level | Items | Passed | Pass Rate | Avg Score |
|-------|-------|--------|-----------|-----------|
| simple | 7 | 6 | 85.7% | 95.2% |

## Results

| ID | Name | Difficulty | Score | Passed | Iterations | Duration |
|----|------|------------|-------|--------|------------|----------|
| medium-grep-files | Поиск Python файлов | medium | 99.3% | PASS | 3 | 7.7s |
| medium-file-analysis | Анализ импортов | medium | 99.3% | PASS | 3 | 12.3s |
| medium-skill-discovery | Список навыков | medium | 100.0% | PASS | 1 | 8.3s |
| medium-config-analysis | Анализ конфига | medium | 99.6% | PASS | 2 | 5.6s |
| medium-project-architecture | Архитектура проекта | medium | 69.3% | FAIL | 7 | 31.2s |
| medium-simple-script | Скрипт на Python | medium | 99.3% | PASS | 3 | 12.3s |
| medium-find-configs | Поиск конфигов | medium | 99.6% | PASS | 2 | 4.6s |

### medium-grep-files

- **Score:** 99.3% (EXCELLENT)
- **Passed:** True
- **Tools Used:** find_files, grep
- **Iterations:** 3
- **Duration:** 7.7s
- **Checks:**
  - ✓ **tools**: All expected tools used: ['grep'] (score: 1.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 3 iterations within limit of 30 (score: 0.97)
  - ✓ **keywords_include**: No keyword requirements (score: 1.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)

### medium-file-analysis

- **Score:** 99.3% (EXCELLENT)
- **Passed:** True
- **Tools Used:** find_files, grep
- **Iterations:** 3
- **Duration:** 12.3s
- **Checks:**
  - ✓ **tools**: All expected tools used: ['grep'] (score: 1.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 3 iterations within limit of 30 (score: 0.97)
  - ✓ **keywords_include**: No keyword requirements (score: 1.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)

### medium-skill-discovery

- **Score:** 100.0% (EXCELLENT)
- **Passed:** True
- **Tools Used:** none
- **Iterations:** 1
- **Duration:** 8.3s
- **Checks:**
  - ✓ **tools**: No tool expectations (score: 1.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 1 iterations within limit of 30 (score: 1.00)
  - ✓ **keywords_include**: All keywords found: ['data-analyzer', 'навык'] (score: 1.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)

### medium-config-analysis

- **Score:** 99.6% (EXCELLENT)
- **Passed:** True
- **Tools Used:** read_file
- **Iterations:** 2
- **Duration:** 5.6s
- **Checks:**
  - ✓ **tools**: All expected tools used: ['read_file'] (score: 1.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 2 iterations within limit of 30 (score: 0.98)
  - ✓ **keywords_include**: All keywords found: ['модел', 'провайдер'] (score: 1.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)

### medium-project-architecture

- **Score:** 69.3% (SATISFACTORY)
- **Passed:** False
- **Tools Used:** read_file, read_file, read_file, read_file, read_file, read_file
- **Iterations:** 7
- **Duration:** 31.2s
- **Checks:**
  - ✗ **tools**: Missing tools: ['find_files'] (score: 0.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 7 iterations within limit of 30 (score: 0.90)
  - ✓ **keywords_include**: All keywords found: ['агент', 'сесси', 'gateway'] (score: 1.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)

### medium-simple-script

- **Score:** 99.3% (EXCELLENT)
- **Passed:** True
- **Tools Used:** write_file, exec
- **Iterations:** 3
- **Duration:** 12.3s
- **Checks:**
  - ✓ **tools**: All expected tools used: ['exec'] (score: 1.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 3 iterations within limit of 30 (score: 0.97)
  - ✓ **keywords_include**: All keywords found: ['1', '2', '3', '10'] (score: 1.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)

### medium-find-configs

- **Score:** 99.6% (EXCELLENT)
- **Passed:** True
- **Tools Used:** find_files
- **Iterations:** 2
- **Duration:** 4.6s
- **Checks:**
  - ✓ **tools**: All expected tools used: ['find_files'] (score: 1.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 2 iterations within limit of 30 (score: 0.98)
  - ✓ **keywords_include**: All keywords found: ['.json'] (score: 1.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)
