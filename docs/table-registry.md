# Resource Model — документ снят

> **Этот документ описывает подсистему, которой в проекте нет.** Он сохранён
> как надгробие: описанная ниже модель не действует, а переход по этой ссылке
> из README раньше выглядел как инструкция к работе.

## Что здесь было

Skill объявлял свои PG-таблицы и векторные индексы в `config.json`, декларации
превращались в dataclass-ресурсы `TableResource` / `VectorResource` и
регистрировались в `lib/services/table_registry.py`. Знание о том, что грузить
в локальный DuckDB-снимок, бралось оттуда; оттуда же шёл lookup по `label` и по
track-колонке.

## Почему снято

С 2026-10-01 локальный снимок в агенте не существует. Реестр ресурсов с владельцем
не остался: снятый снимок вместе с ним унёс единственного потребителя, а
skill'ы получили доступ к данным через операции capability `audit` по MCP, а не
через собственный доступ к кэшу. Декларативная регистрация
(`lib/core/skill_registration.py`, `lib/core/infra_registration.py`,
`ApplicationContext._auto_register_skills`) исчезла вместе с реестром.

Снятые модули: `lib/services/table_registry.py`,
`lib/core/skill_registration.py`, `lib/core/infra_registration.py`,
`lib/core/skill_config.py`, `lib/services/duckdb_cache_store.py`,
`lib/services/cache_provider.py`, `lib/services/cache_load_service.py`,
`lib/services/cache_provider_impl.py`, `lib/services/text_splitter.py`,
`lib/services/vector_index_service.py`, `lib/services/preload_service.py`.

## Где всё живёт сейчас

| Что было здесь | Где теперь |
|---|---|
| Состав таблиц снимка (`tables[]`) | `mcp-platform/platform.json → audit.tables`; читает capability `data` |
| `label` / реестр предустановленных скриптов | `platform.json → audit.tables[].label`; разбирает `server.py::_audit_config` |
| Объявление векторных индексов | `platform.json → vectors.indexes`; читает capability `vectors` |
| Сборка векторов | `python -m servers.enterprise.build_index` (из `mcp-platform`) |
| Путь файла снимка | `platform.json → data.snapshot_path` |
| Доступ skill'а к данным | операции capability `audit` и `vectors` (`audit.list_scripts`, `audit.run_script`, `audit.generate_sql`, `vectors.vector_search`) через tool агента |
| Модель эмбеддинга | `platform.json → llm.embed_*`, владелец — capability `llm` |
| Track-колонка | `platform.json → vectors.indexes.<name>.track_column` |

## Актуальные документы

- [skill-tool-inventory.md](skill-tool-inventory.md) — сводка живых skill'ов и
  tool'ов, включая снятые компоненты.
- [skill-tool-architecture.md](skill-tool-architecture.md) — контракт
  Skill ↔ Tool; там же действующий контракт `label`.
- [DATABASE.md](DATABASE.md) — пул, хранение сессий, схема БД.
- [ARCHITECTURE.md](ARCHITECTURE.md) — слои и жизненный цикл.
- `mcp-platform/docs/MCP-CONTRACTS.md` — контракты операций платформы.

## Если вы ищете ответ на практический вопрос

- *«Какие таблицы грузятся в снимок?»* — `mcp-platform/platform.json → audit.tables`
  и `data`.
- *«Где взять SQL готового скрипта?»* — таблица реестра
  `public.agent_predefined_scripts`, операция `audit.list_scripts` / `audit.run_script`
  capability `audit`.
- *«Как пересобрать векторы?»* — `cd mcp-platform && python -m
  servers.enterprise.build_index --index <name>`, затем перезагрузка снимка.
- *«Куда делся мой `scripts/cli.py`?»* — skill-side CLI не существует и не
  должен появляться; это запрещено `tests/test_docs_consistency.py`.
