# Создание своего skill'а — пошаговый гайд

> Практическое руководство для разработчика. Нормативные правила —
> `docs/TARGET_ARCHITECTURE.md` (§3, §21, §22, §26, §30, §31), контракт
> Skill ↔ Tool — `docs/skill-tool-architecture.md`, текущее состояние
> skill'ов и tool'ов — `docs/skill-tool-inventory.md`. Этот документ — мост
> между «как должно быть» и практическим «что нажимать».

---

## Статус документа

Проект разделён на два дерева: агент (`lib/`, `workspace/`) и платформа
`mcp-platform/` (процесс `enterprise-mcp`). Данные, модель и векторные индексы
уехали на платформу, и большая часть прежней обвязки skill'а была снята вместе
с ними. Прежде чем писать skill, посмотрите, где живёт нужный слой:

| Что | Где живёт сейчас | Статус |
|---|---|---|
| Каталог skill'а (`SKILL.md`) | `workspace/skills/<name>/` | живой; в каталоге один навык — `audit_analyzer` |
| Данные домена | capability `audit` платформы; состав таблиц — `mcp-platform/platform.json` → `audit` | у агента доступа к данным нет |
| Векторные индексы | capability `vectors`; объявления — `platform.json` → `vectors.indexes`, сборка — `python -m servers.enterprise.build_index` (из `mcp-platform`) | у агента кода индекса нет |
| Модель | capability `llm`; настройки и параметры эмбеддера — `platform.json` → `llm` | у агента LLM-клиента нет |
| Снимок данных | capability `data`; путь — `platform.json` → `data.snapshot_path` | у агента нет |
| Объявление навыка | `config.json → gateway.agent.skills.<name>`; форма — `lib/core/project_settings.py` | объявление есть, регистрирующего потребителя нет (§4) |
| Runtime API для skill'ов (`lib/core/skill_config.py` — удалён `b8d3637`) | — | **снят**, §5 |
| Реестр ресурсов (`table_registry.py`, `skill_registration.py`, `infra_registration.py` — удалён `8d63240`) | — | **снят**, §6 |
| CLI навыка (`scripts/cli.py`), `scripts/skill_config.py` | — | **удалены**: доступ к данным даёт tool агента (§7) |
| Валидация SQL (`lib/utils/sql_safety.py`) | `mcp-platform/libs/enterprise_data/sql_safety.py` | уехала на платформу |

Актуальное состояние — [`skill-tool-inventory.md`](skill-tool-inventory.md)
(сводная таблица skill'ов и tool'ов, удалённые компоненты), контракт границ —
[`skill-tool-architecture.md`](skill-tool-architecture.md). Часть расхождений
между документацией и диском ловят тесты: `tests/test_docs_consistency.py`
(относительные ссылки, запрет CLI навыка) и
`tests/test_audit_analyzer_skill_doc.py` (`SKILL.md` против объявлений
платформы).

> Устаревший документ: `docs/table-registry.md` описывает снятый реестр
> ресурсов (`table_registry.py` — удалён, `scripts/register.py`, `_auto_register_skills`)
> и как образец не годится — §6.

---

## 0. TL;DR

**Skill** — доменной пакет для агента:

- инструкции (когда применять, какую операцию выбрать);
- опциональные детерминированные Python-скрипты (парсинг, map-reduce,
  батчинг) — их роль сузилась: за данными они не ходят;
- опциональные references/prompts (progressive disclosure).

Skill **не владеет данными**: ни снимком, ни индексами, ни моделью. Данные
обслуживает capability `audit` платформы, и модель доходит до них через
операции `mcp_enterprise_*` (`config.json → tools.mcpServers`). Skill описывает
в `SKILL.md`, какую операцию и когда звать, — в терминах операций, а не таблиц
и не Python-классов.

Skill **не вызывает** Tool программно (`TARGET_ARCHITECTURE.md` §22.2), Tool **не знает** о Skill (§22.1). Связь — через agent runtime: skill описывает capability терминами, агент решает, какую операцию вызвать.

**Shared infrastructure** (`lib/services`, `lib/core`, `lib/utils`) — общий слой
проверки, исполнения и хранения, используемый и Skills, и Tools. Наличие
callable-функции в `lib/` **не** превращает её в Tool: Tool'ом становится
только то, что агент выбирает и вызывает самостоятельно (§1).

Универсальная структура:

```
workspace/skills/<skill_name>/
    SKILL.md
    scripts/
    references/         # опционально
    prompts/            # опционально
```

Все ключевые инварианты проверяются автоматически — см. §10 «Архитектурные тесты».

---

## 1. Когда создавать Skill, а когда Tool

Перед написанием пройдите decision-чеклист `docs/TARGET_ARCHITECTURE.md:973-1037` (§30). Короткая версия:

| Сценарий | Создаём |
|---|---|
| Доменная логика «как решать задачу X в нашей БД» | **Skill** |
| Доменный workflow из нескольких шагов | **Skill** (`SKILL.md` + `scripts/`) |
| Детерминированная операция **внутри** Skill workflow | **Skill script** |
| LLM-фолбэк на естественном языке для конкретного домена | **Skill** (операция `audit.generate_sql` платформы) |
| Тонкая обёртка вокруг generic utility для домена | **Skill** (описание поверх `lib/utils/`) |
| Capability, которую агент выбирает и вызывает **самостоятельно** | **Tool** (`workspace/tools/`) |
| Реализация, общая для Skill и Tool | **`lib/services`** / **`lib/core`** |
| Универсальный SQL validator / chunker / splitter | **`lib/utils`** |

> **Generic ≠ Tool.** «Capability уже реализована и выглядит generic» —
> **не** достаточное основание завести Tool. Спросите: *агент выбирает и
> вызывает её самостоятельно, как отдельный шаг плана?* Если нет — это
> внутренняя операция Skill'а или shared infrastructure.
>
> Каноничные примеры «generic, но не agent-facing»: свободный read-only
> SQL, семантический поиск, NL→SQL для конкретной схемы. Для них Agent-facing
> Tools (`duckdb_query`, `vector_search`, `nl_sql_generate`) **не создаются**:
> их заменили операции capability `audit` и `vectors` (`audit.run_script`, `audit.generate_sql`,
> `vectors.vector_search`) — см. `docs/skill-tool-architecture.md` §6–§8
> и `docs/skill-tool-inventory.md` («Удалённые компоненты»).
>
> **Операция вместо Tool — не только про экономию строк.** Обёртка над
> операцией переписывала её схему и сводила ошибку к своему формату, и модель
> получала описание, отличное от настоящего. Снятие обёрток оставило у модели
> `inputSchema` самой платформы (change `2026-10-03-mcp-native-tools`, п. D6).

