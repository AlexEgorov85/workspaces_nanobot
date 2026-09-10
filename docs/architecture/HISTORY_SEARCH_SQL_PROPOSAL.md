# HISTORY_SEARCH_SQL_PROPOSAL — два варианта для проверки на реальной БД

> Документ для разработчика, который будет проверять реализацию на живой
> БД (PostgreSQL 13+ или Greenplum 6.5). **history_search_tool.py НЕ
> меняется до явного одобрения** — только этот proposal и benchmark'ы
> остаются как артефакты планирования.
>
> Контекст: `docs/architecture/HISTORY_SEARCH_ANALYSIS.md` (Этапы 1, 2 —
> baseline и инвентаризация). Baseline `Recall@5=0.847`, `MRR=0.867`,
> `false_positive_rate=0.5`. Цель — улучшить без слома.

## 0. Что НЕ меняется в схеме

Этот proposal **не требует** DDL-миграций:

- Нет `CREATE INDEX` (индексы — отдельный тикет, см. Этап 15).
- Нет `CREATE EXTENSION` (`pg_trgm` отменён для GP 6.5).
- Нет изменений таблицы `public.agent_gateway_logs`.

FTS-выражения (`to_tsvector('simple', ...)`, `plainto_tsquery`, `ts_rank`)
работают в Greenplum 6.5 (PostgreSQL 9.4 ядро) без расширений.

## 1. Baseline (текущая реализация)

Источник: `workspace/tools/history_search_tool.py:281–288`.

```sql
SELECT id, "timestamp", event_type, name, level, summary, payload
FROM "<schema>"."<table>"
WHERE (<allow_all> OR session_id = <session_id>)
  AND (<event_type> IS NULL OR event_type = <event_type>)
  AND (name = <tool_name>            -- если задан
       OR true)
  AND (summary ILIKE <%like%> OR payload::text ILIKE <%like%>)
                                       -- если query задан
  AND "timestamp" >= <since>          -- если задан
  AND "timestamp" <= <until>          -- если задан
ORDER BY "timestamp" DESC
LIMIT <effective_limit>
```

### Что плохо в baseline

1. **Нет релевантности** — сортировка только по свежести. Старое идеально
   подходящее событие может оказаться ниже нового шумового.
2. **ILIKE не ловит словоформы** — `'договор'` не найдёт `'договоры'`
   (`'simple'` FTS-конфиг тоже этого не сделает, но мы хотя бы сможем
   применить нормализацию запроса).
3. **`payload::text ILIKE`** кастит весь JSONB в text для каждой строки +
   ищет подстроку. На больших payload'ах медленно, и шумово (служебные
   поля `metadata.tokens_used` и т.п. тоже матчатся).
4. **`filename_complex`** кейс (из baseline: `Recall=0` для
   `my_report_2026_final_v3.pdf`) — ILIKE по длинному имени с
   подчёркиваниями не работает.

## 2. PROPOSAL-A: FTS + ranking, минимальная правка

Один SELECT, который:

- добавляет `to_tsvector` и `ts_rank` для **summary** (короткий текст,
  всегда релевантный);
- для **payload** использует **event-specific extraction** через
  подзапросы с условным `CASE WHEN event_type = ... THEN ... END`;
- сортирует по **composite score**: `ts_rank(summary, q) * w_summary +
  ts_rank(payload_text, q) * w_payload + recency_bonus`;
- для запросов короче 4 символов — fallback на `ILIKE` (короткие слова
  плохо индексируются FTS и часто дают мусор).

### Предлагаемый SQL (псевдокод, нужно адаптировать под Python)

