# Аудит: Dev-утилиты `tools/` (standalone-скрипты)

## Сводка группы

Файлов: 20 · LOC: 4 636 · классов: 9 · методов: 9 (+1 вложенная функция) · функций уровня модуля: 118

Символов разобрано: **144** (9 классов, 15 методов/свойств, 120 функций уровня модуля
и вложенных) + 5 модульных констант `legacy_audit.py` и ключевые
конфигурационные множества/регулярные выражения каждого файла (покрыты
построчно в соответствующих разделах, отдельных строк вердикта не имеют).
`НЕ РАЗОБРАНО`: 0. Сверка полноты: количество `def`/`class` в исходниках — 144
(`build_vectors.py` 17, `test_audit.py` 26, `diagnose_startup.py` 11,
`validate_component_specs.py` 11, `demo_internal_fallback.py` 11, `migrate.py` 12,
`legal_benchmark.py` 8, `legacy_audit.py` 8, `check_indexes.py` 6,
`check_worker_pool_integrity.py` 5, `audit_nanobot_contracts.py` 5,
`release_v252.py` 5, `release_v251.py` 4, `scan_nanobot_inventory.py` 4,
`architecture_guard.py` 3, `apply_test_profile_tables.py` 2,
`extract_office_structure.py` 2, `generate_comments_sql.py` 2,
`smoke_post_cleanup.py` 2, `__init__.py` 0).

**Ключевые находки**

1. **`tools/extract_office_structure.py:10` — падает на импорте.** `from workspace.utils.office_files import extract_structure`; функции `extract_structure` в модуле нет (в `workspace/utils/office_files.py` есть `extract_text`/`extract_tables`/`summarize`, но не `extract_structure`). Проверено запуском: `ImportError: cannot import name 'extract_structure'`. Файл 100 % мёртвый — `main()` недостижим. **Вердикт: Удалить** (замена — `office_files.summarize`, см. §файл).

2. **`tools/legal_benchmark.py:33` — падает на импорте.** Импорты верхнего уровня `chunking.* / document.* / planning.* / llm.*` без префикса; реальные пакеты лежат в `workspace/skills/legal_summarizer/scripts/`, `sys.path` не настраивается. Проверено: `ModuleNotFoundError: No module named 'chunking'`. Плюс `run_benchmark` (`:114`) не вызывается ниоткуда — нет ни `main()`, ни `if __name__ == "__main__"`, т.е. `python tools/legal_benchmark.py` не делает ничего. **Вердикт: Удалить.**

3. **`tools/generate_comments_sql.py:33` — падает при импорте.** Вся логика на верхнем уровне модуля (нет `main()`), строка 33 читает `workspace/skills/audit_analyzer/cache/schema.json`, которого в репозитории **нет** (проверено: `FileNotFoundError`). Скрипт не запускается в принципе, пока оператор вручную не подложит дамп. Плюс выход `sql/comments/apply_all_comments.sql` устарел: в нём нет `agent_gateway_logs."user_id"` (миграция `V004__agent_gateway_logs_user_id.sql`) и нет комментариев для `agent_worker_claims`. **Вердикт: Упростить** (перенести в `main()` + добавить недостающие таблицы) либо `Удалить`, если комментарии к схеме больше не ведутся.

4. **`tools/build_vectors.py:845-848` — документированный exit code не существует.** `--validate-only` обещает «Выход 0 — всё валидно, 1 — ошибки», но `main()` (`:802`) не имеет возвращаемого значения ни на одном пути, а `__main__` делает `sys.exit(main() or 0)` (`:1023`). Ошибки конфига только логируются (`:970-977`). Фактический код возврата — **всегда 0**. **Вердикт: Упростить** (починить или убрать обещание из help).

5. **`tools/build_vectors.py:90-94` — standalone-регистрация storage-ресурса мертва.** `register_vector_storage()` вызывается только на верхнем уровне модуля внутри `try/except Exception: pass`, а `SETTINGS` инициализируется лишь позже, в `main()` (`:811-813`). Значит вызов **гарантированно бросает и всегда глушится**; `main()` его не повторяет. `AGENTS.md:20` утверждает, что standalone-режим `build_vectors.py` делегирует в `TableRegistry.register_infra` — фактически нет. **Вердикт: Упростить** (перенести вызов в `main()` после `_initialize_settings`).

6. **`VectorIndexBuildService` (`lib/services/vector_index_service.py:41`) — 0 вызовов, подтверждено.** `tools/build_vectors.py:78` импортирует **только** re-export `get_embedding`; grep по всему репозиторию даёт 0 внешних ссылок на класс (упоминания — только в самом модуле и в `docs/architecture/runtime-patcher-inventory.md`). Подтверждаю находку другого аудитора; вердикт по классу принадлежит отчёту 02.

7. **`tools/migrate.py:75-84` дублирует `workspace/utils/db.py:867-879`.** Обе реализации `resolve_dsn()`, но с разной семантикой: `migrate.py` читает env `DATABASE_URL` первым и `SystemExit`-ит при пустом значении; `db.py` сначала читает `_dsn` из `configure()`, потом `SETTINGS`, и возвращает `""`. Так как `project.json:44` содержит `"dsn": "${DATABASE_URL}"`, env-ветка `migrate.py` в штатном конфиге **мертва** (тот же результат даёт `get_setting`). **Вердикт: Слить с `workspace/utils/db.py`** (оставить `SystemExit`-обёртку в вызывающем коде).

8. **`tools/migrate.py:177` и `:239` сравнивают номера версий как строки.** `m.version <= target` при `version` вида `"004"`. Для текущих 001–004 это случайно работает, но `--target 10` при появлении `V010` отсечёт всё, что `<= "10"` лексикографически, т.е. `"004" <= "10"` → False. **Вердикт: Упростить** (pad или `int()`).

9. **`tools/legacy_audit.py:371` — config-guard внутри мёртвого `try`.** `json.loads(project.json)` падает с `JSONDecodeError` (файл — JSONC с `//`-комментариями; проверено: `line 2 column 3`), исключение глушится на `:377`, и проверка `gateway.vector_index` **никогда не выполняется**. Покрытие есть только потому, что `tests/test_no_legacy_imports.py:69-95` реализует ту же проверку на regex вручную. **Вердикт: Упростить** (использовать `config.get_setting`/regex, как в тесте).

10. **`tools/legacy_audit.py:240` — allow-list тестовых функций не работает.** `_is_allowed_legacy_test(rel, line_no)` строит ключ `f"{rel}::{line_no}"`, а `_ALLOWED_LEGACY_TESTS` (`:133-200`, 33 записи, 67 строк) содержит ключи вида `rel::test_function_name`. Совпадений быть не может (проверено: `_is_allowed_legacy_test('tests/test_x.py', 137) == False`). Все 33 записи — мёртвый груз. **Вердикт: Упростить** (удалить allow-list) либо починить формат ключа.

11. **`tools/test_audit.py:111-135` — блок проверки smells продублирован.** `_check_compare_smell` и `_check_call_smell` вызываются для одних и тех же узлов дважды (строки 113-114 и 132-133), поэтому **каждый smell репортится дважды**, а `smell_counter` в payload завышен вдвое. Плюс хардкод абсолютного пути `C:\Users\Алексей\.nanobot` (`:23`), `OUT_DIR.mkdir()` **на импорте** (`:25`) и захардкоженный датированный путь сессии `test-suite-audit-2026-09-10` (`:24`). **Вердикт: Упростить** или `Удалить` (см. ниже — файл не применим к текущему состоянию).

