---
name: audit_analyzer
description: Анализ аудиторских проверок — SQL-отчёты, векторный поиск, генерация SQL через LLM.
metadata: {"nanobot":{"emoji":"📊","always":true}}
---

# Audit Analyzer

Три режима анализа аудиторских проверок.

## Режимы

| Режим | Когда использовать |
|:------|:-------------------|
| `predefined` | Стандартные отчёты по имени скрипта + параметры |
| `vector` | Семантический поиск по FAISS-индексу |
| `sql` | Сложные нестандартные запросы — LLM генерирует SELECT |

## Реестр скриптов (predefined)

### `analytics_by_year_month`
- **Описание:** Аналитика проверок по годам и месяцам
- **Параметры:** `year` (number) — год проверки

### `violations_by_type`
- **Описание:** Статистика нарушений по кодам
- **Параметры:** `date_from` (date), `violation_code` (like)

### `top_audited_objects`
- **Описание:** Топ проверяемых объектов
- **Параметры:** `auditee_entity` (like), `date_from` (date), `limit` (number)

### `audit_effectiveness`
- **Описание:** Оценка эффективности проверок
- **Параметры:** `date_from` (date), `date_to` (date), `min_violations` (number)

### `audit_dynamics`
- **Описание:** Динамика проверок по периодам
- **Параметры:** `period` (month/quarter/week), `date_from` (date)

### `audit_types_stats`
- **Описание:** Статистика по типам проверок
- **Параметры:** `audit_type` (like), `date_from` (date)

## Режим sql — генерация SQL через LLM

```
Запрос → Схема БД → LLM генерирует SELECT
  → validate_sql → EXPLAIN (FORMAT JSON) — проверка
  → ошибка → retry до 3 раз → query_sql
```

## Векторные индексы (vector)

Семантический поиск по FAISS-индексу через Ollama embeddings.

**Параметры CLI:**
- `--index-name` — имя индекса: `audits_index`, `violations_index`
- `--top-k N` — ровно N лучших результатов (по умолч. 5)
- `--threshold X` — все результаты выше порога X (0.0–1.0), `--top-k` игнорируется

---

## CLI (standalone)

Запуск через `audit_analyze.bat` (Windows) или `audit_analyze.sh` (Linux):

```bash
# Windows (PowerShell / cmd) — key=value без кавычек:
audit_analyze.bat --mode predefined --script analytics_by_year_month --params year=2024
audit_analyze.bat --mode predefined --script violations_by_type --params violation_code=финансовые
audit_analyze.bat --mode sql --query "топ-10 объектов по нарушениям"

# Векторный поиск: top-3 результата
audit_analyze.bat --mode vector --query "финансовые нарушения" --index-name violations_index --top-k 3

# Векторный поиск: всё выше порога 0.5
audit_analyze.bat --mode vector --query "статусы аудитов" --index-name audits_index --threshold 0.5

# Linux:
audit_analyzer.sh --mode predefined --script analytics_by_year_month --params '{"year": 2024}'
audit_analyzer.sh --mode sql --query "топ-10 объектов по нарушениям"
audit_analyzer.sh --mode vector --query "финансовые нарушения" --index-name violations_index --top-k 3
audit_analyzer.sh --mode vector --query "статусы аудитов" --index-name audits_index --threshold 0.5
```

Параметры:

| Аргумент          | Обязательный | Описание |
|:------------------|:------------:|:---------|
| `--mode`          | да | Режим: `predefined`, `sql`, `vector` |
| `--script`        | для `predefined` | Имя скрипта из реестра |
| `--params`        | нет | Параметры: `year=2024` (key=value) или `'{"year":2024}'` (JSON, Linux) |
| `--query`         | для `sql`/`vector` | Запрос на естественном языке |
| `--index-name`    | для `vector` | Имя FAISS-индекса (без .faiss) |
| `--top-k`         | нет | Количество результатов (по умолч. 5). Для `vector` |
| `--threshold`     | нет | Порог схожести 0.0–1.0. Если задан — все результаты выше порога, `--top-k` игнорируется |
| `--vector-index`  | нет | Директория с индексами (переопределяет config.json) |
| `--context`       | нет | Контекст чата в формате JSON |
