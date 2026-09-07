# `legal_summarizer` — юридический Skill для агента

> Юридический анализ PDF / DOCX / TXT — структурно-осведомлённый
> map-reduce по документу с idempotency, resume и cache.

## Что делает Skill

Берёт `.pdf` / `.docx` / `.txt`, делает саммари в одном из режимов:

| Режим | Описание |
| --- | --- |
| `--length brief` | 150–250 слов (первые 8 chunks) |
| `--length detailed` | 800–1200 слов (весь документ) |
| `--question "..."` | Краткий ответ (≤200 слов) на конкретный вопрос |

Для длинных документов возвращает `confirmation_required` с меню из
вариантов и **ждёт** явного `--confirm` от агента.

## Как запускать

```bash
python workspace/skills/legal_summarizer/scripts/cli.py --file <path> [--flags...]
```

Полный список флагов — `cli.py --help`. Подробности контракта (JSON
payloads, operation_id semantics, resume) — в
[`references/contracts.md`](references/contracts.md).

## Где что лежит

```text
legal_summarizer/
├── SKILL.md                    # инструкция агенту
├── README.md                   # этот файл
│
├── legal_summarizer/           # runtime Python-пакет
│   ├── application/            # orchestration, idempotency, manifest
│   ├── cache/                  # disk-манифест (chunks, result, sections)
│   ├── chunking/               # DocumentStructure → Chunk[]
│   ├── document/               # PhysicalDocument → DocumentStructure
│   ├── execution/              # direct / map-reduce / hierarchical
│   ├── llm/                    # chat wrapper, prompts, single-flight
│   ├── output/                 # JSON-форматирование
│   ├── planning/               # strategy selection
│   └── retrieval/              # query normalization, lexical, fallback
│
├── scripts/                    # entry-points
│   ├── cli.py                  # главная CLI
│   └── cli_query.py            # follow-up по operation_id
│
├── prompts/                    # LLM-инструкции (отдельно от кода)
├── references/                 # подробные документы
│   ├── architecture.md
│   ├── contracts.md
│   └── testing.md
│
└── tests/                      # developer-only
    ├── unit/
    ├── integration/
    └── architecture/           # boundary + skill_layout guards
```

## Архитектура (краткая)

```text
Agent
  → SKILL.md
    → scripts/cli.py
      → application/service.run()
        → document → retrieval → planning → execution → llm → single_flight → LLM API
        ↑                                                                            │
        └──── cache (application-level persistence) ←──────────────────────────────────┘
```

Полная архитектура с dependency direction и invariants —
[`references/architecture.md`](references/architecture.md).

## Где SKILL.md

[`SKILL.md`](SKILL.md) — главная инструкция для **агента**. Содержит
когда вызывать / не вызывать, протокол CLI, confirmation flow.

## Где промпты

[`prompts/`](prompts/) — markdown-файлы, загружаются через
`legal_summarizer.llm.prompts_runtime.load_prompt`:

* `summarize_system.md` — system prompt для map-phase.
* `section_reduce_system.md` — system prompt для section reduce.
* `reduce_system.md` — system prompt для document reduce.

## Как запускать тесты

```bash
cd workspace/skills/legal_summarizer
python -m pytest -q                       # все тесты
python -m pytest tests/architecture       # boundary guards
python -m pytest -k single_flight         # single-flight subset
```

Подробности — [`references/testing.md`](references/testing.md).

## Известные ограничения

* Не подходит для не-юридических документов (см. SKILL.md).
* Документы без текстового слоя (сканы без OCR) вернут ошибку.
* `application.execution_orchestration.run_map_reduce` — большой
  (~300 строк), внутри одной ответственности (map → reduce
  orchestration).

## Контракт стабилен

Skill не меняет:

* JSON-формат `completed` / `confirmation_required` / `requires_continuation` / `failed`.
* Семантику `operation_id` (SHA-256 от text+length+path+question).
* Поведение `--confirm` (без флага для длинных документов → confirmation).
* Cache semantics (idempotency + resume).