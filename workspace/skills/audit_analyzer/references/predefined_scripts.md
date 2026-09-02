# Predefined SQL scripts — технические детали

Это **техническая справка** о реестре `public.agent_predefined_scripts`,
схеме JSONB `parameters` и правилах валидации. Каталог для выбора
скриптов и decision tree — в `SKILL.md` (раздел «Predefined scripts»).

## Источник истины

Реестр готовых SQL-рецептов живёт в PG-таблице
`public.agent_predefined_scripts`
(DDL: `sql/audit_analyzer/create_public_agent_predefined_scripts.sql`).
Через `PgDuckDbSyncService` записи попадают в общий runtime-снапшот
DuckDB (`workspace/data_store/duckdb/cache.duckdb`), откуда их читает
tool `run_predefined_script`.

> **Синхронизация каталога и реестра — задача администратора.** Если
> скрипта из `SKILL.md` нет в реестре, он не работает; если в реестре
> есть скрипт, которого нет в `SKILL.md`, Agent о нём не знает.

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

Параметры передаются как позиционные `?`-placeholder'ы в `sql_template`
в порядке объявления (см. `ParameterValidator` в
`lib/services/predefined_script_validator.py`).

## Контракт вызова

```
run_predefined_script(name="<из SKILL.md>", params={...})
```

- **name** — PK в `public.agent_predefined_scripts` (из каталога в `SKILL.md`).
- **params** — словарь значений, валидируется по JSONB-схеме
  (`type`/`required`/`default`/`validation`). Лишние ключи → `invalid_script`.
- Если скрипт не найден → `script_not_found`.
- SQL из шаблона проходит `validate_sql` (SELECT-only gate).

## Как добавить новый скрипт (для администратора)

1. INSERT/UPDATE через PG напрямую или через SQL-миграцию:
   `INSERT INTO public.agent_predefined_scripts ...`.
2. Дождаться `PgDuckDbSyncService` (инкрементальный sync по `updated_at`).
3. Синхронизировать каталог в `SKILL.md` — добавить имя и описание.

## Чего не делать (для Agent'а)

- Не вызывай `nl_sql_generate`, если запрос 1-в-1 ложится на известный
  скрипт — это лишний LLM-вызов.
- Не выдумывай `name` скрипта — бери только из каталога в `SKILL.md`.
- Не передавай лишних параметров в `params` — будет `invalid_script`.
- Не передавай `sql` руками — tool берёт SQL из реестра.