Если вы сомневаетесь — посмотрите на существующий skill (`audit_analyzer`) как
референс.

---

## 2. Структура каталога

### 2.1 Минимум (только `SKILL.md`)

```text
workspace/skills/<skill_name>/
└── SKILL.md
```

Это то, что получилось у `audit_analyzer`: навык не исполняет запросы, а
выбирает операцию и читает её ответ, поэтому исполняемого кода в нём нет.

### 2.2 Полная (`SKILL.md` + `scripts/`)

```text
workspace/skills/<skill_name>/
├── SKILL.md
└── scripts/
    ├── __init__.py
    └── <домен>.py        # детерминированная логика (парсинг, map-reduce, батчинг)
```

`scripts/` опционален и нужен только когда навыку есть что считать самому:
разбор входа, группировка, скоринг, обход пакета файлов. Skill при этом
запускается подпроцессом, своего MCP-клиента у него нет, и единственный
разрешённый выход наружу — клиент платформы
`mcp-platform/libs/enterprise_client/llm.py` (`complete()`, `complete_json()`,
`embed()`). За данными skill не ходит: к ним обращается tool агента (§7).

> Прежняя структура навыка (`predefined/`, `scripts/cli.py` с
> `--mode predefined|vector|generated_sql`, `scripts/skill_config.py` (удалён),
> `scripts/generated_sql_mode.py`, `providers.py` — оба удалён) **снята**: это был Python-слой,
> который сам открывал снимок и строил запросы. Сейчас таких файлов в
> `workspace/skills/audit_analyzer/` нет, и возвращать их не нужно — данные
> обслуживает capability `audit`; возврат CLI ловит
> `tests/test_audit_analyzer_skill_doc.py` и
> `tests/test_docs_consistency.py`.

### 2.3 Три паттерна структуры skill'а

| Паттерн | Когда | Что есть | Пример |
|---|---|---|---|
| **Полный skill** | Своя доменная логика, за ней capability платформы | `SKILL.md` (+ `scripts/`, если есть что считать) + секция `config.json → gateway.agent.skills.<name>` | `audit_analyzer` (по составу каталога — только `SKILL.md`) |
| **Минимальный skill** | Своя логика, которой не за что зацепиться в capability | `SKILL.md` + `scripts/` с детерминированной обработкой | — (в `workspace/skills/` таких нет) |
| **Documentation-only skill** | Только описывает готовый модуль из `lib/utils/*` | **Только** SKILL.md; без `__init__.py`, без `scripts/`, без секции в `config.json` | — (в `workspace/skills/` таких нет) |

**Documentation-only skill** допустим **только** когда выполняются **все** условия:

1. Реализация уже живёт в `lib/utils/<module>.py` и покрыта собственными unit-тестами.
2. У skill'а нет собственной доменной инфраструктуры — нечего объявлять в `config.json`.
3. SKILL.md нужен исключительно для **discovery** агентом при маршрутизации по описанию.

Если хотя бы одно условие не выполнено — это не documentation-only skill, а
полноценный skill без кода. Нужно либо `scripts/`, либо объявление в `config.json`
(либо удалить skill).

**Когда выбирать documentation-only**, а когда полный:

- ✅ Documentation-only: skill — это `extract_text`/`extract_tables`/`summarize` офисного файла поверх общей утилиты.
- ❌ Не documentation-only: skill делает что-то доменное (выбор операции по каталогу, LLM-разбор, map-reduce) — это полный skill.

### 2.4 Чего НЕ должно быть

- **Никаких `register.py`** (удалён) — мёртвый паттерн. Регистрации ресурсов больше нет
  (§6): объявление навыка — это данные в `config.json`, а не код.
- **Никаких `scripts/skill_config.py`** (удалён) — снятый модуль `lib/core/skill_config.py` (удалён `b8d3637`)
  снят (§5). Параметры прогона skill берёт из своей секции в `config.json`.
- **Никаких `scripts/cli.py` с `--mode ...`** — CLI навыка удалён; возврат ловит
  `tests/test_docs_consistency.py::test_readme_md_describes_the_live_audit_analyzer_entrypoint`.
- **Не импортировать `workspace.tools.*`** в skill (§7.3).
- **Не открывать снимок данных, не строить и не читать векторные индексы** —
  это capability `data` и `vectors`.
- **Не заводить свой LLM-клиент** — выход наружу только
  `mcp-platform/libs/enterprise_client/llm.py` (§2.2).
- **Не класть абсолютные пути** вида `/home/<user>/<project>/...` в аргументы
  файловых команд — см. `workspace/AGENTS.md`.
- **Не делать `pip install`** в коде — `requirements.txt` уже полный
  (`workspace/AGENTS.md`).

---

## 3. SKILL.md — что и как писать

### 3.1 Frontmatter (обязательно)

```yaml
---
name: <skill_name>            # совпадает с ключом в config.json → gateway.agent.skills
description: <одна строка>    # как skill выбирается агентом
metadata: {"nanobot":{"emoji":"📊","always":true}}
```

`description` — это всё, что видит LLM-маршрутизатор при выборе skill'а.
Сделайте его конкретным: «анализ данных аудиторских проверок через capability
audit платформы — каталог готовых скриптов, их выполнение, NL→SQL и семантический
поиск», а не «работа с аудитами».

`metadata.nanobot.always: true` — skill всегда виден агенту. Используйте
`false` если skill нужен только по явному запросу.

### 3.2 Структура основной части

Рабочая структура `workspace/skills/audit_analyzer/SKILL.md` — шаблон:

1. **Заголовок H1** с именем skill.
2. **Одно-двухстрочное описание** назначения + кто владеет данными.
3. **Единственная точка входа** — какая операция/tool обслуживает домен.
4. **Порядок выбора** (decision procedure) — самая важная секция.
5. **Каталоги** (скрипты, индексы) — что доступно и откуда берётся список.
6. **Ответы и что они знают** — разбор кодов ошибок и пустых результатов.
7. **Жёсткие правила / Что не делать** — запреты.
8. **Доменная модель** — бизнес-глоссарий без физических имён хранилища.
9. **Тесты** — какие сторожа держат этот файл.