```sql
WITH q AS (
  SELECT plainto_tsquery('simple', <query>) AS tsq
),
scored AS (
  SELECT
    id, "timestamp", event_type, name, level, summary, payload,
    CASE
      WHEN <query> IS NULL OR length(<query>) < 4 THEN
        -- короткий запрос: только ILIKE-матч, без FTS
        CASE WHEN summary ILIKE <%like%> OR payload::text ILIKE <%like%>
             THEN 1.0 ELSE 0.0 END
      ELSE
        -- полный FTS-score с учётом event_type
        coalesce(ts_rank(to_tsvector('simple', coalesce(summary, '')), q.tsq), 0)
          * CASE
              WHEN <event_type> IS NULL THEN 0.4
              WHEN <event_type> = 'tool_call' OR <event_type> = 'tool_result' THEN 0.3
              WHEN <event_type> = 'run_finished' OR <event_type> = 'llm_call' THEN 0.5
              WHEN <event_type> = 'inbound' THEN 0.4
              ELSE 0.3
            END
        +
        coalesce(ts_rank(
          to_tsvector('simple',
            CASE <event_type>
              WHEN 'tool_call'   THEN coalesce(payload->'args'->>'path', '')
                                   || ' ' || coalesce(payload->'args'->>'query', '')
                                   || ' ' || coalesce(payload->'args'->>'doc_id', '')
                                   || ' ' || coalesce(payload->>'tool', '')
              WHEN 'tool_result' THEN coalesce(payload->'result'->>'path', '')
                                   || ' ' || coalesce(payload->'result'->>'doc_id', '')
                                   || ' ' || coalesce(payload->>'error', '')
              WHEN 'llm_call'    THEN coalesce(payload->'response'->>'content', '')
                                   || ' ' || coalesce(payload->'prompt'->1->>'content', '')
              WHEN 'inbound'     THEN coalesce(payload->>'content', '')
              WHEN 'run_finished' THEN coalesce(payload->>'final_content', '')
              ELSE ''
            END
          ), q.tsq), 0)
        * CASE
            WHEN <event_type> = 'tool_call' OR <event_type> = 'tool_result' THEN 0.6
            WHEN <event_type> = 'run_finished' OR <event_type> = 'llm_call' THEN 0.4
            WHEN <event_type> = 'inbound' THEN 0.5
            ELSE 0.3
          END
    END
    -- recency bonus: log-decay, чтобы свежесть помогала, но не доминировала
    + 0.05 * exp(-extract(epoch FROM (now() - "timestamp")) / (7 * 86400))
    AS score
  FROM "<schema>"."<table>", q
  WHERE (<allow_all> OR session_id = <session_id>)
    AND (<event_type> IS NULL OR event_type = <event_type>)
    AND (<tool_name> IS NULL OR name = <tool_name>)
    AND (<since> IS NULL OR "timestamp" >= <since>)
    AND (<until> IS NULL OR "timestamp" <= <until>)
    AND (
      <query> IS NULL
      OR length(<query>) < 4
      OR summary ILIKE <%like%>
      OR payload::text ILIKE <%like%>
      OR to_tsvector('simple', coalesce(summary, '')) @@ q.tsq
      OR to_tsvector('simple',
           CASE <event_type>
             WHEN 'tool_call'   THEN coalesce(payload->'args'->>'path', '')
                                  || ' ' || coalesce(payload->'args'->>'query', '')
                                  || ' ' || coalesce(payload->'args'->>'doc_id', '')
                                  || ' ' || coalesce(payload->>'tool', '')
             WHEN 'tool_result' THEN coalesce(payload->'result'->>'path', '')
                                  || ' ' || coalesce(payload->'result'->>'doc_id', '')
                                  || ' ' || coalesce(payload->>'error', '')
             WHEN 'llm_call'    THEN coalesce(payload->'response'->>'content', '')
                                  || ' ' || coalesce(payload->'prompt'->1->>'content', '')
             WHEN 'inbound'     THEN coalesce(payload->>'content', '')
             WHEN 'run_finished' THEN coalesce(payload->>'final_content', '')
             ELSE ''
           END) @@ q.tsq
    )
)
SELECT id, "timestamp", event_type, name, level, summary, payload
FROM scored
WHERE score > 0
ORDER BY score DESC
LIMIT <effective_limit>
```

### Что PROPOSAL-A решает

1. **Релевантность** — `ORDER BY score DESC` вместо `timestamp DESC`.
2. **FTS** для естественного текста — `to_tsvector('simple', ...)` +
   `ts_rank`.
3. **Event-specific extraction** — `CASE WHEN event_type = ...` выбирает
   релевантные поля payload вместо каста всего JSON в text.
4. **Recency bonus** — `0.05 * exp(-Δt / 7days)` — слабый сигнал, чтобы
   свежесть помогала, но не доминировала над релевантностью.
