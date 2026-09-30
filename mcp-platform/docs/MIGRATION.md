# Фаза 1 — инвентаризация

> **Устарело в части целевой схемы.** Фаза 1 фиксировала состояние на момент
> старта. Целевая архитектура и итоговая классификация модулей изложены в
> [`TARGET-ARCHITECTURE.md`](TARGET-ARCHITECTURE.md) — там 2 MCP вместо 4
> серверов, локальный снимок DuckDB остаётся под владением capability `data`,
> и раздел «Порядок переноса» ниже заменён.
> Этот документ остаётся верным как описание **исходного** состояния.

Снято **до** любых изменений кода. Цель: понять реальную поверхность
миграции, а не переписывать проект по памяти.

## Главный вывод, меняющий план

> **Связанность с Nanobot в enterprise-логике близка к нулю.**

Проверено AST/grep по всему репозиторию:

| Зона | Импорты `nanobot.*` | Комментарий |
|---|---:|---|
| `workspace/skills/**` | **0** | audit, legal, office — уже чисты |
| `lib/**` | 74 | это фреймворк-обвязка агента, её **не трогаем** |
| `workspace/tools/**` | 6 | ровно 3 tool-файла, это и есть поверхность переноса |
| `benchmarks`, `tools/**` | 3 | вспомогательное |

Enterprise-домены уже вынесены в `workspace/skills/` и не знают про агента.
Перенос — это в основном **смена места жительства** плюс обёртка в MCP,
а не вытаскивание кода из агента.

## Карта механизмов

| Механизм | Текущий код | Зависимость от Nanobot | Целевое место | Объём |
|---|---|---|---|---|
| Audit analyzer | `workspace/skills/audit_analyzer/` + `scripts/cli.py` | нет | `servers/audit/` | **малый** — CLI-граница уже есть |
| Legal summarizer | `workspace/skills/legal_summarizer/` + `scripts/cli.py` **и** `workspace/tools/legal_summarizer_query.py` (14.8 KB) | только tool-обёртка | `servers/legal/` | средний — две точки входа, схлопнуть в одну |
| History / memory | `workspace/tools/history_search_tool.py` (30.6 KB) | да (`Tool`, `ToolContext`) | `servers/memory/` | **средний** — самый крупный tool |
| Context compaction | `lib/services/context_compaction.py` | да | **остаётся в агенте** | не переносится (Фаза 7) |
| Document / Office | `workspace/skills/office_files/`, `workspace/utils/office_files.py` | нет | `servers/document/` | малый — чистая библиотека |
| DuckDB / FAISS / vector | `lib/services/cache_provider*.py`, `duckdb_cache_store.py`, `vector_index_service.py` | косвенно | `libs/enterprise_data/` | **высокий риск** — лежит в `lib/` |
| SQL safety (sqlglot) | `lib/utils/sql_safety.py` | нет | `libs/enterprise_data/` | средний |
| AgentLoop / RuntimePatcher | `lib/services/runtime_patcher.py` (10 импортов) | да | **не переносим** | вне области |

## Две поправки к исходному плану

### 1. Эталона «follow-up MCP» не существует

План предлагает перенести Follow-up первым, потому что «уже есть
MCP-направление». Проверено:

* `config.json` → `"mcpServers": {}` — **пусто**;
* `workspace/skills/legal_summarizer/scripts/retrieval/followup.py` — это
  **шаг retrieval внутри legal_summarizer** (first-run vs follow-up разбор
  документов), а не отдельная подсистема;
* собственного follow-up в проекте нет.

**Следствие:** брать нечего. Значит эталон надо задать самим в Фазе 2 —
иначе слабый агент напишет шесть разных серверов шестью разными способами.
Эталон создан: `servers/_template/` + контрактные тесты.

### 2. Audit уже наполовину мигрирован

ADR `docs/architecture/decisions/audit-analyzer-runtime-boundary.md` зафиксировал,
что эталон для audit — CLI-слой skill'а, а agent-tools не возвращаются.
Это ровно та граница, которую требует план. Фаза 5 сводится к обёртке
существующего `scripts/cli.py` в MCP-сервер.

## Порядок переноса (уточнённый)

Риск расположен не там, где предполагал исходный план. Сначала дёшево и
проверяемо, потом дорого.

1. **Follow-up (эталон)** → переименовать в «Audit как первый домен»;
   эталон сервера уже готов в `servers/_template/`.
2. **Document/Office** → ноль связанности, ноль бизнес-риска.
3. **Legal** → схлопнуть CLI + tool в один MCP.
4. **Memory/History** → вытащить логику из 30 KB tool-класса.
5. **Audit** → обёртка существующего CLI.
6. **Enterprise Data из `lib/`** → последним и отдельным шагом: это
   единственная фаза, трогающая фреймворк-обвязку.

## Правило, которое важнее порядка

Каждый домен переезжает **вместе со своими тестами**. В репозитории уже
4101 тест; при переносе бизнес-логики тесты обязаны переехать туда же,
иначе «зелёный» прогон будет означать «тесты не запускались».
