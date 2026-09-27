## Context

См. `proposal.md` — мотивация и scope. Дополнение: текущее состояние — `profiles/test.jsonc` переключает 6 runtime-имён, но в `sql/` нет ни DDL для таблиц `agent_*_test`, ни инструмента применения. Тесты в `tests/test_profile_lifecycle.py` работают только на уровне конфиг-резолвера (subprocess + `import config`), не трогают БД — поэтому проблема не была поймана pytest'ом. Фикс чисто инфраструктурный (DDL + runner + документация), без правок runtime-кода.

## Goals / Non-Goals

**Goals:**
- Закрыть пробел: для каждого runtime-имени из профиля `test` существует DDL и runner.
- Сохранить структурную эквивалентность test-таблиц prod-собратьям (чтобы `DbLoggingService`, `PGSessionManager`, `PostgresChannel` не зависели от профиля и не имели test-only веток).
- Обновить `configuration/profiles` спеку так, чтобы наличие test-таблиц стало обязательной частью контракта профиля.

**Non-Goals:**
- Не менять runtime-код (`lib/`, `workspace/`).
- Не создавать test-DDL в отдельной схеме.
- Не вводить новые ключи профиля (только 6 уже существующих).
- Не делать data-migration из prod в test (test-таблицы стартуют пустыми).
- Не менять prod-DDL.

## Decisions

1. **Структурное клонирование, а не template-генерация.** Шесть явных create-скриптов вместо единого шаблона с подстановкой имени.
   - *Почему:* соответствует конвенции `sql/README.md` («один файл = одна таблица»), держит `COMMENT ON` рядом с DDL, не вводит новый механизм.
   - *Альтернатива:* `tools/generate_test_ddl.py` генерирует test-скрипты из prod. Отвергнуто — добавляет лишний шаг сборки и нарушает «DDL — единая точка правды».

2. **Применение через Python-скрипт с psycopg2, а не через `tools/migrate.py`.**
   - *Почему:* Greenplum-расширение `DISTRIBUTED BY (...)` не передаётся через psycopg2 extended query protocol (`cur.execute`); стандартный механизм `tools/migrate.py` падает на этом синтаксисе. Python-скрипт сплитит файл на отдельные statement'ы по `;` и применяет каждый через `cur.execute`; `DISTRIBUTED BY` удалён из test-DDL (таблицы без явного distribution создаются на GP с дефолтным random distribution — приемлемо для test-среды).
   - *Альтернативы:* (а) `psql -f` через subprocess — требует установленного psql; (б) версионные миграции с multi-statement через libpq `mogrify` — сложно; (в) оставить `DISTRIBUTED BY` и требовать psql.
   - *Принято:* Python-скрипт (без `DISTRIBUTED BY`) **и** упоминание `psql -f` в README как альтернативный путь (для прод-deploy через psql оба варианта совместимы).

3. **`agent_gateway_logs_test.user_id` создаётся сразу как NOT-NULL-able колонка + индекс.**
   - *Почему:* `tools-history-search` требует, чтобы `user_id` существовал в `agent_gateway_logs` (security boundary для `session_scope="all"`). Если отложить — после первого запуска под профилем `test` `history_search` сломается так же, как сейчас.
   - *Альтернатива:* колонка добавляется отдельной миграцией. Отвергнуто — лишний шаг, можно сразу включить.

4. **Дефолт id в `agent_gateway_logs_test` ставится через `ALTER TABLE ... SET DEFAULT gen_random_uuid()`** (а не только в `CREATE TABLE`), потому что `CREATE TABLE IF NOT EXISTS` на уже существующей таблице не выставляет default.
   - *Почему:* идемпотентность повторного apply. ALTER не падает, если default уже установлен.

## Risks / Trade-offs

- [Drift между prod и test DDL] → ручной review при правке любой prod-таблицы. Автоматический diff-чекер (отдельный OpenSpec change) вне scope.
- [Дублирование DDL] → 6 файлов × ~30 строк ≈ 200 строк копипаста. Приемлемо для разовой фиксации; в перспективе — генератор из общего шаблона (отдельный change).
- [Без `DISTRIBUTED BY` в test-таблицах] → на GP таблицы создаются с дефолтным random distribution. Для test-среды (малый объём, без JOIN-перформанса) приемлемо. Если потребуется идентичное распределение — переписать на `psql -f` или добавить `DISTRIBUTED BY` через отдельный шаг.
- [V005 отменена] → первоначальный план с миграцией V005 отменён из-за несовместимости psycopg2 с `DISTRIBUTED BY`; вместо миграции — Python-runner.

## Migration Plan

1. На greenfield-БД: применить стандартный порядок из `sql/README.md` + `python tools/apply_test_profile_tables.py` для test-таблиц.
2. На существующей БД (с baseline V001–V004): одноразово `python tools/apply_test_profile_tables.py`. Prod-данные не затрагиваются.
3. Rollback: `DROP TABLE public.agent_*_test` руками. Prod-данные не затронуты (отдельные таблицы). Не делается автоматически.
