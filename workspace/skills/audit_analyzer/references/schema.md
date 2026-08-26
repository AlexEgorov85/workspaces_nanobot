# Audit DB schema

Подробная схема таблиц `oarb.*` (см. `sql/audit_analyzer/create_*.sql`).
Загружается по необходимости, когда агенту нужны конкретные колонки
(progressive disclosure — docs/TARGET_ARCHITECTURE.md §10).

## `oarb.audits` — аудиторские проверки

| column | type | description |
|---|---|---|
| `id` | integer | первичный ключ |
| `title` | varchar(500) | название проверки |
| `audit_type` | varchar(100) | тип проверки |
| `planned_date` | date | плановая дата |
| `actual_date` | date | фактическая дата |
| `status` | varchar(50) | статус (`planned`/`in_progress`/`completed`) |
| `auditee_entity` | varchar(500) | проверяемая организация |
| `created_at` | timestamptz | метка создания (sync) |
| `updated_at` | timestamptz | метка обновления (sync) |

Связи:

- `oarb.audit_reports.audit_id → oarb.audits.id` (1-N).
- `oarb.violations.audit_id → oarb.audits.id` (1-N).

## `oarb.audit_reports` — отчёты о проверках

| column | type | description |
|---|---|---|
| `id` | integer | первичный ключ |
| `audit_id` | integer | FK → `oarb.audits.id` |
| `report_number` | varchar(100) | номер отчёта |
| `report_date` | date | дата отчёта |
| `title` | varchar(500) | заголовок отчёта |
| `full_text` | text | полный текст отчёта |
| `created_at` | timestamptz | метка создания (sync) |
| `updated_at` | timestamptz | метка обновления (sync) |

Связи:

- `oarb.report_items.report_id → oarb.audit_reports.id` (1-N).
- `oarb.violations.report_id → oarb.audit_reports.id` (1-N, опционально).

## `oarb.report_items` — пункты отчётов

| column | type | description |
|---|---|---|
| `id` | integer | первичный ключ |
| `report_id` | integer | FK → `oarb.audit_reports.id` |
| `item_number` | varchar(20) | номер пункта |
| `item_title` | varchar(500) | заголовок пункта |
| `item_content` | text | содержимое |
| `order_index` | integer | порядок отображения |
| `created_at` | timestamptz | метка создания (sync) |
| `updated_at` | timestamptz | метка обновления (sync) |

## `oarb.violations` — нарушения

| column | type | description |
|---|---|---|
| `id` | integer | первичный ключ |
| `audit_id` | integer | FK → `oarb.audits.id` |
| `report_id` | integer | FK → `oarb.audit_reports.id` (опционально) |
| `item_id` | integer | FK → `oarb.report_items.id` (опционально) |
| `violation_code` | varchar(100) | код нарушения |
| `description` | text | описание нарушения |
| `recommendation` | text | рекомендация по устранению |
| `severity` | varchar(20) | критичность (`low`/`medium`/`high`/`critical`) |
| `status` | varchar(50) | статус (`open`/`in_progress`/`closed`) |
| `responsible` | varchar(200) | ответственный |
| `deadline` | date | срок устранения |
| `created_at` | timestamptz | метка создания (sync) |
| `updated_at` | timestamptz | метка обновления (sync) |

## Сводные JOIN-связи

```sql
-- Проверка → отчёт → пункты → нарушения
audits a
JOIN audit_reports r ON r.audit_id = a.id
JOIN report_items ri ON ri.report_id = r.id
LEFT JOIN violations v ON v.item_id = ri.id

-- Проверка → нарушения напрямую
audits a
LEFT JOIN violations v ON v.audit_id = a.id
```

## Домен-соглашения

- Все таблицы имеют `updated_at` — синхронизация идёт инкрементально по этой колонке.
- Идентификаторы — `BIGSERIAL` (но в skill описаны как `integer` для краткости).
- Текстовые поля могут содержать `NULL`.