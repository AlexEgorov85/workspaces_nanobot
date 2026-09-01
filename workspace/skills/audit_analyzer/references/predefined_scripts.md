# Predefined SQL scripts

Реестр готовых SQL-рецептов живёт в PG-таблице
`public.agent_predefined_scripts`
(DDL: `sql/audit_analyzer/create_public_agent_predefined_scripts.sql`).
Через `PgDuckDbSyncService` записи попадают в общий runtime-снапшот
DuckDB (`workspace/data_store/duckdb/cache.duckdb`), откуда их читает
tool `run_predefined_script`.

> Этот файл — **навигация**. Источник истины — таблица в БД. Перед тем
> как звать `run_predefined_script(name="<редкое имя>")`, проверь реальный
> реестр через `duckdb_query(sql="SELECT name, description, parameters FROM public.agent_predefined_scripts ORDER BY name")`.

## Колонки реестра

| column | type | что хранится |
|---|---|---|
| `name` | TEXT PK | Имя скрипта (используется в tool) |
| `description` | TEXT | Краткое описание (1–2 строки) |
| `sql_template` | TEXT | SQL с `?`-placeholder'ами (DuckDB-стиль) |
| `parameters` | JSONB | `{name: {type, required, default?, validation?}}` |
| `max_rows_default` | INTEGER | Дефолтный LIMIT |
| `returns` | TEXT | Что возвращает (для LLM-промпта) |
| `long_description` | TEXT | Подробное описание |

## Схема `parameters` (JSONB)

```json
{
  "date_from": {
    "type": "date",
    "required": true,
    "description": "Начало периода",
    "validation": { "pattern": "^\\d{4}-\\d{2}-\\d{2}$" }
  },
  "year": {
    "type": "integer",
    "default": 2024,
    "validation": { "min": 2000, "max": 2100 }
  }
}
```

Поддерживаемые `type`: `string`, `integer`, `number`, `boolean`,
`date`, `datetime`. Поддерживаемые `validation`: `min`, `max`,
`min_length`, `max_length`, `pattern`, `choices`.

## Когда звать `run_predefined_script`

Если запрос пользователя точно соответствует одному из скриптов реестра —
зови `run_predefined_script` (детерминированно, без LLM). Подробный каталог
и decision tree — в `SKILL.md` (раздел «Predefined scripts»).

## Как добавить новый скрипт

1. **Не** редактируй этот файл как «истину» — он just-навигация.
2. Реальный источник — таблица. INSERT/UPDATE делается через PG
   напрямую (или через `INSERT INTO public.agent_predefined_scripts ...`
   в SQL-миграции).
3. После добавления — дождись `PgDuckDbSyncService` (инкрементальный sync
   по `updated_at`) и проверь, что запись появилась в DuckDB:
   `duckdb_query(sql="SELECT name FROM public.agent_predefined_scripts")`.

## Чего не делать

- Не вызывай `nl_sql_generate`, если запрос 1-в-1 ложится на известный
  скрипт — это лишний LLM-вызов.
- Не выдумывай `name` скрипта, если его нет в реестре — будет
  `script_not_found`.
- Не передавай лишних параметров в `params` — будет
  `invalid_script` с указанием неизвестного ключа.
- Не передавай `sql` руками — tool берёт SQL из реестра и валидирует
  через `validate_sql`.