5. **Short-word fallback** — для запросов короче 4 символов (типа
   `'PDF'`, `'риск'`) — ILIKE вместо FTS, потому что FTS на коротких
   словах часто даёт мусор или 0 матчей.
6. **`filename_complex`** — если запрос содержит `_`/`-`/`.`, в Python
   нормализуем (split → несколько ILIKE-подзапросов с OR), либо
   добавляем отдельный fallback `ILIKE '%' || replace(...) || '%'`.
   Подробности — в PROPOSAL-B.

### Что PROPOSAL-A НЕ решает

- **Точные `doc_id` / `ABC-12345`** с дефисами/подчёркиваниями — `'simple'`
  FTS не делает stemming, но и не разбивает по `_`. Нужен substring
  fallback (PROPOSAL-B).
- **Стеммизация русского** — `'договор'` не найдёт `'договоры'`. Это
  осознанное ограничение 'simple'-конфига. Альтернатива: перейти на
  `'russian'` (но это создаст мусор для ID/имён файлов).

## 3. PROPOSAL-B: добавить substring-fallback для составных ID

Дополнение к PROPOSAL-A: после FTS-поиска добавляем OR-условие для
**нормализованного** запроса (split по `_`/`-`/`.`).

```sql
-- Дополнительное условие в WHERE (после FTS):
OR EXISTS (
  SELECT 1 FROM unnest(string_to_array(
    replace(replace(replace(<query>, '_', ' '), '-', ' '), '.', ' '),
    ' ')) AS part
  WHERE length(part) >= 3
    AND (
      summary ILIKE '%' || part || '%'
      OR payload::text ILIKE '%' || part || '%'
    )
)
```

Логика: запрос `my_report_2026` → split → `['my', 'report', '2026']` →
любая из частей длиной ≥3 должна матчиться в `summary` или `payload`.

Это **расширение** PROPOSAL-A, не самостоятельный вариант.

## 4. Как проверить на реальной БД

### 4.1. Подготовка

```sql
-- Проверить, что FTS работает (не требует расширений):
SELECT to_tsvector('simple', 'договор аренды офиса');
-- ожидаем: 'аренды':2 'договор':1 'офиса':3

SELECT plainto_tsquery('simple', 'договор');
-- ожидаем: 'договор'

SELECT to_tsvector('simple', 'contract_2026_04.pdf');
-- ожидаем: '2026':2 '04':3 'contract':1 'pdf':4

SELECT to_tsvector('simple', 'ABC-12345');
-- ожидаем: '12345':2 'abc':1

SELECT ts_rank(
  to_tsvector('simple', 'обработан договор ABC'),
  plainto_tsquery('simple', 'договор')
);
-- ожидаем: ~0.0608 (ненулевой float)
```

Если все эти запросы возвращают ожидаемое — FTS-инфраструктура
доступна. Если нет — на этой БД переход на FTS невозможен.

### 4.2. EXPLAIN ANALYZE на реальных объёмах

```sql
-- Подставить реальные имена schema/table и параметры из текущего tool'а.
-- Замерить время baseline-запроса на 10k+ строках:
\timing on
EXPLAIN ANALYZE
SELECT id, "timestamp", event_type, name, level, summary, payload
FROM public.agent_gateway_logs
WHERE summary ILIKE '%договор%' OR payload::text ILIKE '%договор%'
ORDER BY "timestamp" DESC LIMIT 50;
-- Записать время → "baseline latency".

EXPLAIN ANALYZE
-- PROPOSAL-A с тем же запросом
WITH q AS (SELECT plainto_tsquery('simple', 'договор') AS tsq)
SELECT id, "timestamp", event_type, name, level, summary, payload,
  -- полная score-формула (см. §2)
  ...
FROM public.agent_gateway_logs, q
WHERE ...
ORDER BY score DESC LIMIT 50;
-- Записать время → "fts latency".
```

Если `fts latency` значительно (×3+) хуже `baseline latency` — на
этой БД FTS без GIN-индекса не даёт выигрыша; нужно создавать индекс
(отдельная миграция, Этап 15) или переходить на substring-fallback
(PROPOSAL-B) для всех запросов.

