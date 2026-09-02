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

| column | type | что хранится | используется кодом |
|---|---|---|---|
| `name` | TEXT PK | Имя скрипта (^[a-z][a-z0-9_]*$) | да — lookup |
| `description` | TEXT NOT NULL | Краткое описание (1–2 строки) | да — для LLM-промпта/few-shot |
| `sql_template` | TEXT NOT NULL | SQL с позиционными `?`-placeholder'ами (DuckDB-стиль) | да — выполняется |
| `parameters` | JSONB NOT NULL DEFAULT `'{}'` | `{name: ParamDefinition}` | да — валидация |
| `max_rows_default` | INTEGER NOT NULL | Лимит строк по умолчанию (добавляется в `LIMIT`) | да — добавляется автоматически |
| `returns` | TEXT NOT NULL DEFAULT '' | Что возвращает скрипт (для документации и LLM-промпта) | зарезервировано |
| `long_description` | TEXT NOT NULL DEFAULT '' | Подробное описание для LLM-промпта: что делает, когда использовать, edge cases | зарезервировано |
| `created_at` | TIMESTAMPTZ NOT NULL DEFAULT NOW() | Время создания записи | sync-метаданные |
| `updated_at` | TIMESTAMPTZ NOT NULL DEFAULT NOW() | Время последнего изменения | sync-метаданные (`PgDuckDbSyncService` синхронизирует инкрементально по этой колонке) |

## Контракт `sql_template` и placeholder'ов

**Только позиционные `?`-placeholder'ы (DuckDB-стиль).**

Каждый `?` соответствует параметру из JSONB `parameters` в порядке
объявления (`script.parameter_names()`).

```sql
-- Пример:
SELECT COUNT(*)
FROM oarb.audits
WHERE actual_date BETWEEN ? AND ?
  AND audit_type = ?
```

Соответствующий JSONB:

```json
{
  "date_from":  {"type": "date", "required": true},
  "date_to":    {"type": "date", "required": true},
  "audit_type": {"type": "string", "required": true, "validation": {"choices": ["Внеплановая", "Плановая"]}}
}
```

Реализация: `lib/services/predefined_script_request.py`
(`PredefinedScriptRequestBuilder`).

**Если в SQL нет явного `LIMIT` и `max_rows_default > 0`** — tool
автоматически добавляет `LIMIT ?` с дополнительным аргументом. Если
`LIMIT` уже есть (литерал или `?`) — tool не вмешивается.

**Если в SQL `?` больше или меньше, чем параметров в JSONB** —
`invalid_script` (валидация в `PredefinedScriptRequestBuilder.build`).

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
   `INSERT INTO public.agent_predefined_scripts (name, description, sql_template, parameters, max_rows_default) VALUES (...)`.
2. Дождаться `PgDuckDbSyncService` (инкрементальный sync по `updated_at`).
3. Синхронизировать каталог в `SKILL.md` — добавить имя и описание.

## Чего не делать (для Agent'а)

- Не вызывай `nl_sql_generate`, если запрос 1-в-1 ложится на известный
  скрипт — это лишний LLM-вызов.
- Не выдумывай `name` скрипта — бери только из каталога в `SKILL.md`.
- Не передавай лишних параметров в `params` — будет `invalid_script`.
- Не передавай `sql` руками — tool берёт SQL из реестра.