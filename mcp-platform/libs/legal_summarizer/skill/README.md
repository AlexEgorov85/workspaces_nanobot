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

Команда запускается **из корня репозитория** (агента):

```bash
python mcp-platform/libs/legal_summarizer/cli.py --file <path> [--flags...]
```

Полный список флагов — `cli.py --help`. Подробности контракта (JSON
payloads, operation_id semantics, resume) — в
[`references/contracts.md`](references/contracts.md).

## Где что лежит

```text
mcp-platform/libs/legal_summarizer/        # домен + payload Skill
├── cli.py                       # главная CLI
├── cli_query.py                 # follow-up по operation_id
├── __init__.py
│
├── application/                 # orchestration, idempotency, manifest
├── cache/                       # manifest, DocumentCache, session_key
├── chunking/                    # DocumentStructure → Chunk[]
├── document/                    # PhysicalDocument → DocumentStructure
│   └── extractors/              # pdf / docx / txt
├── execution/                   # direct / map-reduce / hierarchical
├── llm/                         # chat wrapper, prompts, single-flight
├── output/                      # JSON-форматирование
├── planning/                    # strategy selection
├── retrieval/                   # query normalization, lexical, fallback
│
└── skill/                       # payload Skill (этот README лежит здесь)
    ├── SKILL.md                 # инструкция агенту
    ├── README.md                # этот файл
    ├── prompts/                 # LLM-инструкции (отдельно от кода)
    │   ├── reduce_system.md
    │   ├── section_reduce_system.md
    │   └── summarize_system.md
    └── references/              # подробные документы
        ├── architecture.md
        ├── contracts.md
        └── testing.md

mcp-platform/tests/legal_summarizer/       # developer-only, отдельно от домена
├── unit/
├── integration/
└── architecture/              # boundary + skill_layout guards
```

Входы домена — `cli.py` и `cli_query.py` в корне
`libs/legal_summarizer/`; отдельного каталога для entry-points нет.

## Архитектура (краткая)

```text
Agent
  → SKILL.md
    → mcp-platform/libs/legal_summarizer/cli.py
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

* `prompts/summarize_system.md` — system prompt для map-phase.
* `prompts/section_reduce_system.md` — system prompt для section reduce.
* `prompts/reduce_system.md` — system prompt для document reduce.

## Как запускать тесты

```bash
cd mcp-platform
python -m pytest -q tests/legal_summarizer                  # все тесты
python -m pytest -q tests/legal_summarizer/architecture      # boundary guards
python -m pytest -q tests/legal_summarizer -k single_flight # single-flight subset
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