### 4.3. Проверка качества поиска (метрики)

Снять `pg_dump` или anonymized sample таблицы (без user-content) →
загрузить в `tests/fixtures/history_search/` →
запустить `pytest -m benchmark` → сравнить baseline vs FTS-эмулятор.

Эмулятор FTS в `tests/test_history_search_benchmark.py` (в режиме
`OPTIMIZED`) уже реализует PROPOSAL-A+B на Python — он даёт
**верхнюю оценку** ожидаемого прироста (потому что PG FTS может
быть менее точным, чем эмулятор, из-за лексических особенностей).

### 4.4. Решение

| Сценарий | Решение |
|---|---|
| `fts latency ≈ baseline latency`, метрики ≥ baseline | Принять PROPOSAL-A, мержить в `history_search_tool.py` |
| `fts latency ≫ baseline latency`, метрики ≥ baseline | Принять PROPOSAL-A, но запланировать GIN-индекс (отдельная миграция) |
| `fts latency ≫ baseline latency`, метрики ≈ baseline | Отклонить PROPOSAL-A, остановиться на baseline, зафиксировать результат |
| `fts вообще не работает` (запросы §4.1 возвращают ошибку) | На этой БД FTS недоступен. Отклонить PROPOSAL-A, не пробовать vector search, зафиксировать в CHANGELOG |
| Метрики PROPOSAL-A **ниже** baseline | Не должно быть (по design), но если есть — отклонить, искать причину |

## 5. Что нужно для merge в `history_search_tool.py`

После того как PROPOSAL-A одобрен:

1. **Сформировать финальный SQL** с правильным placeholder'ом
   (`%s`-параметры, без интерполяции — `sql_safety` политика).
2. **Заменить текущий SQL** в `workspace/tools/history_search_tool.py`.
3. **Расширить benchmark-эмулятор** до двух режимов (`BASELINE` /
   `OPTIMIZED`) — уже частично сделано.
4. **Добавить тесты** в `tests/test_history_search_tool.py`:
   - existing (regression): все 9 текущих тестов должны проходить;
   - ranking: «старое релевантное > новое шумовое»;
   - fuzzy: запрос `'договор'` находит событие с `'договоры'`
     (через ILIKE-fallback для коротких слов);
   - size-limits: тесты на `max_rows` / `max_result_chars` /
     `truncated=true` — уже есть, но нужно проверить, что ranking
     не ломает инвариант «после truncation JSON валиден».
5. **Прогнать benchmark**: `pytest -m benchmark` — метрики должны быть
   ≥ baseline (Recall@5 ≥ 0.847, MRR ≥ 0.867).
6. **Прогнать все unit-тесты**: `pytest tests/test_history_search_tool.py
   tests/test_context_compaction.py tests/test_db_logging_service.py -q`
   — регрессий быть не должно.

## 6. Альтернатива, если PROPOSAL-A провалится

Если на реальной БД FTS не даёт выигрыша или не работает:

1. **PROPOSAL-C: только event-specific extraction + ILIKE + ranking**
   — без FTS, только Python-нормализация запроса + условный `CASE
   WHEN event_type = ... THEN ... ELSE payload::text END` в SQL +
   ranking по количеству совпавших полей. Это улучшает `filename_complex`
   (где ILIKE сам по себе работает, если запрос нормализован) и
   уменьшает шум для `ambiguous`.
2. **Оставить baseline** — зафиксировать результат, перейти к Этапу 17
   (решение по vector search). Если и lexical+extraction не помогают —
   есть аргумент для semantic retrieval. Если помогают — vector search
   не нужен.

## 7. Связанные документы

- [`HISTORY_SEARCH_ANALYSIS.md`](HISTORY_SEARCH_ANALYSIS.md) — Этапы 1, 2
  (инвентаризация + baseline benchmark).
- `tests/fixtures/history_search/` — синтетический набор для benchmark'а.
- `tests/test_history_search_benchmark.py` — benchmark (BASELINE +
  OPTIMIZED режимы).
- `tests/test_history_search_tool.py` — unit-тесты tool'а (9 тестов).
- `tests/test_context_compaction.py` — тесты compaction (включая
  `TestNotifyRecordsEventLog`, который закрывает gap №1).