### 3.3 Decision procedure — обязательная секция

Если у навыка больше одной операции, нужна decision procedure
(`workspace/skills/audit_analyzer/SKILL.md:30-53`, «Порядок выбора»):

```text
вопрос про данные аудита
          │
          ├── «найди похожие / по смыслу»  ──→ vectors.vector_search
          │
          └── нужно посчитать / сгруппировать / отфильтровать
                    │
                    ├── в каталоге есть подходящий скрипт?
                    │        ├── да  ──→ audit.run_script
                    │        └── нет ──→ audit.generate_sql
                    │
                    └── (в любом случае сначала audit.list_scripts)
```

Рядом держите таблицу операций с обязательными аргументами
(`audit_analyzer/SKILL.md:23-28`): именно по ней модель выбирает вызов, а
обязательность аргумента проверяет tool и возвращает `invalid_params`.

Описывайте **capability и условия выбора**, а не способ доставки. Конкретный
интерфейс — деталь текущей реализации, а не норма: он может измениться, не
делая `SKILL.md` неверным.

### 3.4 Имена таблиц/индексов

**Не зашивайте физические имена хранилища.** Навык описывает операции, а данные
принадлежат capability: состав доступных таблиц объявлен на платформе
(`mcp-platform/platform.json` → `audit`), логические индексы — там же
(`vectors.indexes`). См. `audit_analyzer/SKILL.md:157-173` («Доменная модель»):

> Физические таблицы и колонки — не зона навыка: они объявлены на платформе и
> проверяются до выполнения. Здесь только бизнес-глоссарий.

Исключение — **логические имена индексов**: их skill называет прямо, потому что
передаёт `index_name` в `vectors.vector_search`, и каждое объявленное имя обязано быть
описано (`tests/test_audit_analyzer_skill_doc.py` сверяет список в `SKILL.md` с
`platform.json` в обе стороны).

### 3.5 Секция «Что не делать»

Всегда явно фиксируйте запреты. Пример из живого skill'а —
`workspace/skills/audit_analyzer/SKILL.md:144-156` («Жёсткие правила»):

- не писать SQL и не просить вернуть его — инструмент текст запроса не принимает;
- не выдумывать имена скриптов, параметров и индексов;
- не вызывать `exec` / `python` ради данных — путь только один;
- не обещать «актуальные на сейчас» данные: ответ отражает снимок на момент
  последней загрузки.

Прежние примеры из удалённых skill'ов (`legal_summarizer` уехал в capability
`legal_summarizer` платформы, `office_files` удалён) в дереве
`workspace/skills/` больше не лежат — ориентируйтесь на `audit_analyzer`.

### 3.6 Anti-patterns в SKILL.md

- ❌ Описывать конкретные Python-классы tools. Пишите в терминах capability
  («выполни семантический поиск по индексу `audits_index'`»), не в терминах
  Python («call `VectorSearchTool.execute(...)`»). См. `skill-tool-architecture.md` §5.
