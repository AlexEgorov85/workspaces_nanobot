# Benchmark Report: medium
**Date:** 2026-06-10T08:19:30.937749

## Summary

| Metric | Value |
|--------|-------|
| Total Items | 7 |
| Passed | 0 / 7 |
| Pass Rate | 0.0% |
| Average Score | 60.2% |
| Total Score | 421.4% |
| Duration | 63.2s |

## By Difficulty

| Level | Items | Passed | Pass Rate | Avg Score |
|-------|-------|--------|-----------|-----------|
| simple | 7 | 0 | 0.0% | 60.2% |

## Results

| ID | Name | Difficulty | Score | Passed | Iterations | Duration |
|----|------|------------|-------|--------|------------|----------|
| medium-grep-files | Поиск Python файлов | medium | 71.4% | FAIL | 1 | 10.1s |
| medium-file-analysis | Анализ импортов | medium | 71.4% | FAIL | 1 | 8.7s |
| medium-skill-discovery | Список навыков | medium | 78.6% | FAIL | 1 | 8.7s |
| medium-config-analysis | Анализ конфига | medium | 50.0% | FAIL | 1 | 8.7s |
| medium-project-architecture | Архитектура проекта | medium | 50.0% | FAIL | 1 | 8.7s |
| medium-simple-script | Скрипт на Python | medium | 50.0% | FAIL | 1 | 8.7s |
| medium-find-configs | Поиск конфигов | medium | 50.0% | FAIL | 1 | 8.7s |

### medium-grep-files

- **Score:** 71.4% (GOOD)
- **Passed:** False
- **Tools Used:** none
- **Iterations:** 1
- **Duration:** 10.1s
- **Checks:**
  - ✗ **tools**: Missing tools: ['grep'] (score: 0.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 1 iterations within limit of 30 (score: 1.00)
  - ✓ **keywords_include**: No keyword requirements (score: 1.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)

### medium-file-analysis

- **Score:** 71.4% (GOOD)
- **Passed:** False
- **Tools Used:** none
- **Iterations:** 1
- **Duration:** 8.7s
- **Checks:**
  - ✗ **tools**: Missing tools: ['grep'] (score: 0.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 1 iterations within limit of 30 (score: 1.00)
  - ✓ **keywords_include**: No keyword requirements (score: 1.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)

### medium-skill-discovery

- **Score:** 78.6% (GOOD)
- **Passed:** False
- **Tools Used:** none
- **Iterations:** 1
- **Duration:** 8.7s
- **Checks:**
  - ✓ **tools**: No tool expectations (score: 1.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 1 iterations within limit of 30 (score: 1.00)
  - ✗ **keywords_include**: Missing keywords: ['data-analyzer', 'навык'] (score: 0.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)

### medium-config-analysis

- **Score:** 50.0% (SATISFACTORY)
- **Passed:** False
- **Tools Used:** none
- **Iterations:** 1
- **Duration:** 8.7s
- **Checks:**
  - ✗ **tools**: Missing tools: ['read_file'] (score: 0.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 1 iterations within limit of 30 (score: 1.00)
  - ✗ **keywords_include**: Missing keywords: ['модел', 'провайдер'] (score: 0.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)

### medium-project-architecture

- **Score:** 50.0% (SATISFACTORY)
- **Passed:** False
- **Tools Used:** none
- **Iterations:** 1
- **Duration:** 8.7s
- **Checks:**
  - ✗ **tools**: Missing tools: ['read_file'] (score: 0.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 1 iterations within limit of 30 (score: 1.00)
  - ✗ **keywords_include**: Missing keywords: ['агент', 'сесси', 'gateway'] (score: 0.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)

### medium-simple-script

- **Score:** 50.0% (SATISFACTORY)
- **Passed:** False
- **Tools Used:** none
- **Iterations:** 1
- **Duration:** 8.7s
- **Checks:**
  - ✗ **tools**: Missing tools: ['exec'] (score: 0.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 1 iterations within limit of 30 (score: 1.00)
  - ✗ **keywords_include**: Missing keywords: ['10'] (score: 0.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)

### medium-find-configs

- **Score:** 50.0% (SATISFACTORY)
- **Passed:** False
- **Tools Used:** none
- **Iterations:** 1
- **Duration:** 8.7s
- **Checks:**
  - ✗ **tools**: Missing tools: ['find_files'] (score: 0.00)
  - ✓ **skills**: No skill expectations (score: 1.00)
  - ✓ **iterations**: 1 iterations within limit of 30 (score: 1.00)
  - ✗ **keywords_include**: Missing keywords: ['.json'] (score: 0.00)
  - ✓ **keywords_exclude**: No forbidden keywords (score: 1.00)
