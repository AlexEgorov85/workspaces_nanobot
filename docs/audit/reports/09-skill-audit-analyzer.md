# Аудит: 09 — skill `audit_analyzer` (CLI-слой)

## Сводка группы
Файлов: 12 · LOC: 2035 · классов: 10 · методов: 12 · функций: 45 (всего символов разобрано: **57**)

Подсистема: `workspace/skills/audit_analyzer/scripts/**` — единственная разрешённая точка доступа агента к данным `audit_analyzer` (ADR `docs/architecture/decisions/audit-analyzer-runtime-boundary.md`). Agent-tools (`duckdb_query`, `vector_search`, `audit_analyzer_tool.py`) не возвращаются — это подтверждено и кодом, и тестом `workspace/skills/audit_analyzer/tests/test_audit_analyzer_behavior.py:446-453`.

**Ключевые находки**

- `SKILL.md:104-121` — каталог скриптов в agent-инструкции не соответствует реалю: 4 из 6 описанных имён (`analytics_by_year_month`, `audit_dynamics`, `audit_effectiveness`, `top_audited_objects`) **не существуют** в `sql/audit_analyzer/seed_predefined_scripts.sql` (там 5: `audit_status_summary`, `top_violations_by_type`, `violations_by_period`, `audits_by_period`, `audit_effectiveness_summary`); 4 из 5 реальных не описаны. Плюс ложное «все параметры optional» — у `violations_by_period`/`audits_by_period` `date_from`/`date_to` имеют `"required": true` (`seed_predefined_scripts.sql:68,82`). Агент получит «Скрипт 'X' не найден» на примерах из SKILL.md. **Упростить** (переписать раздел по фактическому seed).
- `SKILL.md:194,198`, `docs/INTERNAL_API.md:312,325,328,331,334`, `scripts/generated_sql_mode.py:18-19` — инструкция предписывает команду `audit_analyze --mode …`, которой **нет**: обёртка `workspace/skills/audit_analyzer/audit_analyze.{bat,sh}` удалена (glob по `*.{bat,sh,ps1}` в каталоге скилла — 0 файлов), при этом `docs/skill-tool-inventory.md:40` и `:36-39` перечисляют её (и три удалённых tool'а) как живые компоненты. Реальная команда — `python workspace/skills/audit_analyzer/scripts/cli.py --mode …` (`SKILL.md:351`). **Упростить** (docs) — продуктовый код не трогаем.
- `cli.py:493-503` и `cli.py:520-531` — **два мёртвых блока обработки целостности индекса**: `IndexIntegrityError` не поднимается ни в одной строке репозитория (подтверждаю находку `02-services-cache`), а `SearchResult.signature_status` не заполняется провайдером (`lib/services/duckdb_cache_store.py:1322+` создаёт `SearchResult` без этих полей; default `""`). Комментарий `cli.py:520-523` утверждает обратное. **Подтверждено** — инвариант целостности FAISS-индекса на agent-пути не проверяется.
- `output.py:79-81` — усиление предыдущей находки: даже если бы `signature_status` заполнялся, `prepare_output` копирует из `data` только `results`/`count`, поэтому ключ `index_warning` (`cli.py:527`) **отбрасывается** и до агента не доходит. Warning doubly dead.
- `cli.py:361-375` — единственная реально работающая проверка «декларация vs факт» (`--list-indexes`) выводит `signature_status: CURRENT` **только по совпадению имени** с `project.json::gateway.vector.index.indexes`; `dimension`/`updated_at`/`metric`/`signature_short` всегда `None`. Смена embedding-модели или размерности (ровно случай STALE из `openspec/specs/data/cache-provider/spec.md:143-148`) не детектится. **Упростить** (убрать всегда-None поля) + **осознанный пробел** в проверке.
- `cli.py:576-581` — `except argparse.ArgumentTypeError` **недостижим**: argparse конвертирует `ArgumentTypeError` из `type=`-callable в `parser.error()` → `SystemExit(2)` (проверено репро: `SystemExit code = 2`, usage в stderr, stdout пуст). Задокументированный контракт «кривой `--params` → JSON + exit 2» не работает. **Упростить** (удалить ветку либо документировать реальное поведение).
- `generated_sql_mode.py:128-132` — мёртвая ветка в `sanitize_sql_response`: комментарий «вернём пустую строку» (128), но обе ветки (130 и 132) возвращают `cleaned.strip().rstrip(";")`. Последствие: прозаический ответ модели уходит в `validate_sql` (239), отклоняется и **сжигает все 4 попытки** вместо раннего `no_match`. **Упростить** (удалить `if`).
- `generated_sql_mode.py:193-213` + `llm.py:45` — вложенные ретраи: `MAX_ATTEMPTS=4` (строка 37) × `max_retries=3` (`llm.py:45`) = **до 16 HTTP-вызовов LLM на одну инвокацию CLI**; при падении самого LLM-вызова в следующую попытку уходит assistant-сообщение с пустым `content` (`generated_sql_mode.py:197` + `sql=""` на 212) — часть OpenAI-совместимых провайдеров такой payload отвергает. Финальный текст (270-273) при этом говорит «не удалось сгенерировать корректный SQL», хотя проблема была в связности. **Упростить** (явные параметры, один уровень ретраев).
- `generated_sql_mode.py:248,254` — детект занятого кэша по русской подстроке `"временно занята"` в тексте ошибки DuckDB. Локале-зависимо: на англоязычном DuckDB сообщение вида `Could not set lock on file …` не срабатывает, и цикл вместо раннего `break` делает 4 попытки. Единственные 2 вхождения этой строки во всём репозитории — здесь. **Упростить** (тайминг/тип ошибки вместо match по строке).
- `skill_config.py:23` — `_PROJECT_ROOT = _SKILL_ROOT.parents[1]` резолвится в `<repo>/workspace`, а не в корень репозитория (`cli.py:51` использует `parents[4]` → `<repo>`). `from lib.core import skill_config` (строка 34) работает только потому, что `cli.py` (или `pythonpath=["."]` в pytest) уже положил настоящий корень в `sys.path`. Инсерт лишний и вводит в заблуждение. **Упростить** (удалить, оставить единственный bootstrap в `cli.py`).
- `skill_config.py:65-66` — `get_max_retries()` мёртвая: 0 вызовов в скилле и 0 в тестах (`llm.py:45` читает `cli["max_retries"]` инлайн). Дубль `lib.core.skill_config.get_max_retries` уже отмечен в `01-core-entrypoints.md:503`. **Удалить** (убрать из `__all__:40`).
- `predefined/mode.py:180` — `result.get("status")` вызывается **без** isinstance-guard, хотя строкой выше (179) guard есть: не-dict ответ провайдера → `AttributeError` вместо диагностичного ответа. Плюс `except Exception` (`mode.py:165,173`) теряет тип ошибки: `ReadOnlyAssertionError`/`UnsupportedSqlError`/`CacheBusyError` схлопываются в текст. **Упростить**.
- `predefined/db_loader.py:117-118,126-127,144-145,153-154` — четыре «тихих» подавления: недоступный кэш (провайдер вернул `{"status":"error"}` — `duckdb_cache_store.py:1092-1094`) или дрейф схемы реестра (KeyError в `_row_to_script`) выглядят как «0 скриптов» / «скрипт не найден. Доступны: » без лога. Это ровно тот process-boundary случай, о котором предупреждает `lib/core/skill_config.py:293-295`. **Упростить** (лог в stderr + различение «пусто» и «ошибка»).
- `predefined/builder.py:32` и `:66` — два присваивания `__all__` в одном модуле; второе (66) молча выкидывает `_param_usage_count` из экспорта, хотя первое объявляло его намеренно. **Упростить** (удалить 66).
- `predefined/validator.py:203-212` + `49-60` — мёртвый типизированный путь ошибки: `validate_or_raise` не вызывается **нигде** в репозитории (0 prod, 0 тест), а `ValidationError` бросается только им. Второй строкой — контракт `mode.run` использует tuple-форму (`validate` → `(merged, error)`). `ValidationError.__init__` отмечен как клон в `duplicates.md:26`. **Удалить** оба.
- `predefined/mode.py:72-83` — `list_scripts()` мёртвая: единственные ссылки — собственное определение, `__all__` (45) и ре-экспорт в `predefined/__init__.py`; `cli._list_scripts` (`cli.py:258-305`) делает ту же работу с более богатым payload. **Удалить**.
- Строки, которые лгут (протокол §5): `predefined/models.py:3-9` («реестр хранится в этом skill'е, а не в PostgreSQL» — прямо противоречит DB-first и собственному `db_loader.py:1-9`); `scripts/__init__.py:4-5` («три режима (predefined / **sql** / vector)» — режима `sql` нет); `output.py:30` («Для predefined и **sql**»); `cli.py:220-230` (`--top-k` help «(default: 5)» при `default=None`; `--threshold` help «0.0–1.0» без единой проверки диапазона); `cli.py:259-265` (`_list_scripts` обещает `returns`/`sql_template`/`parameters.default`, которых в payload нет); `cli.py:318` («Сравнить их двух»); `predefined/validator.py:9-11` (сигнатура `validate` описана как возвращающая тройку, фактически возвращает `(dict, str|None)`); `predefined/mode.py:133` (заявляет, что predefined-SQL защищён через `validate_sql` — **в модуле нет ни одного вызова `validate_sql`**); `SKILL.md:225` («LIMIT добавляется автоматически» — в `generated_sql_mode.run` LIMIT не инъецируется никогда); `SKILL.md:297` («формат вывода `{mode, status, data}` одинаков для всех трёх режимов» — `main()` разворачивает конверт через `prepare_output`, конверт `data` есть только у `--list-*`); `SKILL.md:308-318` («конкуренции за файл нет, работающий gateway не мешает») — противоречит семантике `CacheBusyError` в `lib/core/skill_config.py:293-295` и комментарию `cli.py:247-251`, где зафиксировано наблюдённое поведение process-exclusive.
- Конфигурация: секции `skills.audit_analyzer.cli` в `project.json` нет, поэтому `get_cli_config()` всегда возвращает дефолты (`lib/core/skill_config.py:121-126`), а `default_format` не потребляется **никем** (в CLI нет флага `--format`). **Упростить** (удалить мёртвый ключ из `get_cli_config` — вне моей подсистемы).
- `cli.py:537` — `_run` открывает кэш **до** валидации аргументов, поэтому `--mode predefined` без `--script` при занятом файле падает с infra-ошибкой (`CacheBusyError` → exit 1) вместо понятного «укажите --script» (`cli.py:400-406`). **Упростить** (валидировать args → потом открывать кэш).
- `workspace/skills/audit_analyzer/err1.log` — оставленный артефакт запуска в каталоге скилла; содержит вывод **устаревшей** версии CLI (`[DB] DuckDB cache (...)`, строка не встречается в текущем коде — теперь `[DB] cache provider ready (...)`, `cli.py:254`). **Удалить**.

**Вердикты:** Оставить 39 · Упростить 9 · Удалить 5 · Слить с <файл> 4 · НЕ РАЗОБРАНО 0 (счёт по строкам таблиц символов; 12 файлов, 57 символов)

---

## `workspace/skills/audit_analyzer/scripts/cli.py` — 604 LOC

**Назначение.** Единственная точка входа навыка: argparse → маршрутизация по трём режимам → JSON в stdout.
**Что делает.** Модулевым кодом (`cli.py:50-54`) вставляет корень репозитория и каталог `scripts/` в `sys.path` (из-за этого sibling-модули импортируются как `from output import …` — это **не** дубль `lib/`, а требование запуска как `python scripts/cli.py`). `_ensure_registered()` (148) инициализирует `SETTINGS` профилем `test`, если entrypoint этого ещё не сделал, и регистрирует skill + vector-хранилище в `TableRegistry`; сбой регистрации — только WARN в stderr (159-160). Побочные эффекты: запись в stderr (`[DB] …`, `[registration] WARN: …`), глобальный `sys.path`, `_initialize_settings(profile="test")` (lifecycle-gate всего процесса), чтение PG-конфига индексов.
**Зачем нужен.** Эталон CLI-слоя по ADR: агент, бенчмарки и оператор ходят только сюда; agent-tools удалены намеренно.
**Вердикт.** Упростить
**Обоснование.** Скелет и маршрутизация нужны, но 4 из 12 функций содержат мёртвые ветки (недостижимый `except`, неподнимаемое исключение, недостижимый warning-блок), третий флаг-фильтр маскируется порядком вызовов, а `--top-k`/`--threshold` не валидируются вопреки собственному help.
**Доказательства.** Точка входа `python scripts/cli.py` и `main()` (603-604). Статических импортёров 0 — по протоколу §1 это ожидаемо для entry point. Покрыт `tests/test_audit_analyzer_cli.py`, `tests/test_audit_analyzer_generated_sql.py`, `tests/test_audit_analyzer_mode_selection.py`; в репозитории инвокация встречается только в документации (`SKILL.md:351-357`, `docs/INTERNAL_API.md:290-302`) — прямых prod-вызовов (из `benchmarks/`, `tools/`, кода) нет, единственный «клиент» — агент через `exec`.

| Функция | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_resolve_known_index` | 70-105 | Проверка имени индекса по runtime-реестру (PG) | Fail-closed защита от silent fail: без неё неизвестный индекс вернул бы `[]` как `success` («Документы не найдены») | `cli.py:469` | Оставить |
| `_parse_params` | 108-125 | `--params`: JSON либо `key=value,…` | Единственный парсер параметров скрипта; поднимает `ArgumentTypeError` на битом JSON | argparse `type=` (`cli.py:209`) | Оставить |
| `_ensure_registered` | 128-160 | Инициализация `SETTINGS` (профиль `test`) + регистрация skill/vector-storage | Standalone-CLI обязан сам поднять `TableRegistry`, иначе `get_predefined_scripts_table()` и `search_vector` не найдут цель | `main():563` | Оставить (хардкод `profile="test"` — см. ниже) |
| `_build_parser` | 163-237 | Объявление 9 флагов | Контракт CLI для агента; `choices=MODES` — единственный источник истины о режимах | `main():564` | Оставить |
| `_open_db` | 240-255 | `build_cache_provider()` + строка в stderr | Единственная точка получения кэша в скилле; намеренно без собственной диагностики ошибок (см. комментарий 247-251) | `_run():537` | Оставить |
| `_list_scripts` | 258-305 | Каталог скриптов для discovery без чтения SKILL.md | Единственный машинно-читаемый список скриптов; единственный выход, который **не** проходит `prepare_output` | `_run():540`, `main():571` | Оставить (docstring 259-265 обещает поля, которых нет) |
| `_list_indexes` | 308-388 | Каталог FAISS-индексов из `storage_table` + `CURRENT`/`ORPHAN` | Единственная рабочая проверка «декларация vs факт» в скилле | `_run():542` | Упростить |
| `_run_predefined` | 391-423 | Резолв таблицы реестра + `predefined.run()` | Адаптер cli → skill-side API; даёт понятную ошибку, если skill не зарегистрирован | `_run():544` | Оставить |
| `_run_generated_sql` | 426-440 | Валидация `--query` + ленивый импорт `generated_sql_mode` | Ленивый импорт оправдан: `--help` и `--mode predefined` не должны тянуть LLM-зависимости (435-437) | `_run():546` | Оставить |
| `_run_vector` | 443-532 | Валидация индекса + `search_vector` + группировка | Прямой вызов `CacheProvider` (не tool) — по ADR skill не зависит от tool-слоя | `_run():548` | Упростить |
| `_run` | 535-557 | Маршрутизация + `finally: db.close()` | Единая точка закрытия провайдера; единственный вызов всех режимов | `main():567` | Упростить |
| `main` | 560-600 | argparse → `_run` → JSON + exit code | Контракт процесса для агента: JSON в stdout, exit 0/1/2 | `if __name__ == "__main__":604`; `tests/test_audit_analyzer_cli.py` | Упростить |

**Замечания к символам `_list_indexes` / `_run_vector` / `_run` / `main`:**

- `_list_indexes`: ключи `metric` (371) и `signature_short` (373) **всегда** `None` (`list_runtime_vector_indexes` их не отдаёт), а `dimension`/`updated_at` (370, 374) — `None` по построению; docstring 321-329 перечисляет лишь часть полей и содержит оборванную фразу на 318. `signature_status` (372) вычисляется **только** по вхождению имени в `declared` (366) — это проверка «объявлен/не объявлен», а не проверка целостности сборки. Правка: убрать всегда-None поля, назвать вычисление `declared_match` (или взять реальный статус из провайдера, если интеграция появится).
- `_run_vector`: блок `except IndexIntegrityError` (493-503) недостижим — `raise IndexIntegrityError` в репозитории 0 (см. кросс-подсистемные находки); блок `index_warning` (520-531) недостижим, т.к. `signature_status` пуст, и вдобавок теряется в `output.prepare_output`. Плюс: дефолт `top_k or 5` (490) и `audits_index` (468) продублированы в конфиге, а диапазоны, которые обещает help (220-230), не проверяются ни здесь, ни в `DuckDbCacheStore.search_vector` (проверено: `lib/services/duckdb_cache_store.py:1322+` только `min(top_k, ntotal)`).
- `_run`: `_open_db()` вызывается до `try` и до проверки аргументов (537) → ошибка инфраструктуры маскирует ошибку аргумента; ветка «Неизвестный режим» (551-554) мертва (`choices=MODES`); `hasattr(db, "close")` (556) — утиный тайпинг там, где интерфейс уже гарантирует `close()`.
- `main`: `except argparse.ArgumentTypeError` (576-581) недостижим (репро: argparse сам печатает usage и делает `SystemExit(2)`); `except FileNotFoundError` (582-587) не имеет ни одной точки выброса в пути вызова (`config.py` его не бросает); ветка `except Exception` (590-600) кладёт **полный traceback** (596) в agent-visible JSON — утечка внутренних путей и шум; обход `prepare_output` для `--list-*` (571-572) — неявный контракт, не отражённый ни в docstring, ни в SKILL.md.

**Отдельно про `_ensure_registered` и профиль.** `profile="test"` (148) захардкожен. Проверено: `profiles/test.jsonc` переопределяет **только** имена таблиц канала и логирования, не трогая `skills.audit_analyzer.*`, `gateway.cache.*` и LLM-конфиг, поэтому сегодня вреда нет. Но в prod-развёртывании процесс навыка объявляет себя тестовым профилем — ловушка на будущее: любой profile-специфичный ключ (`gateway.cache.local_path`, LLM-модель) разъедется с рантаймом. Правка: `profile=os.environ.get("NANOBOT_PROFILE", "test")` либо явный флаг CLI.

---

## `workspace/skills/audit_analyzer/scripts/generated_sql_mode.py` — 276 LOC

**Назначение.** Режим NL→SQL: LLM пишет SELECT по схеме, скилл валидирует и исполняет его.
**Что делает.** Берёт whitelist таблиц и схему из `skill_config` + `db.get_schema` (157-160), форматирует её `format_schema` (159), добавляет в system prompt 1-2 few-shot примера, подобранных по keyword-overlap из **DB-реестра** (`_select_few_shot`, 163-189). Затем до `MAX_ATTEMPTS` (37) повторяет: `chat()` → `sanitize_sql_response` → `is_no_match` → **`validate_sql` (239)** → `db.explain` (245) → `db.query_sql` (253). Побочные эффекты: сетевые вызовы LLM, `print` внутри `run` (ошибка EXPLAIN печатается в stderr, ~248-250), кэш-снимок провайдера.
**Зачем нужен.** Единственный режим, где SQL пишет модель, — здесь и находится security-граница `validate_sql`.
**Вердикт.** Оставить
**Обоснование.** Граница безопасности выстроена правильно и дублируется двумя независимыми уровнями (см. раздел «Безопасность»). Упрощения касаются формы, а не сути.
**Доказательства.** Импортируется только `cli.py:438` (лениво). Покрыт `workspace/skills/audit_analyzer/tests/test_audit_analyzer_edge_cases.py` и `tests/test_audit_analyzer_generated_sql.py`.

### Безопасность (ключевой вопрос аудита)

| Уровень | Где | Статус |
|---|---|---|
| `validate_sql` (AST-политика sqlglot) | `generated_sql_mode.py:239` | ✅ вызывается **до** `explain` и `query_sql` |
| Текстовый guard провайдера | `duckdb_cache_store.py:1087-1124` | ✅ DDL — всегда, DML — при `READ_ONLY` |
| Физический read-only | `lib/core/skill_config.py:300` → `open_cache_provider(mode=READ_ONLY)` | ✅ |
| Путь записи | — | ✅ путей нет: `upsert_records`/`replace_records`/`ensure_schema`/`INSERT`/`UPDATE`/`DELETE` в `scripts/**` — 0 совпадений; `import duckdb`/`import faiss` — 0 (инвариант ADR цел) |
| `cache_provider` как единственная точка | `cli.py:253` → `skill_config.build_cache_provider` | ✅ обхода интерфейса нет |

**Но:** режим `predefined` через `validate_sql` **не проходит** — вызова нет ни в `predefined/mode.py`, ни в `predefined/builder.py`. Там защита = текстовый guard провайдера + read-only соединение. Docstring `predefined/mode.py:133`, заявляющий «SQL-безопасность контролируется на стороне runtime через `validate_sql`», неточен: политика `validate_sql` (запрет опасных функций, системных каталогов, multi-statement) к готовым скриптам не применяется. Риск низкий (SQL авторится в репозитории seed-файлом), но заявленный инвариант не соответствует коду.

| Функция | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_normalize` | 40-45 | Токенизация под keyword-overlap | Отсекает короткие служебные слова, чтобы скоринг few-shot не шумил | `_select_few_shot:60` | Оставить |
| `_select_few_shot` | 48-78 | Top-N скриптов из реестра по пересечению токенов | Даёт модели рабочие примеры **без** копирования SQL в скилл (реестр — единственный источник) | `run:165` (внутри построения prompt) | Оставить |
| `is_no_match` | 84-97 | Детект явного `<NO_MATCH>` от модели | Различает «нет данных» и «модель не смогла» — без silent data swap | `run:222`; тесты `test_audit_analyzer_edge_cases.py` | Оставить |
| `sanitize_sql_response` | 100-132 | Чистка CoT/markdown-обёрток LLM | Убирает `<think>…</think>` и ```-блоки, чтобы `validate_sql` видел чистый SELECT | `run:215`; тесты | Упростить (мёртвая ветка 128-130) |
| `run` | 135-276 | Оркестрация LLM→SQL→DuckDB с ретраями | Единственная реализация режима | `cli.py:440` | Оставить (упростить ретраи/тексты ошибок) |

---

## `workspace/skills/audit_analyzer/scripts/skill_config.py` — 82 LOC

**Назначение.** Тонкая обёртка над `lib.core.skill_config` с фиксированным `_SKILL_NAME = "audit_analyzer"`.
**Что делает.** Модульным кодом (22-34) добавляет вычисленный `_PROJECT_ROOT` в `sys.path` и импортирует `lib.core.skill_config as _lib`; далее семь функций-однострочных делегатов. Побочный эффект — глобальная мутация `sys.path` при импорте.
**Зачем нужен.** Конфиг скилла читается только через общий слой (`AGENTS.md`: «Единая точка для всех skill'ов»), а скилл не должен знать, где лежит `project.json`.
**Вердикт.** Упростить
**Обоснование.** 6 из 7 делегатов живые и оправданы (снимают `skill_name`); `get_max_retries` мёртвая, а `_PROJECT_ROOT` указывает не туда.
**Доказательства.** Импортируется `cli.py:57`, `generated_sql_mode.py:31`, `llm.py:11`. Тесты: `tests/test_skill_config_api.py`, `tests/test_audit_analyzer_generated_sql.py` (последние монокейсят `lib.core.skill_config` напрямую, не обёртку).

| Функция | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `get_db_tables` | 45-46 | `project.json::skills.audit_analyzer.tables` | Whitelist таблиц для LLM-промпта | `generated_sql_mode.py:157` | Оставить |
| `get_db_schema` | 49-50 | Имя схемы | Схема для `get_schema` и квалификации имён | `generated_sql_mode.py:158,160` | Оставить |
| `get_predefined_scripts_table` | 53-54 | Таблица реестра скриптов | Адресует `public.agent_predefined_scripts` без хардкода в трёх местах | `cli.py:267,408` | Оставить |
| `get_llm_config` | 57-58 | `skills.audit_analyzer.llm.*` | Единственный источник LLM-параметров для скилла | `llm.py:36` | Оставить |
| `get_cli_config` | 61-62 | `skills.audit_analyzer.cli.*` | `default_mode` + `max_retries`/`timeout_sec` для LLM | `cli.py:166`, `llm.py:37,45,46` | Оставить (ключ `default_format` мёртвый — вне подсистемы) |
| `get_max_retries` | 65-66 | `get_cli_config()["max_retries"]` | — | **никто** (0 ссылок; `llm.py:45` читает инлайн) | Удалить |
| `build_cache_provider` | 69-82 | `open_cache_provider(READ_ONLY)` | Граница «skill не открывает DuckDB» из ADR | `cli.py:253` | Оставить |

### Что скилл использует из `lib.core.skill_config`, а что нет (ключевой вопрос аудита)

Публичный API `lib/core/skill_config.py` — 17 функций. Использование **этим** скиллом:

| Функция `lib.core.skill_config` | Использует audit_analyzer | Где |
|---|---|---|
| `get_db_tables` | да | `skill_config.py:45` |
| `get_db_schema` | да | `:49` |
| `get_predefined_scripts_table` | да | `:53` |
| `get_llm_config` | да | `:57` |
| `get_cli_config` | да | `:61` |
| `build_cache_provider` | да | `:69` |
| `get_max_retries` | **нет** | дубль, читается инлайн в `llm.py:45` |
| `load_db_config` | нет | 0 ссылок в репозитории (`dead_symbols.md`) |
| `get_tool_config` | нет | 0 ссылок в репозитории (`dead_symbols.md`) |
| `get_in_memory_cache_path` | нет | 0 ссылок в репозитории (`dead_symbols.md`) |
| `get_vector_index_path` | нет | 0 ссылок в репозитории (`dead_symbols.md`) |
| `get_vector_indexes` | нет | 0 ссылок в репозитории (`dead_symbols.md`) |
| `get_embedding_model` | нет | 0 ссылок в репозитории (`dead_symbols.md`) |
| `get_embedding_config` | нет | вызывается только из `get_embedding_model` (`:325`) |
| `get_chunking_config` | нет | чужой периметр: `legal_summarizer/scripts/llm/config.py:43` |
| `get_brief_context_config` | нет | чужой периметр: `legal_summarizer/scripts/llm/config.py:52` |
| `get_vector_db_table` | нет | только тесты `tests/test_skill_config_api.py:99,111,118` |

Итог по `dead_symbols.md`: все 6 функций с 0 ссылок (`load_db_config`, `get_tool_config`, `get_in_memory_cache_path`, `get_vector_index_path`, `get_vector_indexes`, `get_embedding_model`) не используются скиллом — т.е. из мёртвого набора audit_analyzer не «держит» ничего; его собственный вклад в мёртвый код — обёртка `get_max_retries` (65-66) и потребность в `get_vector_db_table`, которая в проде тоже не используется.

---

## `workspace/skills/audit_analyzer/scripts/output.py` — 88 LOC

**Назначение.** Приведение внутреннего формата режимов к плоскому JSON для stdout.
**Что делает.** `prepare_output` разворачивает `data` наружу по трём сценариям (predefined/vector/error), `sanitize_output` рекурсивно прогоняет значения через `text_utils.sanitize_value`. Побочных эффектов нет.
**Зачем нужен.** Единый формат выдачи для агента (ADR: CLI — эталон).
**Вердикт.** Упростить
**Обоснование.** Реэкспорт-обёртка `sanitize_output` не добавляет ничего, а `prepare_output` теряет единственный канал предупреждений (`index_warning`) и `sql` в ошибочном ответе `generated_sql`.
**Доказательства.** Единственный импортёр — `cli.py:56`; единственный вызов обоих функций — `cli.py:574`. Тестами покрыт косвенно (`tests/test_audit_analyzer_cli.py` по формату вывода); `test_gaps.md` файл не отмечает.

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `prepare_output` | 27-83 | Плоский формат `{mode, status, …}` | Единый контракт выдачи; скрывает внутренний конверт `data` | `cli.py:574` | Упростить |
| `sanitize_output` | 86-88 | Делегат в `sanitize_value` | Обезличивание значений перед печатью | `cli.py:574` | Упростить |

**По пункту 4 брифа (back-compat re-export `_sanitize_value`).** Символа `_sanitize_value` в скилле **нет** — grep по `*.py` находит его только в упоминании внутри docstring `lib/utils/text_utils.py`. Что реально есть в `output.py`: `from lib.utils.text_utils import sanitize_value` (21), из-за чего имя `output.sanitize_value` импортируемо (back-compat-реэкспорт по факту), и однострочная функция `sanitize_output` (86-88), которая этот же `sanitize_value` вызывает. То есть утверждение `AGENTS.md` («оставлен back-compat re-export») описывает строку импорта, а не отдельную сущность; дублирования реализации нет — реализация одна, в `lib/utils/text_utils.py`. Правка: вызывать `sanitize_value` напрямую из `cli.py` и убрать однострочный посредник (или, наоборот, оставить `sanitize_output` как единственное имя и убрать импорт `sanitize_value` из `__all__`-подобного использования) — сейчас в файле два имени на одну сущность.

---

## `workspace/skills/audit_analyzer/scripts/llm.py` — 47 LOC

**Назначение.** Адаптер skill-side LLM-вызова к общему `lib/services/llm_client.py`.
**Что делает.** `chat()` подставляет `get_llm_config()`, `max_retries` и `timeout_sec` из `get_cli_config()` и вызывает `call_llm`. Никакого собственного HTTP, ретраев или таймаутов.
**Зачем нужен.** Держит skill-side API (`chat(messages, context=…)`) и единый источник конфигурации; собственный клиент в скилле уже удалён (см. docstring 4-7).
**Вердикт.** Оставить
**Обоснование.** Это обёртка, а не дубль: `lib/services/llm_client.py` не знает про `skill_name` и ничего не подставляет, поэтому логика конфигурации обязана жить здесь. Свёрка с `lib/services/llm_config.py` (резолв провайдера/модели) тоже не даёт дубля — этот слой уже вызывается внутри `get_llm_config`.
**Доказательства.** Импортируется только `generated_sql_mode.py:30` (→ `run:210`). Покрыт `tests/test_audit_analyzer_generated_sql.py` (через стабы `chat`).

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `chat` | 16-47 | Единая точка LLM-вызова скилла | Связывает `skills.audit_analyzer.llm/cli` с `lib.services.llm_client.call_llm` | `generated_sql_mode.py:210` | Оставить |

Мелочь: `**kwargs` (16) читается через `.get("model"/"max_tokens"/"temperature")` (42-44), при этом ни один вызывающий их не передаёт — сигнатура скрывает, что поддерживаются ровно три параметра. `Raises`-секция docstring (32-34) корректна: `RuntimeError` бросает сам `call_llm` (`llm_client.py:96`), `httpx.HTTPStatusError` пробрасывается из retry-слоя (`llm_client.py:159-160`).

---

## `workspace/skills/audit_analyzer/scripts/predefined/__init__.py` — 60 LOC

**Назначение.** Фасад public API predefined-пакета.
**Что делает.** Ре-экспортирует `run`, `list_available`, `list_scripts`, `load_script`, `load_all`, `CacheQueryService`, `ParameterValidator`, `DynamicQueryBuilder`, `BuildError`, `ValidationError`, `DBScriptProvider`, `ScriptDefinition`, `ParamDefinition`, `validate_or_raise`(? — через объекты классов) и объявляет их в `__all__`. Ничего не выполняет при импорте.
**Зачем нужен.** Один импорт вместо семи в `cli.py` и `generated_sql_mode.py`; фиксирует, что skill-side зависит от пакета, а не от внутренних модулей.
**Вердикт.** Оставить
**Обоснование.** Фасад работает, но публикует мёртвые имена (`list_scripts`, `ValidationError`), из-за чего «мёртвый» код выглядит как поддерживаемый API.
**Доказательства.** `cli.py:62` (`run`), `cli.py:276` (`load_all`, лениво), `generated_sql_mode.py:34` (`db_loader.load_all`), `generated_sql_mode.py:35` (`models.ScriptDefinition`), тест `test_audit_analyzer_predefined.py:21-22`. Правка: убрать из `__all__` и docstring те имена, которые удаляются.

---

## `workspace/skills/audit_analyzer/scripts/predefined/models.py` — 77 LOC

**Назначение.** Frozen-dataclass'ы описания скрипта и его параметра.
**Что делает.** Ничего, кроме типизации: `ParamDefinition(type, required, default, description)` и `ScriptDefinition(name, description, long_description, sql_template, parameters, max_rows_default, returns)`. Иммутабельность — единственная гарантия, на которую опирается `db_loader` (скрипты не меняются между load и build).
**Зачем нужен.** Тип-контракт между реестром в БД, валидатором, билдером и LLM-промптом.
**Вердикт.** Оставить
**Обоснование.** Модели минимальны и действительно используются всеми четырьмя потребителями. Требует правки только docstring.
**Доказательства.** 5 статических импортёров (`db_loader`, `builder`, `validator`, `mode`, `generated_sql_mode`), `test_audit_analyzer_predefined.py` — косвенно, через реальные скрипты из PG.

| Класс | Строки | Назначение | Зачем нужен | Кто использует | Вердикт |
|---|---|---|---|---|---|
| `ParamDefinition` | 23-46 | Описание одного параметра | `type` управляет и валидацией, и форматированием значения перед подстановкой | `validator.coerce_types`, `builder.build`, `_list_scripts` (через `pdef.type/required/description`) | Оставить |
| `ScriptDefinition` | 50-70 | Полное описание скрипта | Носитель `sql_template` + параметров для `build()`; `long_description`/`returns` уходят в LLM-промпт | `db_loader._row_to_script`, `builder.build`, `generated_sql_mode.run` (few-shot) | Оставить |

Замечание: `type` объявляет 6 значений (`exact`/`like`/`limit`/`boolean`/`number`/`date`), но в `sql/audit_analyzer/seed_predefined_scripts.sql` реально используются только `date` и `number` — четыре ветки в `builder.build`/`validator.coerce_types` не имеют ни данных, ни фистюр в тестах. Это не мёртвый код (тип-система объявлена в DDL-контракте скриптов), но это **пробел покрытия**: при добавлении скрипта с `like`/`limit` ветки впервые поедут без тестов. Особенно подозрительна ветка `limit` (`builder.py:200-205`): она кладёт в `clean_params[param_name] = True`, поэтому шаблон с `:имя_параметра` получил бы `True` вместо числа.

---

## `workspace/skills/audit_analyzer/scripts/predefined/builder.py` — 229 LOC

**Назначение.** Сборка позиционного SQL из шаблона скрипта + валидированных параметров.
**Что делает.** `build()`: дефолты для отсутствующих параметров → приведение типов → вырезание `{% if %}`-блоков (`_render_template`) → добавление `LIMIT :max_rows` при отсутствии (222-223) → конвертация `:name` → `?` в порядке первого появления (`_convert_to_positional` + `_param_usage_count`). Побочных эффектов нет, состояние не хранится.
**Зачем нужен.** Место, где пользовательские значения перестают быть текстом и становятся bind-параметрами — единственная точка, где скрилл может гарантировать отсутствие инъекции в `query_sql`.
**Вердикт.** Упростить
**Обоснование.** Механика правильная и покрыта тестами, но модуль тащит два артефакта: дубль `__all__` и тип-исключение, которое никто не ловит по типу.
**Доказательства.** Импортируется `mode.py` и `predefined/__init__.py`. Тесты: `test_audit_analyzer_predefined.py:428-483` — покрыты `_convert_to_positional` (двойное двоеточие, двоеточие в строковом литерале) и `build` для `number`. **Не** покрыты: `_render_template` напрямую, ветка `limit`, `BuildError`.

| Класс / функция | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `BuildError` | 69-74 | Исключение сборки SQL | Единственный сигнал «нечем собрать запрос» | `build:179` (`raise`) | Упростить |
| `BuildError.__init__` | 72-74 | Инициализация | — | `build:179` | Упростить (клон из `duplicates.md:26`) |
| `DynamicQueryBuilder` | 77-229 | Сборщик SQL (classmethod-only, состояния нет) | Механизм подстановки параметров | `mode.py:164` | Оставить |
| `DynamicQueryBuilder._render_template` | 81-115 | Вырезание `{% if param %}…{% endif %}` | Опциональные условия в шаблоне без Jinja-движка | `build:220` | Оставить (нет прямых тестов) |
| `DynamicQueryBuilder._convert_to_positional` | 118-148 | `:name` → `?` с negative lookbehind | Защищает `::type_cast` и строковые литералы от порчи | `build` (конец) | Оставить (покрыт тестами 440/459) |
| `DynamicQueryBuilder.build` | 151-229 | Полный pipeline + `LIMIT` | Основной контракт сборки | `mode.py:164` | Оставить |
| `_param_usage_count` | 35-63 | Порядок первого появления `:name` | Позиционные параметры должны идти в порядке появления в SQL, иначе `?` разъедутся со значениями | `_convert_to_positional` | Оставить |

Правки: (1) удалить `__all__` на 66 (первый на 32 уже достаточен, но он объявляет `_param_usage_count` — оставить 32, удалить 66); (2) в `mode.run` ловить `BuildError` явно, чтобы его сообщение доходило до агента без обёртки в «Ошибка сборки SQL: …», либо заменить класс на `ValueError` — сейчас единственный бросок (179) перехватывается `except Exception` (`mode.py:165`), и тип ошибки не даёт ничего; (3) docstring 12-14 содержит оборванный дубль фразы «в порядке появления `?` в SQL».

---

## `workspace/skills/audit_analyzer/scripts/predefined/validator.py` — 212 LOC

**Назначение.** Нормализация и проверка пользовательских параметров скрипта.
**Что делает.** `validate()` = `merge` (только объявленные параметры, пустые отбрасываются) → `check_required` → `coerce_types` (по `ParamDefinition.type`, дата — через `_is_valid_iso_date` с `datetime.strptime`); возвращает `(merged, error_message)` — **без исключений**.
**Зачем нужен.** Единственное место, где вход пользователя нормализуется до сборки SQL; сообщения об ошибках — то, что читает агент.
**Вердикт.** Упростить
**Обоснование.** Продуктивный путь (`validate`) нужен и покрыт тестами; параллельный типизированный путь (`validate_or_raise` + `ValidationError`) не используется никем и удалён после обновления docstring 9-11.
**Доказательства.** Импортируется `mode.py` (через фасад) и `predefined/__init__.py`. Тесты: `test_audit_analyzer_predefined.py:349-425` — `validate` покрыт для required/типов/дат; `validate_or_raise` — 0 тестов, 0 вызовов в репозитории (проверено отдельно).

| Класс / функция | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `ValidationError` | 49-60 | Исключение валидации | — | только `validate_or_raise:210` | Удалить |
| `ValidationError.__init__` | 57-60 | Инициализация | — | `validate_or_raise:210` | Удалить (клон, `duplicates.md:26`) |
| `ParameterValidator` | 63-212 | Валидатор параметров (все методы classmethod/staticmethod) | Единая точка нормализации | `mode.py:159` | Оставить |
| `ParameterValidator.merge` | 77-97 | Фильтрация неизвестных/пустых ключей | Защищает от «тихих» лишних параметров | `validate` | Оставить |
| `ParameterValidator.check_required` | 100-117 | Проверка обязательных | Понятная ошибка вместо пустого SQL | `validate` | Оставить |
| `ParameterValidator.coerce_types` | 120-161 | Проверка типов по `ParamDefinition.type` | Ловит мусор до сборки; `date` — строгий ISO | `validate` | Оставить (имя врёт: приводит `builder.build:212`, здесь только проверка) |
| `ParameterValidator.validate` | 164-200 | Полный цикл, tuple-форма | **Единственный** используемый API валидатора | `mode.py:159`; тесты 354/361/368/389/412/421 | Оставить |
| `ParameterValidator.validate_or_raise` | 203-212 | Как `validate`, но бросает | — | **никто** (0 prod, 0 тест) | Удалить |
| `_is_valid_iso_date` | 28-43 | Строгая проверка `YYYY-MM-DD` | `datetime.strptime` отсекает мусорные даты; regex-фильтр сохраняет строгий формат на Python ≥3.11 | `coerce_types` | Оставить |

---

## `workspace/skills/audit_analyzer/scripts/predefined/mode.py` — 199 LOC

**Назначение.** Оркестратор режима predefined: скрипт → валидация → сборка → `query_sql`.
**Что делает.** `run()` валидирует вход, резолвит скрипт **только** из БД (`_resolve_script`), при ошибке дописывает список доступных имён, гоняет `ParameterValidator.validate` + `DynamicQueryBuilder.build`, выполняет `db.query_sql(sql, sql_params)` и формирует плоский результат. Побочные эффекты: два дополнительных round-trip'а к кэшу на пути ошибки (`list_available` при 154).
**Зачем нужен.** Канонический pipeline режима; DB-only source of truth (никакого Python-реестра).
**Вердикт.** Оставить
**Обоснование.** Функционально нужен; требует точечных правок (несогласованный `mode` в payload, неоднородный `result.get`, ложный docstring про `validate_sql`, мёртвая `list_scripts` и дублирующий Protocol).
**Доказательства.** Импортируется только фасадом (`predefined/__init__.py`), вызывается из `cli.py:62,423`. Тесты `test_audit_analyzer_predefined.py` (классы по скриптам, 37-345) — против **живой PG** (`conftest.py: db_service`), то есть это интеграционные, а не юнит-тесты.

| Класс / функция | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `CacheQueryService` | 50-64 | Protocol «нужен только `query_sql`» | Типизация зависимости от кэша без знания реализации | аннотации `run:102` | Слить с `predefined/db_loader.py` |
| `CacheQueryService.query_sql` | 60-64 | Единственный метод протокола | — | (тип) | Слить (тело 5 строк идентично `db_loader.py:42-46`, `duplicates.md:133-134`) |
| `list_available` | 67-69 | Имена скриптов через запятую | Подсказка агенту в сообщении об ошибке | `run:154` | Оставить |
| `list_scripts` | 72-83 | Метаданные всех скриптов | — | **никто** (0 prod, 0 тест; определение + `__all__:45` + ре-экспорт) | Удалить |
| `_resolve_script` | 86-96 | DB-only lookup по имени | Запрет fallback'а на Python-литерал (регрессия silent data swap) | `run:147` | Оставить |
| `run` | 99-199 | Полный цикл режима | Единственная реализация predefined | `cli.py:423` | Оставить |

Замечания по `run`: (1) на 179 стоит isinstance-guard, а на 180 — нет (`result.get("status")` → `AttributeError` при не-dict ответе); (2) payload успеха содержит `mode` (191), а все error-payload (124-188) — нет: форма ответа неоднородна (на маскировку не влияет, но это ловушка для потребителей); (3) `except Exception` (165, 173) схлопывает типизированные ошибки провайдера; (4) docstring 133 заявляет защиту через `validate_sql` — её нет.

---

## `workspace/skills/audit_analyzer/scripts/predefined/db_loader.py` — 154 LOC

**Назначение.** Чтение реестра скриптов из `public.agent_predefined_scripts` через кэш-провайдер.
**Что делает.** `load_all` / `load_script` строят SELECT по 7 колонкам, конструируют `ScriptDefinition` через `_row_to_script` (валидирует `name` по `^[a-z][a-z0-9_]*$`, требует непустые `sql_template`/`description`, парсит JSONB `parameters`). Имя таблицы резолвится вызывающим (`get_predefined_scripts_table`), схема по умолчанию `main` (`_qualified_table`).
**Зачем нужен.** Единственная точка чтения реестра — так сохраняется DB-first и skill не знает про PG.
**Вердикт.** Оставить
**Обоснование.** Нужен; проблема в молчании на ошибках, а не в логике.
**Доказательства.** Импортируется `mode.py:30`, `generated_sql_mode.py:34`, `predefined/__init__.py`. Тесты: `test_audit_analyzer_predefined.py` (в т.ч. `test_no_fallback_when_db_table_missing:306`). Покрытие: `test_gaps.md:61` ошибочно помечает файл как непокрытый (статик смотрел только корневой `tests/`).

| Класс / функция | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `DBScriptProvider` | 34-46 | Protocol «нужен только `query_sql`» | Типизация зависимости от кэша | аннотации `load_all`/`load_script`/`list_available` | Слить с `predefined/mode.py` |
| `DBScriptProvider.query_sql` | 42-46 | Единственный метод протокола | — | (тип) | Слить (клон `CacheQueryService.query_sql`) |
| `_row_to_script` | 49-87 | PG-row → `ScriptDefinition` | Валидация данных реестра: имя-идентификатор, непустые SQL/description, разбор `parameters` | `load_all:125`, `load_script:152` | Оставить |
| `_qualified_table` | 90-95 | `"schema.table"` → `(schema, table)` | Безопасная подстановка имени таблицы в f-string | `load_all:103`, `load_script:136` | Оставить |
| `load_all` | 98-129 | Весь реестр | Few-shot для LLM + каталог для CLI | `mode.list_available:69`, `cli.py:278`, `generated_sql_mode.py:166` | Оставить |
| `load_script` | 132-154 | Один скрипт по имени | Основной путь выполнения | `mode._resolve_script:96` | Оставить |

Замечания: SELECT-список продублирован дословно (111-114 и 138-141) — вынести в константу; четыре `except Exception` (117-118, 126-127, 144-145, 153-154) превращают и «кэш недоступен», и «колонка в реестре переименована» в пустой результат без единой записи в лог. Минимальная правка — `print(..., file=sys.stderr)` + различение этих случаев в сообщении `mode.run:148-156`.

---

## `workspace/skills/audit_analyzer/scripts/__init__.py` — 7 LOC

**Назначение.** Пустой пакет с описательным docstring.
**Что делает.** Ничего. Существует, чтобы `workspace.skills.audit_analyzer.scripts.*` был импортируемым пакетом (это нужно абсолютным импортам в `cli.py:62,276` и `generated_sql_mode.py:34-35`).
**Зачем нужен.** Без него абсолютные импорты вида `workspace.skills.audit_analyzer.scripts.predefined` не разрешаются (namespace-package сработало бы только при добавлении корня в `sys.path`, что cli.py и делает — но пакетный `__init__` фиксирует намерение и упрощает static-анализ).
**Вердикт.** Оставить
**Обоснование.** Функционально нужен для абсолютных импортов; требует только правки docstring, который обещает несуществующий режим `sql` (4-5) — режимов три: `predefined`, `generated_sql`, `vector` (`cli.py:67`).
**Доказательства.** Статических импортёров 0; косвенные импорты — `cli.py:62,276`, `generated_sql_mode.py:34-35`, тесты (`tests/test_audit_analyzer_cli.py` импортирует `workspace.skills.audit_analyzer.scripts.cli`). `docs/skill-tool-inventory.md:41` уже помечает его как «legacy-фасад (никем не импортировался)» — устаревшая формулировка: импортируется как пакет.

---

## `SKILL.md` vs фактический CLI (ключевой вопрос 7)

| Что заявлено | Факт | Вердикт |
|---|---|---|
| `SKILL.md:104-117` — 6 скриптов: `analytics_by_year_month`, `audit_dynamics`, `audit_effectiveness`, `audit_types_stats`, `top_audited_objects`, `violations_by_type` | В seed 5: `audit_status_summary`, `top_violations_by_type`, `violations_by_period`, `audits_by_period`, `audit_effectiveness_summary` (+`audit_types_stats` в другом файле) | Переписать раздел |
| `SKILL.md:119-121` — «все параметры optional» | `date_from`/`date_to` — `required: true` у двух скриптов | Исправить |
| `SKILL.md:58` — «сводка по статусам похожих проверок → не подходит ни один» | Есть `audit_status_summary` | Исправить |
| `SKILL.md:194,198` — `audit_analyze --mode …` | Такой команды нет | Заменить на `python workspace/skills/audit_analyzer/scripts/cli.py` |
| `SKILL.md:225` — «LIMIT добавляется автоматически» | `LIMIT` инъецирует только `builder.build:222-223` (predefined). В `generated_sql_mode` — никогда | Исправить/добавить инъекцию в `run` |
| `SKILL.md:165-166` — `--top-k`: «дефолт 5, потолок 50, валидация в CLI» | `default=None` (`cli.py:221`), потолка нет ни в CLI, ни в `search_vector` | Валидировать диапазон или исправить текст |
| `SKILL.md:171-172` — `--threshold` «диапазон [0.0, 1.0]» | Проверки нет | То же |
| `SKILL.md:233-239, 253-255` — формат вывода `{"mode","status","data":{...}}` | `main()` плоско выдаёт `{"mode","status",...}`; при ошибке `generated_sql` теряется даже `sql` (`output.py:66-77`) | Переписать раздел «Формат вывода» |
| `SKILL.md:297` — «формат `{mode, status, data}` одинаков для всех трёх режимов» | `data`-конверт есть только у `--list-scripts`/`--list-indexes` | Исправить |
| `SKILL.md:308-318` — «файл кэша никто не держит, работающий gateway не мешает skill'у» | `CacheBusyError` документирован как реальный исход (`lib/core/skill_config.py:293-295`), а `cli.py:247-251` фиксирует, что прежний совет «запустите gateway» был вводящим в заблуждение | Смягчить формулировку |
| `SKILL.md:339, 397-399` — тесты в `tests/test_audit_analyzer_{predefined,behavior}.py` | Файлы лежат в `workspace/skills/audit_analyzer/tests/` | Исправить пути |
| `SKILL.md:351, 356` — `python workspace/skills/audit_analyzer/scripts/cli.py …` | ✔ работает (`_PROJECT_ROOT` = `parents[4]`) | Оставить |
| Режимы `predefined` / `generated_sql` / `vector` | ✔ совпадают с `MODES` (`cli.py:67`) | Оставить |
| Флаги `--script/--query/--params/--index-name/--top-k/--threshold/--context/--list-scripts/--list-indexes` | ✔ все 9 существуют и имеют смысл | Оставить |

Согласованность флагов с `AGENTS.md` и `docs/INTERNAL_API.md:290-302`: да — единственная точка доступа `scripts/cli.py --mode predefined`, tools `duckdb_query`/`vector_search` отсутствуют, agent-tools не возвращаются. Расхождений в самих флагах нет; расхождения целиком в документации (имя команды, каталог скриптов, формат вывода).

---

## Кросс-подсистемные находки

1. **`IndexIntegrityError` — подтверждаю находку `02-services-cache`.** `raise IndexIntegrityError` в репозитории — 0 совпадений; определение (`lib/services/cache_provider.py`), импорт и `except` в `cli.py:65,493`, упоминания в тестах и `openspec/specs/data/cache-provider/spec.md`. Следствие: индекс-инвариант из спецификации (`spec.md:143-148` — `search_vector` MUST поднимать `IndexIntegrityError` при STALE/INVALID) **не реализован вовсе**, а CLI его ловит. Это не «мёртвая обработка ошибки», а расхождение с собственной спецификацией. Действие одно из двух: реализовать проверку подписи в `DuckDbCacheStore.search_vector` либо удалить класс, `except` в CLI и требование из спецификации (тогда поправить `openspec`).
2. **`SearchResult.signature_status` не заполняется провайдером** (`duckdb_cache_store.py:1322+` — `SearchResult` создаётся без этих полей, default `""`), поэтому `cli.py:520-531` недостижим **и вдобавок** `output.prepare_output` (`output.py:79-81`) отбрасывает `index_warning`. Связка «provider → CLI → формат вывода» не работает ни на одном из двух концов.
3. **Оставшаяся целостностная проверка в скилле слабее заявленной:** `_list_indexes` (`cli.py:361-375`) считает индекс `CURRENT` по совпадению имени с декларацией, тогда как `dimension`/`updated_at`/`metric` всегда `None` (`cache_provider_impl.list_runtime_vector_indexes` их не отдаёт). Смена embedding-модели на том же имени не детектится нигде. Это и есть тот STALE, ради которого написан спец-инвариант из п. 1.
4. **`lib/services/llm_client.py` vs `scripts/llm.py` — дублирования нет.** Проверено: собственный HTTP-клиент в скилле удалён (docstring `llm.py:4-7`), осталась привязка конфигурации скилла к общему вызову. `lib/services/llm_config.py` тоже не дублируется — он вызывается внутри `get_llm_config`.
5. **Мёртвый API `lib.core.skill_config`:** audit_analyzer использует 6 из 17 функций. 6 функций с 0 ссылок из `dead_symbols.md` не используются скиллом; `get_vector_db_table` — только в тестах; `get_chunking_config`/`get_brief_context_config` принадлежат `legal_summarizer`. Собственный мёртвый вклад скилла — обёртка `get_max_retries` (`skill_config.py:65-66`).
6. **Удалённые core-модули ещё упоминаются в артефактах вне кода:** `sql/audit_analyzer/create_public_agent_predefined_scripts.sql:28` — `COMMENT ON COLUMN sql_template` указывает на «Реализация: PredefinedScriptRequestBuilder в lib/services/predefined_script_request.py»; файла не существует (есть тест, который это фиксирует: `test_audit_analyzer_behavior.py:446-453`). `docs/skill-tool-inventory.md:31-40` перечисляет 5 удалённых core-модулей, 3 удалённых tool'а (`workspace/tools/audit_analyzer_tool.py` не существует) и несуществующую обёртку `audit_analyze.{bat,sh}` как живые компоненты. Комментарий в БД виден агентам через `--list-scripts`? Нет (в payload его нет) — но он виден любому, кто читает схему.
7. **Границы с `lib/core/skill_config`:** `_PROJECT_ROOT` в двух файлах считается по-разному и один из расчётов неверен — `cli.py:51` (`parents[4]` → корень репозитория) против `skill_config.py:23` (`parents[1]` → `<repo>/workspace`). Импорт `lib.core` в обёртке работает только потому, что корень уже в `sys.path` из `cli.py`/pytest. Договориться об одном bootstrap.
8. **Профиль `test` в standalone-процессе** (`cli.py:148`): сегодня безвредно (`profiles/test.jsonc` переопределяет только таблицы канала/логирования), но процесс навыка в prod-развёртывании инициализирует `SETTINGS` как тестовый. Риск для `gateway.cache.*`/LLM-конфига появится, как только профиль начнёт их определять.
9. **Ошибка статического анализа (`test_gaps.md`):** файлы `predefined/builder.py:37`, `validator.py:38`, `mode.py:44`, `db_loader.py:61`, `models.py:88`, `predefined/__init__.py:102` помечены как «прямых тестов нет». Ложно: `workspace/skills/audit_analyzer/tests/test_audit_analyzer_predefined.py` покрывает builder/validator/mode/db_loader через живой PostgreSQL; анализатор смотрел только корневой `tests/`. Реальные пробелы внутри покрытых модулей: `_render_template`, ветка `limit`, `BuildError`, `ValidationError`, `validate_or_raise`, `list_scripts`.
10. **`err1.log` в каталоге скилла** (`workspace/skills/audit_analyzer/err1.log`) — артефакт ручного запуска с выводом уже удалённой строки логирования; попадает в контекст при любом сканировании каталога скилла.

---

## Что проверено и не проверено

**Проверено:** полное чтение всех 12 файлов (2035 LOC); фактические вызовы каждого символа — grep по всему репозиторию (включая `tests/`, `sql/`, `docs/`, `project.json`, `SKILL.md`); недостижимость `except argparse.ArgumentTypeError` — репро на текущем Python; резолв `_PROJECT_ROOT` в обоих файлах — вычислением путей; отсутствие write-путей — grep по `upsert_records|replace_records|ensure_schema|duckdb|faiss|INSERT|UPDATE|DELETE|CREATE` в `scripts/**`; содержимое `profiles/test.jsonc`; типы параметров, реально используемые в `sql/audit_analyzer/*.sql`; соответствие каталога скриптов в `SKILL.md` фактическому seed; существование/отсутствие `audit_analyze.{bat,sh}`, `workspace/tools/audit_analyzer_tool.py`, `lib/services/predefined_script_*.py`.

**Не проверено (нужен запуск):** фактический вывод CLI для всех трёх режимов (нет поднятой БД/кэша в этой сессии — выводы о формате сделаны по коду `main()`/`prepare_output`, а не по фактическому stdout); поведение DuckDB при занятом файле и язык его сообщения об ошибке (строка `"временно занята"` оценена как локале-зависимая по коду, эмпирически не подтверждена); фактическое число LLM-вызовов при вложенных ретраях (16 — расчётное верхнее значение из `MAX_ATTEMPTS=4` × `max_retries=3`).