- ❌ Дублировать полную схему БД в SKILL.md. Используйте progressive
  disclosure — большие reference-файлы выносите в `references/` (у skill'ов агента такого каталога нет в дереве репозитория; живой пример — `mcp-platform/libs/legal_summarizer/skill/references/`).
- ❌ Подмешивать «как именно реализован Python внутри runtime» —
  skill описывает capability, а не код.

---

## 4. Объявление навыка в `config.json`

> **Сначала прочитайте врезку, иначе правильный текст будет прочитан неправильно.**
> Секция объявления **жива** (модели в `lib/core/project_settings.py`, ключи в
> `config.json` есть), но **регистрирующего потребителя у неё больше нет**:
> `_auto_register_skills` и реестр ресурсов сняты (§5, §6). Это декларация, а не
> механизм. Состав таблиц и индексов секция навыка не объявляет и объявить не
> может: полей `tables`/`vector_indexes` в ней больше нет.
>
> **Авторитетные объявления живут на платформе:**
> состав доступных таблиц — `mcp-platform/platform.json` → `audit`,
> векторных индексов — `platform.json` → `vectors.indexes`,
> параметров эмбеддера — `platform.json` → `llm`. Правило «держать секции
> синхронными руками» отменено вместе с полями — дублировать больше нечего.
> Объявить состав в `config.json` повторно нельзя: `SkillSettings` —
> `extra="forbid"` (`lib/core/project_settings.py:500`), и остаток ключа
> поднимает `ConfigurationError` на старте gateway.

### 4.1 Секция `gateway.agent.skills.<name>` — форма

```jsonc
"gateway": {
  "agent": {
    "skills": {
      "<skill_name>": {
        "enabled": true,                          // OPTIONAL, default true
        "cli": { ... },                           // OPTIONAL — §4.4
        "llm": { "max_tokens": 8192, "temperature": 0.1 },   // OPTIONAL
        "chunking": { ... },                      // OPTIONAL — §4.4
        "brief_context": { ... },                 // OPTIONAL — §4.4
        "execution": { ... }                      // OPTIONAL — §4.4
      }
    }
  }
}
```

Форму описывают pydantic-модели `lib/core/project_settings.py`: `SkillSettings`
(`457-507`, `model_config = ConfigDict(extra="forbid")` на `:500`), контейнер
`SkillsSettings` (`510-568`).

**Fail-fast на этой секции действует.** Путь в `config.json` —
`gateway.agent.skills.<name>`, и это не обходит валидацию: `config.py` поднимает
`gateway.agent.{project,cli,skills,logging,enterprise_mcp}` в корень **до**
остальных шагов merge (`_lift_agent_sections`, `config.py:452-487`, вызов на
`:907`), поэтому `validate_project_settings` видит секцию как верхнеуровневую
`skills`, а `SkillsSettings._validate_skill_sections`
(`project_settings.py:529-568`) прогоняет каждую вложенную секцию через
`SkillSettings.model_validate`. Опечатка (`defualt_mode`) и остаток снятого поля
(`tables`, `vector_indexes`, `embedding`, `cache`) поднимают
`ConfigurationError` на старте gateway — проверять секцию руками не нужно.
Валидация вызывается при старте в `lib/core/application_context.py:323-325`
(`validate_project_settings`).

### 4.2 Секция `tables` — снята

Состав таблиц в `skills.<name>` **не объявляется**: поля `tables` и модель
`TableEntry` удалены из `lib/core/project_settings.py` вместе с реестром
ресурсов, который их принимал. Искать описание формы в дереве агента
бессмысленно — описывать состав таблиц навыку больше нечем.

**Где объявляется теперь:** `mcp-platform/platform.json` → `audit.tables`.
Список читает capability `audit` (по нему проверяется сгенерированный запрос) и
оттуда же capability `data` наполняет снимок.

Бывшие поля навыка и их сегодняшний адрес:

| Поле (снято) | Где живёт теперь |
|---|---|
| `name` | `audit.tables[*].name` — формат `"schema.table"` |
| `label` (opaque-метка, отсекавшая реестры метаданных вроде `public.agent_predefined_scripts` от доменных таблиц) | `audit.tables[*]`; в агенте метку не читал никто |
| `tracking_column` (колонка инкрементального обновления) | платформенное понятие; в агенте его не читал никто |

**Повторное объявление отвергается.** `SkillSettings` — `extra="forbid"`
(`project_settings.py:500`), поэтому остаток `tables` в секции навыка
поднимает `ConfigurationError` на старте, а не игнорируется. Снятие полей
ужесточило контракт: пока их описывала модель, `forbid` их типизировал, теперь —
отвергает.

### 4.3 Секция `vector_indexes` — снята

Имена и параметры векторных индексов в `skills.<name>` **не объявляются**:
поле `vector_indexes` и модель `VectorIndexEntry` удалены. Агент своего списка
индексов не держит ни в `gateway.*`, ни в `skills.*` (требование «Агент не
объявляет состав индексов», `openspec/specs/data/vector-indexes`).

**Где объявляется теперь:** `mcp-platform/platform.json` → `vectors.indexes`
(состав и параметры каждого индекса: `table`, `pk`, `source_table`,
`content_columns`, `embedding_columns`, `track_column`, `chunk_size` /
`chunk_overlap`, `metric`, `enabled`) и `platform.json` → `vectors.storage_table`
(таблица сырых эмбеддингов). Модель узнаёт имена индексов операцией
`vectors.list_indexes`, а не из `config.json`. Прежнее `gateway.vector.index.*` снято
целиком.

**Чего в `skills.<name>` быть не должно** (всё это — платформенные понятия, и
`extra="forbid"` завернёт любое из них в `ConfigurationError`):

- `source` и прочие поля объявления индекса — `platform.json` →
  `vectors.indexes.<name>`;
- `embedding` и параметры эмбеддера — `platform.json` → секция `llm`:
  `embed_api_base`, `embed_path`, `embed_model`, `embed_dimension`,
  `embed_timeout`, `embed_key` (`${EMBED_TOKEN}`). Захардкоженных констант
  `_EMBED_*` в коде агента нет.

### 4.4 Опциональные runtime-секции

| Секция | Обязательные поля | Расширения |
|---|---|---|
| `cli` | `default_mode`, `default_format`, `max_retries`, `timeout_sec` (`project_settings.py:506-516`) | `SkillCliSettings(extra='allow')` — skill-специфичные флаги допустимы. |
| `llm` | `max_tokens`, `temperature` (не выбор модели!) (`project_settings.py:519-533`) | — |
| `chunking` | `chunk_size`, `chunk_overlap`, `single_call_threshold` (`project_settings.py:536-554`) | — |
| `brief_context` | пороги и оценки символов на символ (`project_settings.py:557-571`) | — |
| `execution` | бюджеты и батчинг контекста (`project_settings.py:574-603`) | — |

**Модель, провайдер и адрес API — не здесь.** Общение с моделью принадлежит
capability `llm` платформы (`platform.json` → `llm`). `skills.<name>.llm` —
только execution policy: сколько токенов и какая температура.

> **Контракт `extra="allow"`:** вложенные секции (`SkillCliSettings`,
> `SkillLlmSettings`, `SkillChunkingSettings`, `SkillBriefContextSettings`,
> `SkillExecutionSettings`) наследуют `_StrictOptional(extra='allow')`, поэтому
> skill-специфичные поля проходят валидацию. Однако **собственные** поля
> `SkillSettings` — `enabled`, `cli`, `llm`, `chunking`, `brief_context`,
> `execution` — `extra="forbid"`: это и fail-fast на опечатках, и запрет
> вернуться к снятым `tables`/`vector_indexes`/`embedding`/`cache`.
> Это намеренная асимметрия: жёсткий контракт на уровне декларации, мягкое
> расширение внутри каждой подсекции.

### 4.5 Что НЕ должно быть в `skills.<name>`

| Legacy ключ | Куда перенесён |
|---|---|
| `embedding.*` | → платформа, `platform.json` → `llm.embed_*` |
| `cache.*` (был мёртвым) | — (удалён; снимком владеет capability `data`) |
| `sync.*` | — (удалена: синхронизации в агенте нет) |
| `tables[*]` | → платформа, `platform.json` → `audit.tables` (§4.2) |
| `vector_index.*` (секция, не массив) | → платформа, `platform.json` → `vectors.indexes` / `vectors.storage_table` (§4.3) |
| `vector_indexes[].source` | → платформа, `platform.json` → `vectors.indexes.<name>` |

Обратной совместимости нет — legacy-ключи ловит
`tests/test_no_legacy_imports.py`.

### 4.6 Валидация

Pydantic-валидация выполняется на старте в `ApplicationContext.create()`
(`lib/core/application_context.py:262-265`) и падает с `ConfigurationError` со
списком всех проблем сразу. Формы секций зафиксированы тестами
`tests/test_project_settings.py`.

---

## 5. Runtime API для skill'ов

> **Раздел описывает снятый API.** Модуля `lib/core/skill_config.py` (удалён `b8d3637`) в проекте
> нет: skill больше не получает доступ к данным, DuckDB-снимку и векторным
> индексам сам. Реестр ресурсов (`table_registry.py` — удалён `8d63240`), декларативная регистрация
> (`skill_registration.py`) и `infra_registration.py` снесены вместе с ним
> (фаза 5, 2026-10-01). Живой инвентарь — `docs/skill-tool-inventory.md`.

Единственный вход skill'а к данным аудита — операции capability `audit`
платформы, объявленные в `config.json → tools.mcpServers` и приходящие как
`mcp_enterprise_{audit_list_scripts,audit_run_script,audit_generate_sql,vectors_vector_search}`. Параметры
прогона skill берёт из своей секции
`config.json → gateway.agent.skills.<name>`, а к LLM ходит операцией `llm.complete`
capability `llm`.

---

## 6. Реестр ресурсов (снят)

> **Раздел описывает снятую подсистему.** `lib/services/table_registry.py` (удалён `8d63240`),
> `lib/core/skill_registration.py` и `lib/core/infra_registration.py` удалены
> вместе с локальным кэшем (фаза 5, 2026-10-01). Реестра ресурсов с владельцем
> у него больше нет: состав таблиц снимка объявляет capability `data` платформы
> (`mcp-platform/platform.json -> audit.tables`), а состав векторных индексов -
> capability `vectors` (`vectors.indexes`). Ни skill, ни tool не регистрируют
> ресурсы сами.

---

## 7. Контракт Skill ↔ Tool

### 7.1 Главный принцип (`TARGET_ARCHITECTURE.md §22.1-§22.9`)

```text
SKILL instructions → Agent → selects Tool → Tool executes capability
```

Skill **не вызывает** Tool программно (TARGET §22.2,
`tests/test_skill_tool_independence.py:59-73`).
Tool **не импортирует** Skill (TARGET §22.1,
`tests/test_skill_tool_independence.py:76-89`).

### 7.1.1 Два пути к одной инфраструктуре

Инфраструктура данных и модели уехала в процесс `enterprise-mcp`: снимок
загружает и держит capability `data` (путь — `platform.json` →
`data.snapshot_path`, по умолчанию `~/.cache/nanobot/duckdb/cache.duckdb`),
векторные индексы строит и держит в памяти capability `vectors`, модель —
capability `llm`. В агенте этого кода больше нет: ни локального кэша, ни
`CacheProvider`, ни FAISS. Снимок актуален на момент загрузки и обновляется
перезапуском процесса.

К этой инфраструктуре подключаются **две независимые поверхности**:

| Поверхность | Кто использует | Когда |
|---|---|---|
| **Операции capability `audit` по MCP** | `config.json → tools.mcpServers.enterprise` | Единственный вход к данным аудита: `mcp_enterprise_{audit_list_scripts,audit_run_script,audit_generate_sql,vectors_vector_search}` |
| **Операция `llm.complete` capability `llm`** | Клиент платформы `mcp-platform/libs/enterprise_client/llm.py` | Обращение к модели из skill'а, запущенного подпроцессом |

Прямого доступа к данным у skill'а больше нет: снимком владеет capability `data`,
индексами — capability `vectors`, и оба живут в процессе `enterprise-mcp`.
Skill запускается подпроцессом без собственного MCP-клиента, поэтому ходит к
модели через `libs.enterprise_client.llm`, а к данным — только через tool агента.

Связь skill↔данные — **через agent runtime**: skill в `SKILL.md` описывает
capability в терминах операций («выполни семантический поиск по индексу
`violations_index`»), агент выбирает tool и передаёт аргументы. Сам skill tool'ы
**программно не вызывает**.

### 7.2 Что РАЗРЕШЕНО в Skill

```python
from lib.utils.text_utils import truncate_middle     # ЗАПРЕЩЕНО
```

Skill не импортирует `lib` и не касается данных. Из общей инфраструктуры ему
доступны только чистые утилиты без побочных эффектов; всё, что ходит в базу,
живёт в tool'ах агента.

### 7.3 Что ЗАПРЕЩЕНО

**В Tool** (`skill-tool-architecture.md:50-60`,
`tests/test_architecture_tool_domain_free.py`):

```python
from workspace.skills.<anything> import ...           # ЗАПРЕЩЕНО
spec_from_file_location(...)                          # ЗАПРЕЩЕНО
sys.path.insert(.../skills...)                        # ЗАПРЕЩЕНО
```

**В Skill** (`skill-tool-architecture.md:57-60`,
`tests/test_skill_tool_independence.py`):

```python
from workspace.tools import ...                       # ЗАПРЕЩЕНО
from lib.hooks.mcp_identity_hook import McpIdentityHook   # ЗАПРЕЩЕНО
```

Личность вызова подставляет framework-хук `McpIdentityHook` перед вызовом
операции; skill'у и tool'у (а tool'а, покрывающего capability, больше нет)
нечего знать про `session_id` / `user_id` / `request_id`.

### 7.4 Что Tool не должен знать

Запрещены домен-идентификаторы: `audit`, `violations`, `audits_index`,
`audit_analyzer`. Любой `if caller == "...":` routing — запрещён
(TARGET §22.9). Tool — generic capability.

### 7.5 Что Skill не должен знать

Skill пишет инструкции в терминах capability, не Python:

- ✅ «выполни семантический поиск по индексу `violations_index`»
- ❌ «call `VectorSearchTool.execute(query=...)`»
- ❌ «import VectorSearchTool»

Норма фиксирует **форму** инструкции, а не способ доставки: конкретный вызов
(`operation=vector_search` вместо прежнего `--mode vector`) — деталь текущей
реализации, а не требование.

### 7.6 Capability доступ Skill'ам

| Capability | Контракт | Конфиг |
|---|---|---|
| `mcp_enterprise_audit_list_scripts` | — → каталог допустимых скриптов | `config.json → tools.mcpServers.enterprise.enabled_tools` |
| `mcp_enterprise_audit_run_script` | `{script, params}` → `{status, columns, rows, ...}` | там же |
| `mcp_enterprise_audit_generate_sql` | `{query}` → SQL и результат | там же |
| `mcp_enterprise_vectors_vector_search` | `{query, index_name}` → результаты поиска | `mcp-platform/platform.json → vectors.indexes` |
| `mcp_enterprise_vectors_list_indexes` | — → имена и состояние векторных индексов | `mcp-platform/platform.json → vectors.indexes` |
| `compact_context` tool | `{session_key, force}` | `config.json → gateway.compact.*` |

Skill-side CLI (`scripts/cli.py` с `--mode predefined|vector|generated_sql`)
**не существует** и не должен появляться. Отдельные generic-tools
`duckdb_query` / `vector_search` тоже не создаются: это внутренние операции
платформы, а не agent-facing capability (границы — в
`docs/skill-tool-architecture.md` § 6–§8). Новый Tool заводится **только** при
agent-facing критерии (§1); образец — `workspace/tools/document_read.py`
(извлечение текста: платформа отдаёт данные, но не отдаёт готовый текст
документа — то есть операции здесь не помогут).

---

## 8. Storage policy и пути

Из `workspace/AGENTS.md`:

- Новые файлы — в `files/` каталога сессии, то есть
  `data_store/sessions/<session_key>/files/`. **НЕ**
  пишите в корень проекта.
- Используйте **относительные пути** в `write_file`/`write`/`edit` —
  `SessionFileRedirectHook` (`workspace/hooks/session_file_redirect_hook.py`)
  сам перенаправит их. Хук работает **только** для `write_file`/`edit` —
  не для произвольных `exec`-команд.
- **Запрещены** абсолютные пути вида `/home/<user>/<project>/...` —
  на сервере таких путей нет.

### 8.1 Файловый вход (`--file`) у skill'а с `scripts/`

`SessionFileRedirectHook` НЕ перенаправляет пути в произвольных командах
(`exec`, `nanobot exec`) — он рассчитан только на `write_file`/`edit`. Скрипт
навыка **сам не делает redirect** и не имеет доступа к session_key агента.
Поэтому **агент обязан передавать корректный путь явно**.

Допустимые пути для `--file <path>`:

- ✅ **Абсолютный** путь: `<project_root>/data_store/sessions/<session_key>/files/<file>.pdf`
- ✅ **Относительный от корня репо** (cwd = корень проекта): `data_store/sessions/<session_key>/files/<file>.pdf`
- ❌ Только basename файла (`<file>.pdf` без префикса) — обработчик вернёт
  «Файл не найден», потому что в cwd такого файла нет.

Если агент не знает session_key и видит только basename из media-attach —
он должен найти файл через `glob` по `data_store/sessions/*/files/**/<file>`
или передать абсолютный путь, который знает из контекста канала.

Скрипт навыка со своей стороны **не делает redirect-логику** — это контрактная
ответственность агента: «передавай то, что есть; мы валидируем и либо читаем,
либо отдаём структурированную ошибку с понятным сообщением».

Живой пример приёма файлового входа в агенте — tool
`workspace/tools/document_read.py`: путь приходит аргументом, разбор текста
делегирован парсеру платформы (`mcp-platform/libs/office`).

---

## 9. Тестирование skill'а

### 9.1 Что тестировать

| Слой | Тесты |
|---|---|
| **Документ skill'а** | `tests/test_audit_analyzer_skill_doc.py` — четыре операции названы с обязательными аргументами, индексы совпадают с `platform.json` в обе стороны, физических имён (таблиц, снимка, движков) нет |
| **Объявление операций** | `tests/test_mcp_platform_declaration.py` — состав `enabled_tools`, минимальный env, флаг `require_call_meta`, равенство имён ключей идентичности файлу платформы |
| **Подстановка личности** | `tests/test_mcp_identity_hook.py` — инъекция трёх ключей, перебитие присланного моделью (в т.ч. частичной подмены), досылка `request_id`, когда у оборота его нет, и отказ хука `McpIdentityRefused`, когда нет `session_id`/`user_id` — то есть вызов идёт вне оборота вообще; «неполной личности» как отдельного случая нет |
| **Tool** | `tests/test_tools_project_loader.py` — регистрация и баннер инвентаря |
| **Architecture** | `tests/test_skill_tool_independence.py`, `tests/test_architecture_tool_domain_free.py`, `tests/test_core_infrastructure_independence.py` |
| **Конфиг и инвентарь** | `tests/test_project_settings.py`, `tests/test_runtime_inventory.py` |
| **Согласованность документации** | `tests/test_docs_consistency.py`, `tests/test_no_legacy_imports.py` |

Тестов «skill против живого DuckDB-кэша» и «регистрации skill'а» больше нет:
и кэша у агента нет, и регистрации тоже (§4, §6).

### 9.2 Шаблон теста tool'а навыка

**Tool'а, покрывающего capability, у навыка теперь нет** — операции приходят
модели штатным MCP-клиентом, и тестировать нечего: схема и обработка живут в
`mcp-platform`, где у платформы свои тесты (`test_audit_capability.py`,
`test_vectors_*`, `test_data_service.py::TestHistorySearchIsolation`).

Что остаётся тестировать на стороне агента — **собственный** код: хук
`McpIdentityHook` и объявление операций. Паттерн — подставить контекст оборота
и проверить словарь аргументов после вызова хука; ни сеть, ни снимок, ни
модель в таком тесте не участвуют.

```python
params: dict = {"query": "сколько аудитов за 2024"}
with _sender("alice"):
    await hook.before_execute_tool(ctx, tool_call, None, params)
assert params["user_id"] == "alice"          # личность подставлена
assert "session_id" not in {"q"}             # доменный аргумент не тронут
```

Тест проверяет ровно две вещи: **что подставлено** (три ключа личности на
месте) и **что не перебито** (значение, присланное моделью, заменено на
принадлежащее обороту). Второе важнее: в опубликованной схеме таких полей
нет, поэтому в аргументах они могут прийти только снизу.

Живой образец — `tests/test_mcp_identity_hook.py` и
`tests/test_mcp_platform_declaration.py`.

### 9.3 Что НЕ нужно тестировать

Не пишите тестов регистрации (`register.py` — удалён, `_ensure_registered()`,
`tests/test_skill_register.py` — удалён): регистрации больше нет (§6), и такой тест
проверял бы код, которого не существует. Лучше покройте доменную логику
навыка и границы его собственного tool'а, если он есть.

---

## 10. Архитектурные тесты — обязательно зелёные

Перед коммитом убедитесь, что эти тесты проходят (поломан любой =
архитектурная регрессия):

```bash
pytest tests/test_skill_tool_independence.py          -v
pytest tests/test_architecture_tool_domain_free.py    -v
pytest tests/test_core_infrastructure_independence.py -v
pytest tests/test_audit_analyzer_skill_doc.py         -v
pytest tests/test_mcp_platform_declaration.py         -v
pytest tests/test_mcp_identity_hook.py               -v
```

Что они проверяют:

- `test_skill_tool_independence.py` — Skill не импортирует Tool, Tool не импортирует Skill.
- `test_architecture_tool_domain_free.py` — Tool не содержит audit/домен-строк в коде и описаниях.
- `test_core_infrastructure_independence.py` — `lib/services` и `lib/utils` не зависят от skills.
- `test_audit_analyzer_skill_doc.py` — `SKILL.md` описывает реальные операции платформы и не содержит физических имён хранилища.
- `test_mcp_platform_declaration.py` — объявление операций согласовано с платформой, а имена ключей идентичности — с её конвейером.
- `test_mcp_identity_hook.py` — личность оборота подставляется и не может быть перебита значением из аргументов.

---

## 11. Best practices — сводка

### 11.1 DO

✅ Пишите `SKILL.md` в терминах capability, не Python-классов Tool'ов.

✅ Описывайте операции tool'а и условия их выбора; обязательные аргументы
перечисляйте явно — по ним модель строит вызов.

✅ Называйте логические имена индексов только те, что объявлены в
`mcp-platform/platform.json` → `vectors.indexes`.

✅ Берите параметры прогона из секции `config.json → gateway.agent.skills.<name>`,
не дублируя их литералами в коде.

✅ Соблюдайте storage policy из `workspace/AGENTS.md` — относительные пути.

✅ Используйте progressive disclosure — большие знания выносите в `references/` (у skill'ов агента такого каталога нет в дереве репозитория; TARGET §10, §25).

✅ Запускайте архитектурные тесты (см. §10).

✅ Покрывайте страж документа skill'а и границы его tool'а (см. §9).

### 11.2 DON'T (anti-patterns)

❌ `from workspace.tools import ...` в Skill (TARGET §22.2).

❌ Hardcode домен-имён в Skill (TARGET §22.3).

❌ Прятать домен-логику в `lib/services` (TARGET §22.9).

❌ Создавать `register.py` или `scripts/skill_config.py` (оба удалён) — (§5, §6).

❌ `pip install` в коде skill'а. Все библиотеки в `requirements.txt`.

❌ Абсолютные пути `/home/<user>/<project>/...`.

❌ Tool, который знает о Skill (поймает `test_architecture_tool_domain_free.py`).

❌ Заводить Tool только потому, что capability уже реализована и выглядит generic. Сначала §1 / TARGET §30 вопрос 11.

❌ Multi-statement SQL или DDL/DML. Валидация запроса —
`mcp-platform/libs/enterprise_data/sql_safety.py::validate_sql`; в агенте
модуля `lib/utils/sql_safety.py` больше нет.

❌ Секреты в `config.json` — `${VAR}` + `.secrets.env`.

❌ Заводить свой LLM-клиент: модель принадлежит capability `llm`, выход из
skill'а — `mcp-platform/libs/enterprise_client/llm.py`.

❌ Параметры эмбеддера, путь снимка и объявления индексов в `skills.<name>` —
это объявления платформы (`platform.json` → `llm`, `data`, `vectors`).

❌ Зашивать физические имена таблиц в код или промпты как строковые константы.

---

## 12. Definition of Done — чек-лист

Перед коммитом нового skill'а:

### Обязательная часть (все паттерны)

1. ☐ Структура соответствует одному из трёх паттернов §2.3 (полный / минимальный / documentation-only).
2. ☐ `SKILL.md` написан по §3: правильный frontmatter, decision procedure, «Что не делать».
3. ☐ Skill **НЕ импортирует** `workspace.tools` и **НЕ вызывает** Tool'ы (в т.ч. через tool-call).
4. ☐ Skill **не зависит** от конкретных Tool implementation: данные приходят через
   операции capability, а не через чужой код tool'а. Tool создан **только** если
   capability действительно agent-facing — агент выбирает и вызывает её самостоятельно, как
   отдельный шаг плана (§1, TARGET §30 вопрос 11); наличие готовой generic-функции в
   `lib/services` основанием для Tool'а не является.
5. ☐ Архитектурные тесты `tests/test_skill_tool_independence.py tests/test_architecture_tool_domain_free.py tests/test_core_infrastructure_independence.py tests/test_audit_analyzer_skill_doc.py tests/test_mcp_platform_declaration.py tests/test_mcp_identity_hook.py` — без падений.
6. ☐ `pytest tests/ -q` — без регрессий.
7. ☐ `python cli_agent.py` стартует без ошибок (smoke).
8. ☐ Документация обновлена:
    - `docs/skill-tool-inventory.md` (строка в сводной таблице);
    - `docs/README.md` (если добавился новый файл);
    - корневой `AGENTS.md` (если новый ключ config);
    - `CHANGELOG.md` (секция `[Unreleased]`).

### Полный skill (audit_analyzer)

9. ☐ Каталог `workspace/skills/<name>/SKILL.md` создан; `scripts/` — только если
   навыку есть что считать самому (§2.2). `scripts/cli.py` и
   `scripts/skill_config.py` (удалён) не заводятся.
10. ☐ В `config.json` добавлена секция `gateway.agent.skills.<name>` с
    параметрами прогона (§4.1); физические таблицы в skill'е не упоминаются.
11. ☐ Состав доступных таблиц совпадает с `mcp-platform/platform.json` → `audit`.
12. ☐ Каждый упомянутый в `SKILL.md` индекс объявлен в `platform.json` →
    `vectors.indexes` (сверяет `tests/test_audit_analyzer_skill_doc.py`).
13. ☐ Тест стража документа skill'а проходит.

### Минимальный skill

9'. ☐ Каталог `workspace/skills/<name>/{SKILL.md, scripts/}` создан (секция в
`config.json` объявляет только домен skill'а — состав таблиц и индексов не
переносите в неё, §4.2–§4.3).
10'. ☐ В `config.json` есть `gateway.agent.skills.<name>` с `llm`/`chunking`/`cli`
    (по необходимости).
11'. ☐ Если скрипт ходит к модели — только через
    `mcp-platform/libs/enterprise_client/llm.py`.
12'. ☐ Есть тест на доменную логику `scripts/` (паттерн §9.2).

### Documentation-only skill

9''. ☐ Реализация уже живёт в `lib/utils/<module>.py`.
10''. ☐ У skill'а нет доменной инфраструктуры — секцию в `config.json` НЕ трогаем.
11''. ☐ SKILL.md секции: «Когда использовать», «Когда не вызывать», «Что не делать» (может называться «Ограничения»), «Что внутри» со ссылкой на utility-модуль.

---

## 13. Пошаговый сценарий создания нового skill'а

### Шаг 1. Спроектируйте

- Это Skill, Tool или shared infrastructure? (см. §1, TARGET §30 вопрос 11)
- Какая capability стоит за доменом и какие операции она даёт?
- Нужны ли `scripts/` (есть что считать)? Нужна ли модель? Чанкинг?

### Шаг 2. Создайте структуру каталога

```bash
mkdir -p workspace/skills/<name>
```

`scripts/` добавляйте, только если навыку есть что считать самому (§2.2);
`__init__.py` в корне каталога skill'а не нужен.

### Шаг 3. SKILL.md (см. §3)

### Шаг 4. Объявите в `config.json` (см. §4)

```jsonc
"gateway": {
  "agent": {
    "skills": {
      "<name>": {
        "enabled": true,
        "llm": {
          "max_tokens": 8192,
          "temperature": 0.1
        }
      }
    }
  }
}
```

Состав таблиц и индексов в эту секцию не дублируется «на всякий случай»: он
объявлен на платформе (`mcp-platform/platform.json` → `audit` и
`vectors.indexes`), и расхождения никто не проверяет. Параметры эмбеддера
(`embed_api_base`, `embed_path`, `embed_model`, `embed_dimension`,
`embed_timeout`, `embed_key` = `${EMBED_TOKEN}`) — тоже там, в секции `llm`.

### Шаг 5. Реализуйте `scripts/` (если нужен)

- `scripts/__init__.py`;
- `scripts/<домен>.py` — детерминированная логика;
- к модели — только `mcp-platform/libs/enterprise_client/llm.py`
  (`complete()`, `complete_json()`, `embed()`).

Образца в `audit_analyzer/scripts/` нет: навык сейчас `SKILL.md`-only.
Ориентируйтесь на §2.2 и на границы §7.

### Шаг 6. Тесты (см. §9)

### Шаг 7. Документация (см. §12)

### Шаг 8. Проверки

```bash
pytest tests/test_skill_tool_independence.py \
       tests/test_architecture_tool_domain_free.py \
       tests/test_core_infrastructure_independence.py \
       tests/test_audit_analyzer_skill_doc.py \
       tests/test_mcp_platform_declaration.py \
       tests/test_mcp_identity_hook.py -v

pytest tests/ -q
python cli_agent.py          # smoke
```

---

## 14. Сводка референсных файлов

### Нормативные документы
- `docs/TARGET_ARCHITECTURE.md` — нормативный контракт (§3, §22.1-§22.9, §30, §31).
- `docs/skill-tool-architecture.md` — Skill ↔ Tool contract.
- `docs/skill-tool-inventory.md` — текущее состояние skill'ов и tool'ов.

### Живой код агента
- `workspace/skills/audit_analyzer/SKILL.md` — рабочий образец доменного навыка.
- `workspace/skills/enterprise_mcp/SKILL.md` — рабочий образец навыка-контракта.
- `config.json → tools.mcpServers.enterprise` — единственный вход к данным аудита
  (операции `mcp_enterprise_*`).
- `lib/hooks/mcp_identity_hook.py` — подстановка личности оборота в вызов операции.
- `workspace/tools/document_read.py` — чтение текста офисных документов.
- `lib/core/project_settings.py` — форма секции `skills.<name>` (`SkillSettings`,
  `extra="forbid"`; контейнер — `SkillsSettings`). Состава таблиц и индексов
  эта модель больше не описывает.
- `lib/services/enterprise_mcp_client.py` — клиент агента к платформе для фоновых
  служб (вне оборота модели).

### Живой код платформы
- `mcp-platform/platform.json` — объявления capability: `audit` (таблицы),
  `vectors` (индексы, `storage_table`), `data` (путь снимка), `llm` (модель и `embed_*`).
- `mcp-platform/libs/enterprise_client/llm.py` — `complete()`, `complete_json()`, `embed()` для skill'ов-подпроцессов.
- `mcp-platform/libs/enterprise_data/sql_safety.py::validate_sql` — SQL security boundary.
- `mcp-platform/servers/enterprise/build_index.py` — сборка векторных индексов.

### Конфигурация
- `config.json` — главная карта; секция навыка — `gateway.agent.skills.<name>`.

### Существующие skill'ы как reference
- `workspace/skills/audit_analyzer/` и `workspace/skills/enterprise_mcp/` — два
  skill'а в `workspace/skills/`; в каждом только `SKILL.md` (логика уехала в
  capability платформы).
- `legal_summarizer` и `office_files` — каталогов в `workspace/skills/` больше нет
  (`legal_summarizer` живёт в capability `legal_summarizer` платформы).

### Тесты для архитектурных инвариантов
- `tests/test_skill_tool_independence.py`
- `tests/test_architecture_tool_domain_free.py`
- `tests/test_core_infrastructure_independence.py`
- `tests/test_audit_analyzer_skill_doc.py`
- `tests/test_mcp_platform_declaration.py`
- `tests/test_mcp_identity_hook.py`
- `tests/test_project_settings.py`
- `tests/test_docs_consistency.py`
- `tests/test_no_legacy_imports.py`
- `tests/test_runtime_inventory.py`

### Hooks и runtime
- `lib/hooks/tool_audit_hook.py` — автоматическая audit trail для всех tool'ов.
- `lib/hooks/mcp_identity_hook.py` — личность вызова для операций платформы.
- `workspace/hooks/session_file_redirect_hook.py` — перенаправление файлов в каталог сессии.
- `workspace/hooks/recent_files_hook.py` — автоприкрепление созданных файлов.
- `workspace/tools/{compact_context,document_read}.py` — два оставшихся tool'а.
  Образец нового tool'а — `document_read.py`: платформа отдаёт данные, но не
  готовый текст документа, то есть операциями его не закрыть. (Tools
  `duckdb_query` / `vector_search` не существуют.)

При изменении `TARGET_ARCHITECTURE.md` или `skill-tool-architecture.md`
синхронизировать этот документ.