12. **`tools/architecture_guard.py` — не CLI, а три неиспользуемые функции.** Нет ни `main()`, ни `argparse`; `python tools/architecture_guard.py` не печатает ничего. `openspec/changes/archive/2026-09-27-post-0.3.5-patches-cleanup/tasks.md:53` числит его verify-шагом («если существует») — шаг является no-op. `legal_summarizer_progress_audit.md:159` фиксирует «Этап 37 — решить судьбу `architecture_guard.py`», то есть судьба не решена. Плюс `is_factory_pattern` срабатывает на 5+ легитимных модулях репозитория (`lib/core/agent_factory.py`, `bus_factory.py`, `lib/services/channel_factory.py`, `llm_usage_store_factory.py`). **Вердикт: Удалить** (или превратить в реальный CLI, если идея guard'а нужна).

13. **`tools/audit_nanobot_contracts.py` не используется, а его главная фича — самоизменение импортированных модулей.** CI-джоб `upgrade-readiness` (`.github/workflows/ci.yml:102-106`) выполняет `pytest tests/contract/`, а не этот скрипт. При этом `_walk_dotted` на строках 63-67 делает `setattr(cur, ...)` по объектам реального `nanobot` — инструмент мутирует проверяемую библиотеку. Сам скрипт признаёт непригодность вывода («audit имеет False Positives на dataclass fields / private instance attrs», `:175-176`) и всегда возвращает exit code 0, то есть как CI-гейт непригоден. **Вердикт: Упростить** (удалить `setattr`, дедуп findings, сделать exit code значимым) либо `Удалить` в пользу `tests/contract/`.

14. **`tools/smoke_post_cleanup.py` привязан к завершённой change.** Ссылка `:16` на `openspec/changes/post-0.3.5-patches-cleanup/tasks.md` устарела — каталог переехал в `openspec/changes/archive/2026-09-27-post-0.3.5-patches-cleanup/`; задачи 6.3/8.3/8.4 там помечены `[x]`, change архивирована. Хуже: заявленный в docstring третий контракт (`turn_completed` в `agent_gateway_logs_test`) при отсутствии Postgres даёт `return 0` (`:121-123`), а при отсутствии события — только `WARN` (`:150-153`). Smoke **не может провалиться** по своему главному условию. Плюс хардкод DSN с паролем (`:31`) и `--profile=test` (`:33`). **Вердикт: Упростить** (сделать проверки блокирующими) либо `Удалить`.

15. **`tools/release_v251.py` и `tools/release_v252.py` — вердикт по каждому (ниже).** `docs/RELEASE.md:83` прямо предписывает «Не редактировать старый … оставь как исторический маркер», а §1.5 называет `release_v251.py` образцом для копирования. Формально это задокументированное решение, но фактически образец устарел: `release_v252.py` **не является** копией v251 (в нём `_print_payload`, флаг `--run`, безопасный дефолт dry-run, payload в `tempfile` вместо CWD) — то есть `docs/RELEASE.md:86` (`cp tools/release_v251.py tools/release_v252.py`) сейчас даёт неправильный результат.

16. **`tools/` полностью вне линтера.** `pyproject.toml` содержит `extend-exclude = ["tests", "tools", "benchmarks", "*.egg-info"]`, при этом шаг CI `ruff check lib workspace tests tools` (`.github/workflows/ci.yml:38`) явно перечисляет `tools` — но ruff при заданном каталоге находит в нём **51 ошибку** (12 × `F841` unused-local, 8 × `F541`, 7 × `I001`, 6 × `E402`, 6 × `F401`, 4 × `B905`, …). По всей команде CI — 653 ошибки. Три сломанных файла (`extract_office_structure`, `generate_comments_sql`, `legal_benchmark`) проходят ruff, потому что ruff не резолвит импорты. **Кросс-подсистемная находка** (принадлежит не `tools/`).

17. **`tools/diagnose_startup.py` — инструмент испорчен чужим багом, но сам корректен.** Он потребляет `lib/services/runtime_inventory.py:153`, где канон записан как `name="ExampleTool"`, тогда как `project_tool_loader` регистрирует tool по `name`-свойству (`example_tool`). Асимметрия `registered` (`name`-свойство) vs `disabled`/`failed` (`cls.__name__`, fallback в `_canonical_name`) означает: при `example` выключенном (дефолт в `config.json`) — расхождения нет; при включённом — `unexpected: ['example_tool']` и `--strict` даёт **exit 2**. Функционально инструмент исправен, канон надо чинить в `lib/` (отчёты 03/08).

**Вердикты (по строкам таблиц символов, 154 строки):** Оставить 76 · Упростить 51 · Удалить 26 · Слить 1 · Перенести 0
(из них 5 строк — модульные константы `legacy_audit.py`; 4 вложенные функции
`render_report` учтены одним блоком; остальные соответствуют 144 символам
исходников + 5 строкам-дублям «вложенная функция»/«класс» в таблицах)

---

## `tools/__init__.py` — 1 LOC

**Назначение.** Пометить каталог как Python-пакет.
**Что делает.** Ничего: единственная строка — docstring `Project-level dev tooling (not part of any skill package).`
**Зачем нужен.** Превращает `tools/` в импортируемый пакет: от этого зависят `tests/test_no_legacy_imports.py:44` (`from tools.legacy_audit import assert_no_legacy`) и `tests/test_build_vectors_cli.py` (`import tools.build_vectors`). Без него `import tools.X` работал бы только как namespace-package (сработало бы в 3.3+, но неявно).
**Вердикт.** `Оставить`
**Обоснование.** 1 строка, единственная точка, делающая `tools.*` импортируемым для тестов; удаление не ломает ничего, но и не даёт выигрыша, а расхождение с `workspace/tools/__init__.py` (который нужен для auto-discovery) вводит в заблуждение — стоит в комментарии указать, что auto-discovery `workspace/tools/` этот пакет **не** использует.
**Обоснование (доказательства).** Импортеры: `tests/test_no_legacy_imports.py:44`, `tests/test_build_vectors_cli.py`. `orphans.md:53` фиксирует 1 LOC / 0 ссылок — статика не видит `import tools.*` в тестах.

---

## `tools/build_vectors.py` — 878 LOC

**Назначение.** Standalone-индексатор: читает декларацию `project.json::gateway.vector.index.indexes`, эмбеддит строки исходных PG-таблиц, пишет в `storage_table`, прогревает FAISS через `CacheProvider`.
**Что делает.** Инициализирует `SETTINGS` в профиле `test` (`:812-813`), регистрирует infra-ресурс (не срабатывает, см. находку 5), для каждого включённого индекса валидирует конфиг, читает строки батчами, эмбеддит, `upsert` в storage, пересобирает FAISS. Пишет в БД и в `storage_table`; логи идут через `loguru` в stderr.
**Зачем нужен.** Основной producer векторных данных. Документирован как рабочая процедура в `AGENTS.md:54`, `docs/DATABASE.md:369-378`, `docs/INTERNAL_API.md:356-394`, `docs/MIGRATION.md:19-51`, `README.md`. Покрыт `tests/test_build_vectors_cli.py`. Без него `--list-indexes`/`search_vector` в runtime дадут пустые индексы.
**Вердикт.** `Упростить`
**Обоснование.** Нужен и является самой используемой утилитой каталога, но содержит минимум 4 функциональных дефекта (exit code, мёртвая infra-регистрация, неверный help для `--batch-size`, мёртвый параметр `metric`) и устаревший import-контракт.

**Побочные эффекты, не отражённые в docstring:** (1) `OUT_DIR`-подобных записей на диск нет, но `_rebuild_faiss:443` открывает `CacheProvider` и **никогда не вызывает `close()`** — по одному провайдеру на индекс; (2) `--dry-run` подключается к PG и читает данные, то есть не является «режимом без БД»; (3) `--validate-only` тоже требует живой PG (проверяет `information_schema`), несмотря на обещание «без сборки».

#### Функции уровня модуля
| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `fetchone` | 97–100 | Обёртка `utils.db.fetch` → первая строка | Удобство, дублирует `utils.db.fetchone` | `_validate_index_config`, `build_index` | Слить с `workspace/utils/db.py` |
| `_setup_logging` | 103–115 | Перенастроить loguru на stderr, без ANSI | Идемпотентный логгер под cron/redirect | `main():851` | Оставить |
| `_fmt_eta` | 118–127 | Человекочитаемый ETA | UI прогресса | `_print_progress:151` | Оставить |
| `_interactive_stderr` | 130–140 | TTY или нет | Выбор режима прогресса | `_print_progress` (через `build_index`) | Оставить |
| `_print_progress` | 143–158 | Прогресс-строка с ETA или лог раз в `batch_size` | Наблюдаемость долгих прогонов | `build_index:656` | Оставить (но переименовать `batch_size` → `progress_every`: параметр **не** влияет на эмбеддинг) |
| `_validate_index_config` | 161–340 | Pre-flight: поля, existence таблиц/колонок, формат `embedding_cols`, дубликаты колонок | Единственная защита от сборки по сломанному конфигу | `main():967` | Упростить — параметр `metric` (`:162`) **не используется в теле**; вызывается как `main():966-968` |
| `_format_content` | 342–350 | Склейка `content_columns` в текст | Сборка plaintext для эмбеддинга | `build_index` | Оставить |
| `_norm_pk` | 352–365 | Нормализация PK в строку | Совместимость UUID/BIGINT/INT | `build_index` | Оставить |
| `_get_existing_entries` | 367–375 | Прочитать `content_hash` по PK из storage | Инкрементальный diff | `build_index` | Оставить |
| `_get_source_rows` | 377–385 | SELECT строк источника по `track_column` | Инкрементальная выборка | `build_index` | Оставить |
| `_build_search_text` | 387–397 | Текст для эмбеддинга | — | `build_index` | Оставить |
| `_content_hash` | 399–402 | MD5 от `search_text` | Инкрементальный признак «изменилось» | `build_index` | Оставить |
| `_normalize_cols` | 404–423 | Привести `embedding_columns` к списку dict'ов | Нормализация конфига | `build_index`, `_validate_index_config` | Оставить |
| `_rebuild_faiss` | 425–472 | Прогреть FAISS через `CacheProvider.preload_indexes` | Пост-сборка индекса | `build_index:612` | Упростить — `open_cache_provider` (`:443`) без `close()`; `except (ImportError, ModuleNotFoundError)` (`:445`) не ловит `CacheOpenError`/`CacheBusyError` |
| `build_index` | 474–760 | Сборка одного индекса: read → chunk → embed → upsert → FAISS | Ядро утилиты | `main():986` | Оставить |
| `_filter_unchanged` | 762–794 | Режим `--check`: отбросить индексы без изменений | Быстрая проверка при старте | `main():946` | Оставить |
| `main` | 802–1019 | CLI | Точка входа | `__main__:1023` | Упростить — нет параметра `argv` (не тестируется), нет возвращаемого значения, помощник `:823` врёт про `--batch-size`, хардкод `profile="test"` (`:813`) |

**Модульные константы.** `_ROOT` (`:62`) — единственный sys.path-инжект; дальше `utils.db` резолвится только потому, что `lib.services.cache_provider_impl:250` уже добавил `workspace` в `sys.path` — неявная связь, которую легко сломать перестановкой импортов.

**Нет `argv` → не тестируется.** Все 7 кейсов `tests/test_build_vectors_cli.py` тестируют `_validate_index_config` напрямую; `main()` не вызывается ни одним тестом.

---

## `tools/test_audit.py` — 647 LOC

**Назначение.** AST-аудит тестовой suites: собирает по каждому `test_*` метаданные (fixtures, imports, mocks, smells, skip/xfail) и выставляет вердикт KEEP/REWRITE/DELETE/XPASS/INVESTIGATE.
**Что делает.** Обходит `tests/` и `workspace/skills/legal_summarizer/tests/`, парсит AST, пишет `test-audit.json` + `test-audit-summary.txt` в захардкоженный каталог сессии. **Побочный эффект на импорте:** `OUT_DIR.mkdir(parents=True, exist_ok=True)` (`:25`) создаёт директорию при любом `import tools.test_audit`.
**Зачем нужен.** Инструмент одноразовой кампании по quality-аудиту тестов. Покрытия (тестов) нет; потребителей нет; выход лежит в `workspace/data_store/cache/sessions/test-suite-audit-2026-09-10/audit/` — то есть в одноразовом артефакте, а не в репозитории.
**Вердикт.** `Удалить` с предварительной работой
**Обоснование.** Продукт не потребляет, потребителей нет, CI не вызывает, документация не упоминает ни в `AGENTS.md` (только «служебные»), ни в `docs/`, ни в CI. Артефакт его работы лежит в одноразовом каталоге сессии. Содержит 7 неиспользуемых локалей (ruff `F841`), продублированный блок проверки smells, недостижимую ветку `_classify:558`, абсолютный Windows-путь в `:23` и `mkdir` на импорте — файл непереносим и непокрыт. Перед удалением: если анализ нужно воспроизводить, оставить его как отчётный артефакт в `docs/audit/`, а не как исполняемый скрипт. Удаление безопасно: `tests/` его не импортируют (проверено grep по `tests/`).

**Дефекты, фиксирующие вердикт:**

- `:111-135` — блок `if isinstance(t, ast.Compare)` / `if isinstance(t, ast.Call)` (`:111-116`) **дублируется** в `:130-135`. Каждый smell удваивается; `payload["smell_counter"]` (`:687`) завышен ×2.
- `:23` — `REPO = Path(r"C:\Users\Алексей\.nanobot")`. Абсолютный путь с не-ASCII: скрипт не запускается ни у другого разработчика, ни в CI, ни на Linux.
- `:24` — захардкоженный «session key» `test-suite-audit-2026-09-10`: каталог создан 30.09.2026 заново при каждом запуске и содержит устаревшие отчёты.
- `:558-559` — **недостижимый код**: `if "endswith_only" in smells and binding == "UNBOUND": return "DELETE"` не может сработать, потому что более общая проверка `:556-557` возвращает `STRENGTHEN` раньше.
- `:538` — `skip_count > 0 and assert_count == 0` помечается как `XPASS`, что семантически неверно (XPASS = `xfail`, неожиданно прошедший).
- `:732-733` — help для `--strict` обещает «Exit non-zero на any **new** …», но сравнения с baseline нет: «new» не вычисляется.
- `:657` — параметр `summary: bool = False` **не используется** в теле.
- `:420-422` — заглушка `if class_node is not None: pass`; параметр `class_node` фактически не используется. `:424` `referenced_names` вычисляется и выбрасывается.
- `:613-620` — комментарий «self-fake heuristic: …» и вычисляемые `local_names` / `body_assign_names` **никуда не используются** (мёртвый блок).
- `:245, :262, :389` — `mock_specs` инициализируется и не возвращается; `:436` `bad_mock_targets` и `:439` `imported` мертвы (комментарий `:440` — остатки лесакома).
- `:571-593` — `extract_tests_in_file` возвращает **разные типы**: список тестов либо dict-запись с `parse_error`. `main():673` разбирается с этим хаком `if "parse_error" in per_file[0]`.

#### Функции уровня модуля
| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_is_test_call` | 41–47 | Сверка имени вызова с набором | Хелпер | `_is_pytest_skip` | Оставить (внутри удаляемого файла) |
| `_short` | 50–54 | `ast.unparse` с fallback | Хелпер | 12 мест | Оставить |
| `_collect_names` | 57–58 | Все `ast.Name` | Хелпер | `_collect_fixtures_used`, `_summarize` (результат не используется) | Упростить |
| `_function_body_range` | 65–66 | Границы функции | Хелпер | `_summarize` | Оставить |
| `_walk_collect_calls` | 69–70 | Все `ast.Call` внутри функции | Хелпер | `_summarize` | Оставить |
| `_walk_collect_asserts` | 73–74 | Все `ast.Assert` | Хелпер | `_summarize` | Оставить |
| `_is_pytest_skip` | 77–82 | Детект `pytest.skip/skipif/xfail` | Skip-аудит | `_collect_skips` | Упростить — требует `ast.Attribute` (`:78`), голый `skip("x")` не ловится |
| `_has_pytest_marker` | 85–99 | Маркеры pytest из декораторов | Метаданные | `_summarize:631` | Оставить |
| `_collect_assertion_smells` | 102–139 | Классификация assertions | Ядро анализа | `_summarize` | Упростить — дубликат `:130-135` |
| `_check_compare_smell` | 142–149 | `is None` / `is not None` | Слаймы | `_collect_assertion_smells` ×2 | Оставить (после устранения дубля) |
| `_check_call_smell` | 152–165 | `callable/hasattr/endswith`-only | Слаймы | `_collect_assertion_smells` ×2 | Оставить (после устранения дубля) |
| `_compare_lefts` | 168–181 | Нормализация `Compare.left` для 3.12 | Совместимость версий | 4 места | Оставить |
| `_collect_skips` | 184–189 | skip/xfail внутри функции | Метаданные | `_summarize` | Оставить |
| `_collect_silent_excepts` | 192–209 | `except: pass` / `except Exception` без assert/raise | Слайм | `_summarize` | Оставить |
| `_collect_import_fallbacks` | 212–270 | Soft-import guards, превращающие guard-тесты в NOP | Слайм | `_summarize` | Оставить |
| `_detect_self_fake` | 273–361 | Детект self-fake верификации | Слайм | `_summarize` | Оставить |
| `_detect_mock_of_target` | 364–375 | `@patch` на сам тестируемый target | Предупреждение | `_summarize` | Упростить — срабатывает только при `binding == "BOUND"` |
| `_collect_mocks_patches` | 378–413 | Mock/monkeypatch/patch-инвентарь | Метаданные | `_summarize` | Упростить — `mock_targets` (`:384`) и `mock_specs` (`:389`) мертвы |
| `_collect_fixtures_used` | 416–425 | Fixtures по аргументам | Метаданные | `_summarize` | Упростить — `class_node` не используется, `referenced_names` (`:424`) мертва |
| `_collect_production_calls` | 428–461 | Эвристика «production-вызовов» | Метаданные | `_summarize` | Упростить — `bad_mock_targets` (`:436`) и `imported` (`:439`) мертвы |
| `_resolve_imports` | 464–474 | Таблица alias → модуль | Для target-эвристики | `_summarize:581` | Оставить |
| `_production_target_heuristic` | 477–533 | Привязка теста к production-символу | Вход для `_classify` | `_summarize:607` | Оставить |
| `_classify` | 536–562 | Вердикт по smells + target | Выход анализа | `_summarize:649` | Упростить — `:558-559` недостижим |
| `extract_tests_in_file` | 571–593 | Извлечь тесты из файла | Ядро | `main():672` | Упростить — неоднородный тип возврата |
| `_summarize` | 596–650 | Сборка записи по одной функции | Сборка | `extract_tests_in_file` | Упростить — мёртвый блок `:613-620` |
| `main` | 657–726 | Обход + запись отчёта + strict | Точка входа | `__main__:735` | Упростить — `summary` (`:657`) мёртв, `summary_rows` (`:670`) мёртв |

---

## `tools/legacy_audit.py` — 436 LOC

**Назначение.** Архитектурный guard: в production-коде репозитория не должно быть импортов/символов из списка legacy.
**Что делает.** `audit()` обходит репозиторий (от `Path.cwd()`, не от корня проекта!), классифицирует находки по 4 корзинам, `assert_no_legacy()` превращает их в `AssertionError`, `main()` печатает отчёт. Side-effect'ов записи нет.
**Зачем нужен.** Реальный regression-guard: `tests/test_no_legacy_imports.py:44` вызывает `assert_no_legacy()` — это единственная причина, по которой файл жив. Покрыт `tests/test_no_legacy_imports.py` (2 теста, зелёные).
**Вердикт.** `Упростить`
**Обоснование.** Guard работает (0 hits на текущем дереве, тесты зелёные), но три внутренних механизма сломаны: config-guard гасится `JSONDecodeError` на JSONC, allow-list тестовых функций не может сработать никогда, а `main()` дублирует цикл классификации из `assert_no_legacy`.

**Ключевой риск при удалении:** `tests/test_no_legacy_imports.py:44` перестанет работать — тест надо удалять или переносить логику в сам тест.

**Дублирование:** `_FORBIDDEN_MODULES` (`:45`) и весь классификатор дублируют `workspace/skills/legal_summarizer/tests/test_legal_summarizer_no_legacy.py` (170 LOC), при этом docstring `:24-25` называет skill-тест «единым source of truth». Это два независимых списка, которые разъедутся при первой правке.

#### Модульные константы
| Константа | Строки | Назначение | Вердикт |
|---|---|---|---|
| `_FORBIDDEN_MODULES` | 45–68 | Запрещённые legacy-модули | Оставить (дублируется skill-тестом) |
| `_FORBIDDEN_SYMBOLS` | 70–88 | Запрещённые символы | Оставить |
| `_FORBIDDEN_FILES` | 90–118 | Файлы, которых не должно быть на диске | Оставить |
| `_LEGACY_CONFIG_KEYS` | 120–131 | Запрещённые ключи конфига | Упростить — недостижим из-за `JSONDecodeError` (см. ниже) |
| `_ALLOWED_LEGACY_TESTS` | 133–200 | Allow-list: 33 записи `rel::test_name` | Удалить — формат ключа не совпадает с форматом запроса |

#### Функции уровня модуля
| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_iter_python_files` | 203–208 | Обход `*.py` с исключением кэшей | Обход | `audit:305` | Оставить |
| `_is_test_file` | 210–213 | Файл внутри test-каталога | Отделить тесты от production | `audit:311` | Оставить |
| `_is_allowed_legacy_test` | 215–242 | Allow-list для legacy-ссылок в тестах | Разрешить fixture-ссылки | `audit:322, :356` | Упростить — `:240` строит `f"{rel}::{line_no}"`, а ключи содержат имя функции → **всегда False** (проверено). Ветка `line_no is None` (`:236`, для `_FORBIDDEN_FILES`) рабочая |
| `audit_legacy_in_module` | 244–285 | Классифицировать находки одного файла | Ядро анализа | `audit:313` | Оставить |
| `audit` | 287–319 | Обход репо → dict 4 корзин | Точка сбора | `assert_no_legacy`, `main`, внешние вызовы | Упростить — `skill_root` (`:287`) принимается и **не используется**; корень берётся из `Path.cwd()` (`:290`), т.е. скрипт работает только из корня репозитория |
| `_is_production_file` | 321–323 | Не test и не tools | Фильтр | `assert_no_legacy:333` | Оставить |
| `assert_no_legacy` | 325–411 | Guard-обёртка: 4 assert'а | Контракт для теста | `tests/test_no_legacy_imports.py:44` | Упростить — блок `:369-378` (config-guard) мёртв: `json.loads(project.json)` (`:371`) падает на JSONC и глушится на `:377` |
| `main` | 414–436 | CLI-отчёт | Точка входа | `__main__` | Упростить — цикл классификации `:427-434` дословно повторяет `:334-360` |
| `__all__` | 475 | Публичный API | — | — | Оставить |

**Проверено:** `python -m pytest tests/test_no_legacy_imports.py -q` → `2 passed`; `audit()` возвращает `{}` (0 hits), поэтому сломанный allow-list и мёртвый config-guard **сейчас не проявляются** — это латентные дефекты, а не текущие падения.

---

## `tools/diagnose_startup.py` — 386 LOC

**Назначение.** Парсер startup-лога gateway/CLI и сверка фактов с каноническим инвентарём `lib.services.runtime_inventory`.
**Что делает.** Извлекает секции `Hooks connected` / `Registered N tools` / `Custom (project) tools` / `Runtime patches` регулярными выражениями, строит `StartupFacts`, зовёт `diff_hooks` / `diff_project_tools` / `diff_runtime_patches`, рисует rich-панели. Читает лог из файла или stdin. Exit 0/1/2.
**Зачем нужен.** Прод-процедура: `CHANGELOG.md:94` даёт рецепт `gateway.py > gateway.log && diagnose_startup.py --log gateway.log`. Покрыт `tests/test_diagnose_startup.py` (7 кейсов, включая CLI через subprocess). Ловит дрейф startup-инвентаря, который иначе виден только в баннере `ApplicationContext`.
**Вердикт.** `Оставить`
**Обоснование.** Единственный инструмент, который делает `runtime_inventory` наблюдаемым; тестируется, документирован, имеет корректные exit codes. Единственная претензия — качество данных на его входе (находка 17), это баг `lib/`, а не скрипта.

**Скрытый контракт.** 9 регулярных выражений (`:57-66`) привязаны к **точному тексту** баннеров `ApplicationContext`/`RuntimePatcher`. Любая правка формата в `lib/` тихо ломает парсер — вместо DRIFT инструмент начнёт рапортовать `OK` по пустому `facts`. Это не enforced ни тестом на стороне `lib/`, ни контрактом; `tests/test_diagnose_startup.py` тестирует парсер на фикстурах, а не на реальном выводе `ApplicationContext`.

#### Функции уровня модуля
| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `parse_startup_log` | 95–185 | Regex-парс лога в `StartupFacts` | Ядро | `main`, тесты | Оставить |
| `diff_against_canonical` | 187–217 | Сверка с `runtime_inventory` | Ядро | `render_report`, `main` | Оставить |
| `_print_panel` | 220–238 | rich `Panel` / fallback | UI | `render_report` | Оставить |
| `render_report` | 240–348 | Человекочитаемый отчёт OK/DRIFT/CRITICAL | UI | `main` | Оставить |
| `main` | 350–386 | CLI: `--log`/`--strict`/`--json`/`--no-color` | Точка входа | `__main__` | Оставить |

#### Класс `StartupFacts` (строки 70–92, 11 полей, 1 свойство)
Дата-класс «что распаросилось из лога»: `hook_names`, `hook_factory_count`, `builtin_tool_names`, `project_tools_registered/disabled/failed`, `runtime_patches_applied/skipped/failed`, `runtime_patches_failed_header`, `parse_errors`. Все поля имеют `default_factory=list`, поэтому объект валиден и при неудачном парсе.
**Вердикт.** `Оставить`

| Свойство/метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `is_empty` | 85–92 | «Не удалось распознать ни одной секции» | Отсечка пустого лога до вывода панелей | `parse_startup_log:177`, `main:430` | Оставить. **Замечание:** учитывает только 4 из 11 полей — `project_tools_disabled/failed`, `runtime_patches_skipped/failed` и `parse_errors` в проверку не входят, поэтому лог, где распознались только disabled-tools, будет считаться «пустым» |

#### Вложенные функции в `render_report` (строки 256–269, 4 шт.)
Четыре локальных помощника, замыкающие `console` (`:254`): `_line` печатает через rich или `print` в зависимости от доступности `Console`; `_ok`/`_warn`/`_err` добавляют цветовые маркеры `[green]OK` / `[yellow]DRIFT` / `[red]CRITICAL` (или их plain-версии без rich). Нужны, чтобы не дублировать тернарник `console if console else …` в 12 местах отчёта.
**Вердикт.** `Оставить` (все 4)

---

## `tools/check_indexes.py` — 245 LOC

**Назначение.** Declared-vs-runtime diff по векторным индексам: `project.json` (desired) против `storage_table` в DuckDB-кэше (actual).
**Что делает.** Читает декларацию через `read_vector_index_config()`, открывает `CacheProvider` в `READ_ONLY`, зовёт `list_runtime_vector_indexes(provider)`, сравнивает по имени и по сигнатуре индекса. Ничего не пишет. Exit 0/1/2.
**Зачем нужен.** Ловит «векторы не собраны» / «сигнатура устарела» до того, как это станет пустым `search_vector`. Покрыт `tests/test_check_indexes.py` (17 кейсов). Документирован в `docs/MIGRATION.md:23, :57`, `CHANGELOG.md:722`.
**Вердикт.** `Оставить`
**Обоснование.** Корректно реализует единственный разрешённый слой (`lib.services.cache_provider_impl` — единственный источник runtime-индекса, дублирования нет), покрыт тестами, exit codes задокументированы и соответствуют `AGENTS.md:54`.

**Мелкие дефекты:** (1) `_load_runtime:190` открывает провайдера и не вызывает `close()`; (2) `compute_index_signature` импортируется **внутри функции** внутри `try/except Exception: pass` (`:121-124`) — при том что соседние импорты уже на модульном уровне (`:50`); (3) docstring `:12` обещает «точка входа для CI» — в `.github/workflows/ci.yml` скрипта нет; (4) `main():261` хардкодит `profile="test"` с обоснованием в комментарии `:254-257` (тот же хардкод, что и в `build_vectors.py`).

#### Функции уровня модуля
| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_diff` | 58–166 | Чистая функция сравнения declared/runtime | Тестируется | `main:265`, `tests/test_check_indexes.py` | Оставить |
| `_load_declared` | 169–173 | Декларация или исключение | Вход `_diff` | `main:263` | Оставить |
| `_open_provider` | 176–185 | `open_cache_provider(READ_ONLY)` | Единственная точка создания | `_load_runtime:190` | Оставить (+ `close()` в вызывающем) |
| `_load_runtime` | 188–196 | Runtime-список или исключение | Вход `_diff` | `main:264` | Оставить |
| `_format_text` | 199–239 | Текстовый отчёт | UI | `main:271, :277` | Оставить |
| `main` | 242–281 | CLI `--json` | Точка входа | `__main__:285` | Оставить |

---

## `tools/generate_comments_sql.py` — 270 LOC

**Назначение.** Сгенерировать `sql/comments/apply_all_comments.sql` — `COMMENT ON TABLE/COLUMN` для 7 групп таблиц.
**Что делает.** Читает внешний дамп `workspace/skills/audit_analyzer/cache/schema.json` (домен `oarb`) и добавляет 6 захардкоженных словарей (`public.agent_predefined_scripts`, `oarb.audit_vectors`, `public.agent_vector_index_config`, `public.agent_session_meta/_messages`, `public.agent_conversation_messages`, `public.agent_question_runs`, `public.agent_gateway_logs`, `public.agent_benchmark_runs/_results`), затем пишет SQL. **Вся работа — на верхнем уровне модуля:** нет `main()`, нет `if __name__ == "__main__"`, поэтому `import tools.generate_comments_sql` пишет файл.
**Зачем нужен.** Разовая задача документирования схемы комментариями в БД. Результат (`sql/comments/apply_all_comments.sql`) закоммичен; повторная генерация требует ручного дампа схемы, штатного производителя которого в репозитории нет (признано в docstring `:4-6`).
**Вердикт.** `Упростить`
**Обоснование.** Пайплайн верен, но в текущем состоянии не запускается (входного файла нет), весь код вне функций, выход устарел относительно схемы. Минимальная починка: обернуть в `main(argv)`, читать `schema.json` из аргумента с понятной ошибкой, добавить недостающие таблицы/колонки.

**Устаревание выхода (проверено):** в `sql/comments/apply_all_comments.sql` нет ни `COMMENT ON COLUMN public.agent_gateway_logs.user_id` (добавлена `sql/migrations/V004__agent_gateway_logs_user_id.sql`), ни комментариев для `public.agent_worker_claims` (таблица есть в `sql/workers/`, в семи словарях отсутствует). `AGENTS.md:54` описывает скрипт как обычную утилиту, без оговорки про ручной входной файл.

#### Функции уровня модуля
| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_sql_escape` | 16–17 | Экранирование `'` | Инъекция в DDL | `render_table` ×2 | Оставить |
| `render_table` | 20–29 | COMMENT для таблицы и колонок | Ядро | `сборка:288` | Упростить — параметр `name` (`:20`) равен `full` на единственном вызове (`:288`), т.е. мёртв |
| 6 словарей + чтение schema.json | 33–268 | Данные описаний | Вход рендера | `сборка:284-285` | Оставить как данные; **дополнить** `user_id` и `agent_worker_claims` |
| Сборка и запись | 271–291 | Запись файла | Выход | `__main__`-отсутствует, выполняется на импорте | Упростить — обернуть в `main()` |

**Прочее:** нумерация блоков в комментариях пропускает «6» (`:175` → `:177`); идентификаторы не экранируются (контролируемый генератор, приемлемо).

---

## `tools/validate_component_specs.py` — 224 LOC

**Назначение.** Валидатор структуры OpenSpec component-spec'ов по шаблону `openspec/specs/architecture/component-model/spec.md`.
**Что делает.** Обходит `openspec/specs/**/spec.md`, проверяет 9 обязательных и 6 опциональных разделов, наличие `### Требование:` и сценария `#### Сценарий:` с маркерами `КОГДА/ТОГДА`, парсит `COMPONENTS.md` и сверяет наличие `spec.md` для компонентов со статусом ≠ `missing`. Exit 0/1/2. Пишет только в stdout.
**Зачем нужен.** Guard структуры документации. Упомянут как обязательный шаг в `docs/README.md:116` («`python tools/validate_component_specs.py`»). Покрытия тестами нет.
**Вердикт.** `Упростить`
**Обоснование.** Нужен как задокументированная процедура, но логика содержит избыточность: блок проверки кириллицы в заголовке (`:110-118`) — тавтология (все константы в `REQUIRED_SECTIONS`/`OPTIONAL_SECTIONS` уже кириллические, `CYRILLIC_RE` не может сработать), а `--strict` (`:262-269`) не меняет результат: `return 1` в обоих ветках.

**Скрытый контракт / риск ложных срабатываний:** `validate_registry` (`:196`) ищет spec через `specs_dir.glob(f"*/{_slug_to_kebab(name)}/spec.md")`, затем fallback `specs_dir.glob(f"*/{name}/spec.md")`. Точное отображение «имя компонента в `COMPONENTS.md` → путь каталога» не гарантировано, и при rename компонента валидатор начнёт требовать несуществующий путь. `VALID_STATUSES` (`:67`) включает `deprecated`, но `validate_registry` его не пропускает — deprecated-компонент без `spec.md` будет отмечен как нарушение.

#### Класс `SpecIssue` (строки 70–79, 1 метод)
Проблема валидации одного spec'а: `spec_path` + `section` + `message`. Нужен как носитель нарушения до печати.
| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `render` | 78–79 | Человекочитаемая строка нарушения | Вывод | `main:256` | Оставить |

#### Класс `ValidationReport` (строки 82–96, 1 property + 1 метод)
Сводный накопитель: счётчик проверенных spec'ов + 3 списка проблем.
| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `ok` (property) | 91–93 | Есть ли хоть одна проблема | Гейт exit code | `main:250` | Оставить |
| `add` | 95–96 | Добавить `SpecIssue` | Сбор | 5 мест | Оставить |

#### Функции уровня модуля
| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `iter_spec_files` | 99–101 | Обход `**/spec.md` | Вход | `main:236` | Упростить — `for … yield` → `yield from` (ruff `UP028`) |
| `validate_spec_sections` | 104–155 | Проверка разделов/требований/сценариев | Ядро | `main:239` | Упростить — `:110-118` тавтологичен; `:120` `or "## Requirements" in text` при `"## Требования" in text or …` — избыточно |
| `parse_components_registry` | 158–184 | Разбор `COMPONENTS.md` | Вход `validate_registry` | `main:234` | Оставить |
| `validate_registry` | 187–205 | Проверка наличия `spec.md` | Ядро | `main:241` | Упростить — не пропускает `deprecated` |
| `_slug_to_kebab` | 208–211 | CamelCase → kebab | Fallback поиска | `validate_registry:196` | Оставить |
| `main` | 214–270 | CLI `--verbose`/`--strict` | Точка входа | `__main__:274` | Упростить — `--strict` (`:262-269`) не меняет exit code (обе ветви → `return 1`) |

---

## `tools/migrate.py` — 214 LOC

**Назначение.** Runner версионных миграций `sql/migrations/V*.sql` с таблицей учёта `public.schema_migrations` и SHA256-контролью дрейфа.
**Что делает.** Discover по имени файла → checksum (без `--`-комментариев и пустых строк) → сравнение с `schema_migrations` → применение в транзакции с записью версии. Режимы `--status/--dry-run/--verify/--baseline/--apply`, `--target`, `--force`. Пишет в БД.
**Зачем нужен.** **Load-bearing.** На него ссылается не только документация (`sql/README.md`, `docs/DATABASE.md:369, :453-457`, `docs/MIGRATION.md:45, :274`), но и продуктовый код: `lib/services/schema_validation.py::_hint_for_profile` печатает «python tools/migrate.py --apply» в сообщении об ошибке при старте gateway (`CHANGELOG.md:117`). Удаление ломает подсказку пользователю и процедуру деплоя.
**Вердикт.** `Оставить` + `Слить с `workspace/utils/db.py``
**Обоснование.** Единственный поддерживаемый путь миграции схемы, документирован в четырёх местах и упомянут в рантайм-подсказке. `resolve_dsn` (`:75-84`) — дубликат; остальное функционально.

**Отсутствие тестов.** Ни одного теста на `compute_checksum` / `discover` / `cmd_status` / `cmd_apply` при том, что `discover` поднимает `SystemExit` (`:69`) и `cmd_apply` мутирует БД. Логика версий и drift — самая опасная часть инструмента, и она не покрыта.

#### Класс `Migration` (строки 42–48, 0 методов)
Frozen-dataclass: `version`, `name`, `path`, `checksum`, `sql`. Нужен как носитель данных discover'а.
**Вердикт.** `Оставить`

#### Функции уровня модуля
| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `compute_checksum` | 51–56 | SHA256 нормализованного SQL | Drift-детект | `discover:71` | Оставить |
| `discover` | 59–72 | Найти и отсортировать миграции | Вход всего | `main:220` | Оставить (`SystemExit` вместо исключения — приемлемо для CLI) |
| `resolve_dsn` | 75–84 | DSN из env или конфига | Подключение | `main:229` | **Слить с `workspace/utils/db.py`** — env-ветка мертва, т.к. `project.json:44` = `${DATABASE_URL}` |
| `ensure_tracking_table` | 87–100 | `CREATE TABLE IF NOT EXISTS schema_migrations` | Инициализация учёта | `main:231` | Оставить |
| `fetch_applied` | 103–109 | Прочитать применённые версии | Вход команд | `main:233` | Оставить |
| `apply_migration` | 112–134 | Одна миграция в транзакции + INSERT записи | Атомарность | `cmd_apply:193` | Упростить — параметр `force` (`:112`) **не используется в теле**; вызывается с `force=force` |
| `stamp_migration` | 137–147 | Записать версию без выполнения | `--baseline` | `main:241` | Оставить |
| `cmd_status` | 150–165 | Таблица состояния | `--status` | `main:235` | Упростить — `extra`/«ORPHAN?» (`:160-162`) печатается, но на exit code (`:163-165`) не влияет |
| `cmd_apply` | 168–195 | Применение pending + drift-гейт | `--apply` | `main:245` | Упростить — `:177` и `:239` сравнивают `version` (строку) с `target` (строкой) лексикографически; сломается на 10+ миграциях |
| `cmd_verify` | 198–205 | Сверка checksum'ов | `--verify` | `main:237` | Оставить |
| `main` | 208–248 | CLI | Точка входа | `__main__:252` | Упростить — `return 2` (`:246`) **недостижим**: группа флагов `required=True` и mutually exclusive (`:210`) |

---

## `tools/check_worker_pool_integrity.py` — 165 LOC

**Назначение.** Диагностика инварианта `processing ⇔ claim` между `agent_conversation_messages` и `agent_worker_claims`, с опциональным `--fix`.
**Что делает.** 5 read-only SQL-проверок через глобальный пул `utils.db.run`; при `--fix` — `UPDATE`/`DELETE` по 4 правилам в одной транзакции. Exit 0/1.
**Зачем нужен.** Процитирован в `docs/TROUBLESHOOTING.md:10` («целостность пула воркеров — `python tools/check_worker_pool_integrity.py --fix`») и `:158` (таблица быстрых рецептов). Указан в `AGENTS.md:54`. Покрытия тестами нет.
**Вердикт.** `Упростить`
**Обоснование.** Нужен как runbook-инструмент, но `--fix` содержит SQL, который не проверен, а часть диагностики гарантированно пуста.

**Дефекты:**
1. `:188` — `DELETE FROM {claims} c WHERE …` с псевдонимом. В Greenplum 6.5 (основа — PostgreSQL 8.3, см. `docs/DATABASE.md` и упоминание Greenplum 6.5 в `AGENTS.md`) конструкция `DELETE FROM tbl alias` не поддерживается → синтаксическая ошибка на 4-й из 5 команд `_fix`. **Требует проверки на целевой БД** (у меня нет подключения). Соседние `DELETE` в этом же файле (`:179`) сделаны без псевдонима — вероятная причина, почему баг не замечен.
2. `:124-131` — проверка №4 «несколько claim на одну задачу» **не может дать результата**: `agent_worker_claims` имеет UNIQUE PK на `task_id` (это и заявлено в docstring `:9`), т.е. `HAVING count(*) > 1` всегда пусто. Мёртвая диагностика.
3. `:49-52` — `_connect()` — бессмысленная обёртка: имя говорит «connect», возвращает функцию `run`. Дублируется как `tools/check_indexes.py::_open_provider` в `duplicates.md:171` (обе — 4-10 LOC тривиадии). Упростить до `from utils.db import run` в `main`.
4. `:55` — `main()` без параметра `argv` → не тестируется.
5. Имена таблиц из `--messages`/`--claims` интерполируются в SQL через f-string (`:97, :107, :117, :128, :137`) — обход `lib/utils/sql_safety`. Для локального админского инструмента некритично, но стоит валидировать `[a-z_][a-z0-9_]*` и требовать `schema.table`.

#### Функции уровня модуля
| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_connect` | 49–52 | Вернуть `utils.db.run` | Хелпер | `main:87` | Упростить — удалить обёртку |
| `main` | 55–158 | CLI `--messages/--claims/--fix` | Точка входа | `__main__:201` | Упростить — без `argv`; проверить SQL `:188` |
| `check(sql)` (вложена) | 90–91 | Выполнить SELECT через `run` | Хелпер | `вложена в main`, 5 мест | Оставить |
| `_rows` | 161–166 | RealDictCursor → list[dict] | Хелпер | `check` | Оставить |
| `_fix` | 169–197 | Reclaim + heal + чистка | `--fix` | `main:153` | Упростить — исправить `:188` |

---

## `tools/legal_benchmark.py` — 166 LOC

**Назначение.** Benchmark-сравнение стратегий исполнения unified execution planner'а по token/call-эффективности на 4 сценариях размера документа.
**Что делает.** Ничего. Все 10 импортов (`:33-60`) — верхнеуровневые `chunking.* / document.* / planning.* / llm.*` без префикса; `sys.path` не настраивается; `python tools/legal_benchmark.py` падает на первом импорте. `run_benchmark` (`:114`) не имеет ни одного вызывающего, `main()` и `if __name__ == "__main__"` в файле нет.
**Зачем нужен.** Не нужен. Ссылок нет: grep по `docs/`, `README.md`, `CHANGELOG.md`, `tests/`, CI — только упоминание в `AGENTS.md:54` («`legal_benchmark.py` — бенчмарк legal_summarizer»). Реальный бенчмарк-подсистема живёт в `benchmarks/` (runner/evaluator/scorer/reporter) и покрывается `benchmarks/`.
**Вердикт.** `Удалить`
**Обоснование.** Файл физически неработоспособен (проверено: `ModuleNotFoundError: No module named 'chunking'`), не имеет entrypoint, продублирован функционально подсистемой `benchmarks/`, а его единственная ссылка — строка в `AGENTS.md`, которую нужно будет убрать. Перед удалением: исправить/удалить строку `AGENTS.md:54`; проверить, не используется ли `PLAN §51` как живой контракт где-то ещё (в репозитории ссылок на `§51`/`§60` нет — оба файла ссылаются на внешний документ).

**Дополнительно (усиливает вердикт):**
- Docstring (`:17-26`) описывает бенчмарки, не соответствующие коду: заявлены «small: 1 chunk (≤ 12000 chars)», «very_large: 1000 chunks (~1M chars, 20 sections)», тогда как `very_large_scenario` (`:176`) строит 25 секций × ~3 600 символов ≈ 90 тыс. символов. Числа в docstring выдуманы/устарели.
- 3 мёртвые локали (ruff `F841`): `body` (`:96`), `para` (`:99`), `estimator` (`:119`).
- `_build_chunks_and_struct` принимает неиспользуемый `tmp_path_factory=None` (`:90`) и создаёт временный `.docx` через `tempfile.NamedTemporaryFile(delete=False)` (`:100-104`) **без удаления** — утечка файлов в tempdir.
- Ruff-диагностика того же класса, что и в `legal_summarizer`: `legal_summarizer_final_audit.md:254` описывает `scripts/structure/architecture_guard.py` skill-уровневого guard'а — вероятно, `tools/architecture_guard.py` и `tools/legal_benchmark.py` являются осиротевшими копиями артефактов skill-а (см. §архитектурный вывод ниже).

#### Класс `BenchmarkMetrics` (строки 63–77, 0 методов)
Frozen-dataclass из 12 счётчиков. Нужен только для возврата из `run_benchmark`. **Вердикт.** `Удалить` (с файлом)

#### Класс `BenchmarkScenario` (строки 80–86, 0 методов)
Frozen-dataclass сценария: `name`, `text`, `n_sections`. **Вердикт.** `Удалить` (с файлом)

#### Функции уровня модуля
| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_build_chunks_and_struct` | 89–111 | Синтез `.docx` → structure → chunks | Сборка входа | `run_benchmark:116` | Удалить (файл) |
| `run_benchmark` | 114–139 | Прогнать 1 сценарий, вернуть метрики | Ядро | **никто** | Удалить (файл) |
| `small_scenario` | 142–147 | Фабрика сценария | Данные | никто | Удалить (файл) |
| `medium_scenario` | 150–160 | Фабрика сценария | Данные | никто | Удалить (файл) |
| `large_scenario` | 163–173 | Фабрика сценария | Данные | никто | Удалить (файл) |
| `very_large_scenario` | 176–186 | Фабрика сценария | Данные | никто | Удалить (файл) |

---

## `tools/audit_nanobot_contracts.py` — 169 LOC

**Назначение.** AST-аудит зависимостей нашего кода от `nanobot`: резолвит `from nanobot.X import Y` и атрибут-доступ и репортит MISSING.
**Что делает.** Обходит `lib/`, `workspace/`, `tools/`; для каждого импорта зовёт `_walk_dotted`, который **в процессе делает `setattr(cur, parts[i-1], mod)` по объектам реально импортированного `nanobot`** (`:63-67`) — то есть инструмент мутирует проверяемую библиотеку в своём процессе. Пишет JSON при `--json`.
**Зачем нужен.** Заявлен как «первая линия обороны при апгрейдах nanobot» (`:15-16`), но в CI не подключён: джоб `Upgrade readiness (contract tests)` (`.github/workflows/ci.yml:84-106`) выполняет `pytest tests/contract/`. Упоминание в `CHANGELOG.md:107, :148` — историческое. Тестов нет.
**Вердикт.** `Упростить`
**Обоснование.** Идея полезна (на `CHANGELOG.md:148` зафиксировано, что из 23 MISSING реальными были 4), но в текущей форме инструмент ненадёжен и не используется: он признаёт свои ложные срабатывания (`:175-176`), всегда возвращает exit code 0, дедуплицирует только при печати, но не в `--json`, и мутирует `nanobot`. Утилита, которая не может ни провалить пайплайн, ни дать достоверный список, дешевле как подсказка, чем как скрипт.

**Риск при upgrade `nanobot`:** мутация `sys.modules`-объектов делает повторные вызовы в одном процессе неидемпотентными и способна замаскировать реальный `ImportError` (искусственно созданный атрибут будет найден).

#### Функции уровня модуля
| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_iter_py_files` | 34–45 | Обход `SCAN_DIRS` | Вход | `audit:90` | Упростить — `import os` внутри функции (`:35`) |
| `_walk_dotted` | 48–69 | Резолв dotted-пути через `importlib`+`getattr` | Ядро | `audit:112, :146` | Упростить — **удалить `setattr` на `:65`** (побочный эффект, ломающий идемпотентность) |
| `_kind` | 72–85 | Тип объекта строкой | Вывод | `audit:119, :154` | Оставить |
| `audit` | 88–156 | Обход + findings | Ядро | `main:164` | Упростить — findings не дедуплицируются (только при печати, `:173-184`); per-function повторный обход `:122-136` дублирует модульный `:100-109` |
| `main` | 159–190 | CLI `--json` | Точка входа | `__main__:194` | Упростить — нет ненулевого exit code при `MISSING`; дедуп только в stdout-ветке |

---

## `tools/scan_nanobot_inventory.py` — 153 LOC

**Назначение.** Генератор `docs/architecture/nanobot-inventory.json` — инвентарь зависимостей от `nanobot` с классификацией GREEN/YELLOW/ORANGE/RED.
**Что делает.** Регулярками (не AST!) собирает `from nanobot… import`, `getattr(obj, "_private")`, `setattr(...)`; классифицирует; пишет JSON с `_meta`/`summary`/`files`.
**Зачем нужен.** Питает `docs/architecture/nanobot-inventory.{md,json}` — документ, на который ссылается `AGENTS.md` («инвентаризация зависимостей»). Это read-only инструмент upgrade-readiness.
**Вердикт.** `Упростить`
**Обоснование.** Актуален — это единственный генератор `.json`-инвентаря, и он **воспроизводим**: контрольный прогон в `$TMPDIR` дал `total_imports: 65`, `RED: 6 / GREEN: 49 / YELLOW: 1 / ORANGE: 9`, `getattr_private_total: 22`, `setattr_total: 1` — полное совпадение с закоммиченным файлом; расходится только `files_scanned` (188 → 187, ушёл один файл из двух последних MCP-коммитов). Дрейф, зафиксированный другим аудитором, относится к **рукописному** `nanobot-inventory.md`, а не к этому артефакту. Нужны только мелкие правки мёртвого кода и хардкода версии.

**Дефекты:**
1. `:148` — `"nanobot_version_pinned": "0.3.0"` **захардкожен**, а фактический пин — `requirements.txt:11: nanobot-ai==0.3.5` (и установлен 0.3.5). Поле в закоммиченном JSON (`"0.3.0"`) и в сканере расходится с реальностью.
2. `:135, :137` — fallback `"0.3.0 (pinned)"` устарел по той же причине.
3. `:25` — `DIRECT_PRIVATE_RE` **объявлен и нигде не используется**.
4. `:26-28` — `SETATTR_RE` **объявлен и нигде не используется**; `scan_file:112` дублирует ту же регулярку инлайном.
5. `:100-103` — `elif` с телом `pass`: условие ложно по построению (любой `n.startswith("_")` уже поймал выше), т.е. мёртвая ветка.
6. `:68-70` — комментарий «продолжение многострочного from-import» стоит над `continue`, который этого не делает; фактическая обработка — в `:85-103`.
7. `:94-96` — `if n != ")"` тавтологично: `re.findall(r"[A-Za-z_][\w]*")` никогда не возвращает `")"`.
8. Версия берётся из `subprocess` с `sys.executable` (`:132`) → результат зависит от того, каким интерпретатором запущен скрипт (в закоммиченном JSON `installed: "0.3.0"`, при прогоне из текущего venv — `0.3.5`). Воспроизводимость артефакта не гарантирована.
9. `main()` без `argv`; exit code всегда 0 (приемлемо для генератора).

#### Функции уровня модуля
| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `iter_py_files` | 31–42 | Обход `SCAN_PATHS` | Вход | `main:139` | Оставить |
| `classify` | 45–59 | GREEN/YELLOW/ORANGE/RED | Ядро | `scan_file:80` | Оставить (маркеры `internal_markers` на `:49-54` — хардкод внутренних модулей nanobot, требует ревизии при upgrade) |
| `scan_file` | 62–122 | Regex-обход одного файла | Сбор | `main:139` | Упростить — удалить мёртвый `elif` `:100-103`, дублирующую регулярку `:112` |
| `main` | 125–172 | CLI `--out` | Точка входа | `__main__:176` | Упростить — исправить хардкод версии (`:148, :135`) |

---

## `tools/smoke_post_cleanup.py` — 147 LOC

**Назначение.** Smoke-прогон gateway для opencode change `post-0.3.5-patches-cleanup`: поднимает `gateway.py --profile=test`, проверяет отсутствие `_last_usage` в рантайм-логах и наличие `turn_completed` в `agent_gateway_logs_test`.
**Что делает.** `subprocess.Popen` gateway, `time.sleep(8)`, читает stdout после `terminate()`, затем **открывает psycopg2-соединение напрямую** (не через `utils.db`) и делает 3 SELECT'а к `public.agent_gateway_logs_test`.
**Зачем нужен.** Не нужен: change завершена и архивирована (`openspec/changes/archive/2026-09-27-post-0.3.5-patches-cleanup/tasks.md`, задачи 6.3/8.3/8.4 помечены `[x]`; задача 6.3 прямо ссылается на этот скрипт как на выполненную проверку). Других потребителей нет. Ссылки в `AGENTS.md:54` — обезличенные («smoke-прогон»).
**Вердикт.** `Упростить` либо `Удалить`
**Обоснование.** Как проверка — выполнена и задокументирована в архиве change. Как инструмент — нерабочий: главное условие docstring не может провалить прогон, хардкод DSN с паролем в исходнике (`:31`), жёстко зашитый `--profile=test` (`:33`) без флага, устаревшая ссылка на несуществующий путь change (`:16`). Если команда нужна вновь (следующий upgrade nanobot) — её следует переписать на `utils.db`, сделать проверки блокирующими и параметризовать профиль; иначе удалить вместе с обновлением `AGENTS.md:54`.

**Дефекты (все подтверждены чтением):**
- `:118-123` — при недоступном Postgres возвращает **0** (успех), хотя БД-проверка была core-контрактом.
- `:141-153` — отсутствие `turn_completed` даёт `WARN`, а не ненулевой код.
- `:92-110` — проверка `_last_usage` в stdout gateway; сам символ в `lib/` встречается только в docstring'ах `runtime_patcher.py:110, :139` (как запрещённый fallback), т.е. проверка тривиально зелёная и не имеет диагностической ценности.
- `:112-116` — `'RuntimeEventsSubscriber' in stdout` → `WARN`, не влияет на код возврата.
- `:126-129, :135-139, :155-159` — таблица `public.agent_gateway_logs_test` захардкожена, хотя `logging.db.table_name` настраивается (профиль `test` переопределяет его).
- `:124, :140, :160` — `cur`/`conn` без `try/finally`; при исключении в `cur.execute` соединение не закрывается.

#### Функции уровня модуля
| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_extract_event_type` | 37–43 | Нормализация psycopg2 JSON → строка | Устойчивость к типу jsonb | **никто** | Удалить — функция не вызывается ни в одном из 3 SELECT'ов |
| `main` | 46–172 | Прогон gateway + запросы к БД | Точка входа | `__main__:176` | Упростить / Удалить |

---

## `tools/release_v252.py` — 136 LOC

**Назначение.** Публикация GitHub Release для тега `v2.5.2` через `gh release create` (fallback — печать curl-команды).
**Что делает.** `TAG`/`TITLE`/`BODY` (release notes) зашиты в исходнике; `--dry-run` (по умолчанию) печатает JSON payload в stdout; `--curl` пишет body во временный файл и печатает curl-строку; `--run` вызывает `gh` с временным payload-файлом, удаляемым в `finally`.
**Зачем нужен.** Нужен как повторно используемая процедура: `docs/RELEASE.md` §1.5, §2, §3, §5, §7 ссылается на него как на рабочий инструмент публикации. Текущая версия `project.json::project.version = 2.5.2` (`:33`) совпадает с `TAG`.
**Вердикт.** `Оставить` + `Упростить`
**Обоснование.** Повторно используемая процедура, документированная как шаг релиза; удалять нельзя до выхода 2.5.3. Но содержимое `BODY` (`:38-72`) **описывает удалённую архитектуру**: `DuckDbCacheStore.publish()` (по `AGENTS.md:16` метода `publish()` больше нет), `sync_publish_failed` / `initial_load` / `sync_registry_initial_load_publish` (относятся к удалённым `pg_duckdb_sync_service`/`cache_ownership`, см. `AGENTS.md:19-20` и change `drop-local-cache-read-from-pg`). То есть release notes, попавшие бы в GitHub Release, описывают несуществующий код.

**Дефекты:**
- `:9-10` (docstring) — «без флагов — вызывает `gh release create`» **противоречит** `:13-14` и реализации `main():158-165` (по умолчанию dry-run). Устаревший текст от v2.5.1.
- `:35-36` — `CHANGELOG_BLOCK_HEADER` и `NEXT_BLOCK_HEADER` объявлены и **нигде не используются** (мёртвые константы, перенесённые из v251).
- `:105-122` — `try/finally: pass`: `finally` не делает ничего, а комментарий «Удалим через 1 час» не реализован. При этом файл с телом релиза остаётся в `tempfile.gettempdir()` намеренно.
- `:6-7` (docstring) — «ничего не создаёт и не пишет на диск» противоречит `:102-104`, где `_print_curl` пишет файл на диск.
- `:31` — `REPO = "AlexEgorov85/workspaces_nanobot"` захардкожен (приемлемо для скрипта релиза, но продублировано в `docs/RELEASE.md:173, :187`).
- Перед следующим релизом `docs/RELEASE.md:86` (`cp tools/release_v251.py tools/release_v252.py`) надо заменить на `cp tools/release_v252.py tools/release_v253.py` — иначе инструкция скопирует устаревший образец.

#### Функции уровня модуля
| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_payload` | 75–76 | Сборка dict payload | Ядро | `_print_payload`, `_print_curl`, `_run_gh` | Оставить |
| `_print_payload` | 79–89 | Печать JSON в stdout, без записи на диск | `--dry-run` | `main:164` | Оставить (корректно реализует обещание) |
| `_print_curl` | 92–122 | Напечатать curl-команду | Fallback без `gh` | `main:161` | Упростить — удалить `try/finally: pass` (`:105-122`) |
| `_run_gh` | 125–144 | `gh release create` с temp-payload | Боевой запуск | `main:159` | Оставить (temp-файл + `finally: unlink` — корректно) |
| `main` | 147–165 | CLI `--dry-run/--curl/--run` | Точка входа | `__main__:169` | Оставить (безопасный дефолт dry-run) |

---

## `tools/release_v251.py` — 94 LOC

**Назначение.** То же, что v252, но для тега `v2.5.1` (релиз выпущен 2026-09-13, `CHANGELOG.md:848`).
**Что делает.** `_payload_path()` (`:74-80`) **пишет `release_v251_payload.json` в текущий каталог** (CWD) при каждом вызове; `main():109-114` в ветке `--dry-run`/`--curl` этот файл создаёт и **не удаляет**. `_print_curl(token)` (`:56`) принимает неиспользуемый параметр и получает литерал `"${GITHUB_TOKEN}"` (`:113`).
**Зачем нужен.** `docs/RELEASE.md:83` предписывает: «**Не редактировать старый** (`tools/release_v251.py` уже сделал свою работу — оставь как исторический маркер)». §1.5 называет его образцом для копирования. Это осознанное проектное решение, а не oversight.
**Вердикт.** `Оставить` как исторический маркер, но `Упростить` до безопасного состояния
**Обоснование.** Формально задокументированная роль есть, и я её не оспариваю. Но «маркер» не должен быть и активной утилитой: сейчас `python tools/release_v251.py --dry-run` **пишет файл в корень репозитория в обход File Storage Policy** (`AGENTS.md`, раздел «File Storage Policy»: «Не пиши напрямую в корень проекта»), и `docs/RELEASE.md:99-108` (`.gitignore`-страховка для `release_v*_payload.json`) это подтверждает как известную проблему. Тег `v2.5.1` уже опубликован → повторный запуск завершится ошибкой `gh`. Практический вывод: роль «маркера» надо подтвердить явно (например, однострочным комментарием «исторический артефакт, не запускать»), а §1.5/§8 `docs/RELEASE.md` — перевести на `release_v252.py` как на образец; иначе следующий релиз снова скопирует устаревшую форму (payload в CWD вместо `tempfile`).

**Дефекты (подтверждены ruff и чтением):**
- `:75` — `Path("release_v251_payload.json")`: запись в CWD, нарушение File Storage Policy, файл не удаляется в `--dry-run`-ветке.
- `:110` и `:70` — `_payload_path()` вызывается дважды за один прогон `--curl` (лишняя запись).
- `:56` — `token` не используется; `body_json` (`:60`) — мёртвая локаль (ruff `F841`).
- `:15-16` — `CHANGELOG_BLOCK_HEADER`/`NEXT_BLOCK_HEADER` мертвы.
- `:57, :76` — `import json` внутри двух функций вместо уровня модуля.

#### Функции уровня модуля
| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_print_curl` | 56–71 | Напечатать curl-форму | Fallback | `main:113` | Упростить — убрать неиспользуемый `token` и `body_json` |
| `_payload_path` | 74–80 | Записать payload **в CWD** | Сбор | `main:110`, `_print_curl:70` | Упростить — писать в `tempfile.gettempdir()`, как в v252 |
| `_run_gh` | 83–98 | `gh release create` | Боевой запуск | `main:116` | Оставить (тег уже опубликован — фактически неисполним) |
| `main` | 101–116 | CLI | Точка входа | `__main__:120` | Упростить — двойной вызов `_payload_path` |

---

## `tools/demo_internal_fallback.py` — 113 LOC

**Назначение.** Демонстрация того, что при internal-ошибке `AgentLoop` пользователь получает `_DEFAULT_INTERNAL_ERROR_TEXT`, а не upstream-литерал `"Sorry, I encountered an error."`, и что `turn_completed` всё равно публикуется.
**Что делает.** **Подменяет `sys.modules["nanobot.agent.turn_delivery"]`** на модуль-стаб (`:75-78`), содержащий локальную перереализацию upstream-поведения `_StubTurnDelivery.fail` (`:55-72`), применяет `RuntimePatcher.patch_turn_delivery_fail` и вызывает `fail()` на экземпляре, собранном через `__new__` + `__dict__.update` (`:101-114`). Печатает результат. Exit code всегда 0.
**Зачем нужен.** Единственная быстрая визуальная проверка поведения без БД. Но то же поведение покрыто 25+ unit-тестами в `tests/test_runtime_patcher.py` (`patch_turn_delivery_fail` / `_DEFAULT_INTERNAL_ERROR_TEXT`, строки 1348-1765).
**Вердикт.** `Удалить` (покрыто тестами) либо `Упростить` до честной демонстрации
**Обоснование.** Скрипт демонстрирует, что patch-функция корректно оборачивает **переписанную в этом же файле** заглушку. Это тавтологично: `_StubTurnDelivery.fail` (`:55-72`) — локальная копия upstream-логики, а `content="Sorry, I encountered an error."` (`:60`) — ровно тот литерал, о непубликации которого заявляет docstring (`:9`). Ключевое утверждение (пункт 2 docstring) вообще **не проверяется**: скрипт печатает `content=...` и завершается, ничего не сравнивая с `_DEFAULT_INTERNAL_ERROR_TEXT` (строка 138 лишь печатает константу). `docs/TROUBLESHOOTING.md` этот сценарий не описывает, так что диагностической замены у него нет — но она и не нужна, потому что поведение закрыто тестами. Удаление безопасно: `tests/` и `docs/` скрипт не импортируют.

**Дефекты:**
- `:75-78` — подмена `sys.modules` monkey-patch'ом проверяемой библиотеки; при `import tools.demo_internal_fallback` в чужом процессе `nanobot.agent.turn_delivery` остаётся заменённым.
- `:101-114` — `__new__` + `__dict__.update` дублирует набор полей из `_StubTurnDelivery.__init__` (`:48-53`); при изменении состава полей заглушка рассинхронизируется молча.
- `:124-126` — при пустом `bus.published` скрипт делает `return` **без вывода и без ненулевого кода**: главный провал неотличим от успеха.
- `:141-142` — `asyncio.run(main())`, `main()` возвращает `None` → exit code 0 при любом исходе; использовать как gate нельзя.
- Docstring (`:10`) обещает проверку «запись попадает в `agent_gateway_logs`» — код этого не делает вообще (нет подключения к БД).

#### Класс `_StubBus` (строки 29–34, 2 метода)
Заглушка `MessageBus.publish_outbound`, собирающая outbound'ы в список.
**Вердикт.** `Удалить` (с файлом) — либо `Оставить` при переписывании скрипта в честную демонстрацию

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 30–31 | Инициализация списка | — | `main:98` | Удалить (с файлом) |
| `publish_outbound` | 33–34 | Сбор outbound | Замена Bus | `_StubTurnDelivery.fail:56` | Удалить (с файлом) |

#### Класс `_RuntimeEventPublisher` (строки 37–42, 2 метода)
Заглушка runtime-событий, собирающая kwargs `turn_completed`.
**Вердикт.** `Удалить` (с файлом)

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 38–39 | Инициализация списка | — | `main:99` | Удалить (с файлом) |
| `turn_completed` | 41–42 | Сбор события | Замена publisher'а | `_StubTurnDelivery.fail:65` | Удалить (с файлом) |

#### Класс `_StubTurnDelivery` (строки 45–72, 2 метода)
Локальная перереализация upstream `nanobot/agent/turn_delivery.py:TurnDelivery.fail`, устанавливаемая вместо реального класса.
**Вердикт.** `Удалить` (с файлом) — дублирует upstream-логику, из-за чего демонстрация теряет смысл

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 48–53 | Инициализация 5 полей | Дублируется в `main:102-114` | `__new__`-обход в `main:101` | Удалить (с файлом) |
| `fail` | 55–72 | Публикация fallback + `turn_completed` | Объект демонстрации | `main:119` | Удалить (с файлом) |

#### Функции уровня модуля
| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_install_stub_module` | 75–78 | Подменить `sys.modules[...]` | Подготовка | `main:87` | Удалить (с файлом) |
| `main` | 81–138 | Прогон демонстрации | Точка входа | `__main__:142` | Удалить (с файлом) |

---

## `tools/apply_test_profile_tables.py` — 80 LOC

**Назначение.** Создать 6 runtime-таблиц профиля `test` (`public.agent_*_test`) из 6 DDL-файлов.
**Что делает.** Наивный сплит SQL по `;` (после выкидывания `--`-комментариев), затем `cur.execute` каждого statement'а. Одна транзакция на все 6 файлов; при ошибке — `conn.rollback()` и `return 1`.
**Зачем нужен.** Прямо цитируется в рантайм-подсказке: `lib/services/schema_validation.py::_hint_for_profile` для профиля `test` выдаёт «python tools/apply_test_profile_tables.py» (`CHANGELOG.md:117`). Также `AGENTS.md:54`. Все 6 DDL-файлов на месте (проверено). Файл — единственный путь подготовки test-профиля, альтернативы через `tools/migrate.py` нет (миграции создают prod-имена).
**Вердикт.** `Оставить`
**Обоснование.** Load-bearing для тестового профиля и для диагностической подсказки при старте. Замечания ниже — качественные, не блокирующие.

**Дефекты и риски:**
1. `split_statements:45-60` — наивное разделение по `;`. Ломается на `;` внутри строкового литерала или внутри `$$ … $$`. В текущих 6 файлах таких мест нет (проверено: только `CHECK (level IN ('DEBUG', …))` без `;` и `DEFAULT gen_random_uuid();` как отдельный statement'а), поэтому сейчас безопасно — но это скрытая ловушка для будущих DDL.
2. `main():88` — `conn.rollback()` откатывает **все** ранее применённые файлы в этой же транзакции, а не только текущий. Частичное применение невозможно, но оператор увидит «FAIL» на 6-м файле и может считать, что первые 5 применены. Не задокументировано.
3. `FILES:35-42` — 6 путей захардкожены; рассинхронизация с `profiles/test.jsonc` (строки 4-9 docstring) не проверяется. `SchemaValidationService` проверяет наличие таблиц, но не их создание этим скриптом.
4. `main()` без `argv` → не тестируется; тестов нет.
5. `psycopg2.connect(dsn)` напрямую, минуя `utils.db` (в отличие от `check_worker_pool_integrity.py`) — два разных способа подключения в одном каталоге.

#### Функции уровня модуля
| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `split_statements` | 45–60 | Разбить DDL на statement'ы | Ядро | `main:76` | Упростить — заменить на `psycopg2.sql`/`sqlparse` или хотя бы задокументировать ограничение |
| `main` | 63–93 | CLI (без аргументов) | Точка входа | `__main__:97` | Оставить (+ задокументировать поведение `rollback`) |

---

## `tools/architecture_guard.py` — 61 LOC

**Назначение.** Заявлен как «проверка архитектурных invariant'ов» (`AGENTS.md:54`); фактически — три неиспользуемые вспомогательные функции.
**Что делает.** Ничего исполнимого: нет `main()`, нет `argparse`, нет `if __name__ == "__main__"`. `python tools/architecture_guard.py` определит 3 функции и завершится с кодом 0 без вывода.
**Зачем нужен.** Не нужен в текущем виде. Ссылки: `AGENTS.md:54` (неточное описание), `CHANGELOG.md:922` (историческая), `openspec/changes/archive/2026-09-27-post-0.3.5-patches-cleanup/tasks.md:53` (verify-шаг «если существует»), `OPENSPEC_MIGRATION.md:483-485` (ожидает лог от `python tools/architecture_guard.py`), `legal_summarizer_final_audit.md:254` (упоминает одноимённый guard **внутри skill'а** — `scripts/structure/architecture_guard.py`), `legal_summarizer_progress_audit.md:159` («Этап 37 — решить судьбу `architecture_guard.py` (CI helper или …)»). Последняя ссылка прямо фиксирует, что судьба инструмента **не решена**.
**Вердикт.** `Удалить`
**Обоснование.** Ни одна из трёх функций не вызывается нигде; модуль не имеет точки входа; собственная история проекта (`legal_summarizer_progress_audit.md:159`) оставляет его судьбу открытой, а `legal_summarizer_final_audit.md:254` показывает, что рабочий вариант guard'а живёт в skill'е. Ниша, которую он занимает (premature-abstraction guard), уже покрыта `tools/validate_component_specs.py` (структурный guard спецификаций) и `tools/legacy_audit.py` (guard legacy-символов, вызывается из pytest). Перед удалением: убрать упоминание из `AGENTS.md:54`; учесть, что `tasks.md:53` и `OPENSPEC_MIGRATION.md:483-485` описывают verify-шаг, который при удалении станет явно неисполнимым (это архив — править не нужно).

**Дополнительно:** даже если переписать в CLI, `is_factory_pattern` даёт ложные срабатывания — подстроги `Factory`/`Builder` матчатся на 5+ легитимных модулях репозитория (`lib/core/agent_factory.py`, `lib/core/bus_factory.py`, `lib/services/channel_factory.py`, `lib/services/llm_usage_store_factory.py`), то есть «guard» заблокировал бы сам проект.

#### Функции уровня модуля
| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `is_factory_pattern` | 43–47 | Подстрочный матч `Factory/Builder/…` | Псевдо-guard | **никто** | Удалить |
| `count_abstract_classes` | 50–58 | Подсчёт ABC в модуле | Псевдо-метрика | **никто** | Удалить |
| `has_oversized_class` | 61–71 | Класс > 100 строк | Псевдо-метрика | **никто** | Удалить |

**Мёртвый код внутри:** `:63` `import re` не используется; `:64` `source = inspect.getsource(module)` не используется (ruff `F841`); `:53, :65` `name` в циклах не используется (ruff `B007`); `.count("\n")` — грубый прокси вместо `end_lineno` (считает и пустые строки, off-by-one).

---

## `tools/extract_office_structure.py` — 51 LOC

**Назначение.** CLI-обёртка над `office_files.extract_structure` — выгрузка структуры (заголовок/начало/окончание) офисных файлов в JSON.
**Что делает.** Ничего. `:10` — `from workspace.utils.office_files import extract_structure`; функции `extract_structure` в модуле нет, импорт падает **до** определения `main()`.
**Зачем нужен.** Не нужен. Ссылок нет: grep по `docs/`, `README.md`, `CHANGELOG.md`, `tests/`, `SKILL.md`, CI — только строка в `AGENTS.md:54` («`extract_office_structure.py` — отчёт по структуре office-файлов»).
**Вердикт.** `Удалить`
**Обоснование.** Файл гарантированно падает на импорте (проверено запуском: `ImportError: cannot import name 'extract_structure'`), функция `main()` недостижима, потребителей нет, эквивалентной процедуры в документации нет. Дублирует возможности `workspace/utils/office_files.py`, которые уже используются навыками. Перед удалением: убрать упоминание из `AGENTS.md:54`.

**Что делать, если функция нужна:** переписать на существующий API — `workspace.utils.office_files.summarize(path, preview_chars=500)` (`:266-305`) уже возвращает `format`/`size_bytes`/превью/`tables` по docx/pdf/pptx/xlsx/xls/csv с graceful-деградацией (`metadata_error` при сбое). Это покрывает задачу «структура без LLM в JSON» без нового кода. Дополнительно: `SUPPORTED` (`:12`) не совпадает с `detect_format` (устаревший список), а `sys.path.insert(0, parents[1])` (`:8`) указывает на корень репозитория, тогда как остальные `tools/` добавляют и `workspace/` и импортируют `utils.*` — стиль импорта здесь тоже несогласован.

#### Функции уровня модуля
| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_iter_files` | 15–21 | Обход каталога по `SUPPORTED`-суффиксам | Сбор входа | `main:41` | Удалить (файл) |
| `main` | 24–60 | CLI `--begin/--end/--no-text/-o` | Точка входа | `__main__:64` | Удалить (файл) |

---

## Кросс-подсистемные находки

1. **`tools/` вне ruff, но назван в CI.** `pyproject.toml:4` — `extend-exclude = ["tests", "tools", "benchmarks", "*.egg-info"]`; `.github/workflows/ci.yml:38` — `ruff check lib workspace tests tools`. Явно перечисленный каталог всё же сканируется: `ruff check tools` → **51 ошибка** (12 `F841`, 8 `F541`, 7 `I001`, 6 `E402`, 6 `F401`, 4 `B905`, 2 `B007`, …); вся команда CI — **653 ошибки**. Три сломанных файла из этого отчёта проходят ruff, потому что ruff не резолвит импорты. Принадлежит не `tools/`, а конфигурации линтинга.
2. **`VectorIndexBuildService` (`lib/services/vector_index_service.py:41`, 31 строка) — 0 вызовов.** Подтверждаю находку отчёта 02: `tools/build_vectors.py:78` импортирует только re-export `get_embedding`; других ссылок нет. Влияет на вердикт: 31 строка сервисного слоя существует без единого потребителя.
3. **Канон `runtime_inventory` (`lib/services/runtime_inventory.py:153`) расходится с регистрацией.** `name="ExampleTool"` против фактически регистрируемого `example_tool`; `config_key`-строки неверны. Потребитель — `tools/diagnose_startup.py:203-207`. При текущем дефолте (`example` выключен) инструмент не сломается, при включении даст ложный DRIFT + exit 2. Владелец правки — отчёт 03.
4. **Версия `nanobot` в документации и утилитах устарела.** `requirements.txt:11` = `nanobot-ai==0.3.5`, установлена 0.3.5; при этом `AGENTS.md` (шапка) говорит «поверх `nanobot 0.3.0»», `tools/scan_nanobot_inventory.py:148` хардкодит `"0.3.0"`, `docs/architecture/nanobot-inventory.json._meta` содержит `installed: "0.3.0"`, `docs/audit/AUDIT_PROTOCOL.md` тоже говорит 0.3.0, `tools/audit_nanobot_contracts.py:1` — 0.3.5. Расхождение в самом аудите фиксировалось непоследовательно.
5. **Три «зомби»-каталога `tools/`:** `tools/debug/` (только `__pycache__`, не под git) — остаточный артефакт удалённого скрипта; `tools/test_audit.py` + `tools/legacy_audit.py` (1123 LOC, из них 7 неиспользуемых локалей только в `test_audit.py`) — одноразовая кампания аудита тестов; `tools/architecture_guard.py` + `tools/legal_benchmark.py` — осиротевшие копии артефактов skill'а `legal_summarizer` (см. `legal_summarizer_final_audit.md:254`).
6. **`docs/RELEASE.md:83-97` устарел относительно собственных скриптов.** §1.5 предписывает `cp tools/release_v251.py tools/release_v252.py` и правку `CHANGELOG_BLOCK_HEADER`/`NEXT_BLOCK_HEADER`, но в реальном `release_v252.py` этих правок нет (константы мертвы), а форма отличается (`tempfile` + `_print_payload` + `--run`). §1.6 утверждает «Сейчас артефакты идут в `tempfile.gettempdir()`» — верно для v252, **неверно** для v251, который пишет в CWD.
7. **Ссылка на завершённую change.** `tools/smoke_post_cleanup.py:16` указывает на `openspec/changes/post-0.3.5-patches-cleanup/tasks.md`; фактический путь — `openspec/changes/archive/2026-09-27-post-0.3.5-patches-cleanup/`, и все три упомянутые задачи (6.3, 8.3, 8.4) помечены `[x]`.
8. **Два способа подключения к PG внутри одного каталога:** `tools/check_worker_pool_integrity.py:50` и `tools/build_vectors.py:79` — через пул `utils.db`; `tools/apply_test_profile_tables.py:73` и `tools/smoke_post_cleanup.py:120` — прямой `psycopg2.connect` с собственной логикой DSN. Расхождение делает разные инструменты по-разному чувствительными к профилю и к `channels.postgres.dsn`.
9. **Непокрытые тестами инструменты с реальным побочным эффектом:** `migrate.py` (пишет в БД, `SystemExit` в `discover`), `check_worker_pool_integrity.py::_fix` (UPDATE/DELETE), `apply_test_profile_tables.py` (DDL), `validate_component_specs.py`, `architecture_guard.py`, `generate_comments_sql.py`, `scan_nanobot_inventory.py`, `audit_nanobot_contracts.py`, `release_v25*.py`, `demo_internal_fallback.py`. `test_gaps.md:30-66` это фиксирует; мой вердикт подтверждает, что для *паттернов* из `test_gaps.md` покрытие не обязательно, но для трёх DDL/DML-инструментов — обязательно.
10. **Хардкод `profile="test"` в standalone-утилитах** (`build_vectors.py:813`, `check_indexes.py:261`) и `--profile=test` (`smoke_post_cleanup.py:33`) — при том что активная opencode change `remove-profile-environment-selection` в `openspec/changes/` меняет модель выбора профиля. Проверить совместимость (не проверено: требует чтения spec'и этой change).

---

## Сводка вердиктов по файлам

| Файл | LOC | Вердикт файла | Ключевое |
|---|---|---|---|
| `build_vectors.py` | 878 | Упростить | 4 функциональных дефекта, exit code всегда 0, мёртвая infra-регистрация |
| `test_audit.py` | 647 | Удалить | Не запускается вне этой машины, дубли smells, 7 мёртвых локалей |
| `legacy_audit.py` | 436 | Упростить | Guard жив, но 3 внутренних механизма сломаны; 33 записи allow-list мертвы |
| `diagnose_startup.py` | 386 | Оставить | Единственный наблюдатель `runtime_inventory`; 7 тестов |
| `check_indexes.py` | 245 | Оставить | Покрыт 17 тестами, дублирования с `lib/` нет |
| `generate_comments_sql.py` | 270 | Упростить | Падает на импорте; нет `main()`; выход устарел (нет `user_id`, `agent_worker_claims`) |
| `validate_component_specs.py` | 224 | Упростить | Процедура задокументирована; `--strict` не меняет код; тавтологичная проверка кириллицы |
| `migrate.py` | 214 | Оставить + Слить | Load-bearing (цитируется в рантайм-подсказке); `resolve_dsn` дубликат; строковое сравнение версий |
| `check_worker_pool_integrity.py` | 165 | Упростить | Runbook-инструмент; вероятный SQL-баг в `--fix:188`; мёртвая проверка №4 |
| `legal_benchmark.py` | 166 | Удалить | `ModuleNotFoundError`; нет entrypoint; дублирует `benchmarks/` |
| `audit_nanobot_contracts.py` | 169 | Упростить | Не в CI; мутирует `nanobot` через `setattr`; признаёт ложные срабатывания |
| `scan_nanobot_inventory.py` | 153 | Упростить | Актуален и воспроизводим; 2 мёртвые регулярки, мёртвая ветка, хардкод версии |
| `smoke_post_cleanup.py` | 147 | Упростить / Удалить | Привязан к архивированной change; проверки не могут провалить прогон; пароль в исходнике |
| `release_v252.py` | 136 | Оставить + Упростить | Процедура релиза задокументирована; BODY описывает удалённую архитектуру; 2 мёртвые константы |
| `release_v251.py` | 94 | Оставить (маркер) + Упростить | `docs/RELEASE.md:83` требует сохранить; но пишет в CWD и не удаляет файл |
| `demo_internal_fallback.py` | 113 | Удалить | Тавтологичен (стаб в том же файле); покрыт 25+ тестами; ключевая проверка не выполняется |
| `apply_test_profile_tables.py` | 80 | Оставить | Load-bearing для test-профиля; наивный сплит SQL; нет тестов |
| `architecture_guard.py` | 61 | Удалить | Не CLI; 3 функции без вызовов; судьба не решена по `legal_summarizer_progress_audit.md:159` |
| `extract_office_structure.py` | 51 | Удалить | `ImportError` на строке 10; потребителей нет; замена — `office_files.summarize` |
| `__init__.py` | 1 | Оставить | Единственная точка, делающая `tools.*` импортируемым для тестов |
