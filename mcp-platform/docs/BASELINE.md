# Фаза 0 — точка отсчёта

Снято **до** первого изменения кода. Любой регресс миграции отслеживается
относительно этих чисел.

## Идентификация

| Параметр | Значение |
|---|---|
| Ветка | `refactor/mcp-platform` |
| `BASE_COMMIT` | `8ef9d08c515f1150a9610c4117d42e1eebe7732e` |
| Зафиксировано | 2026-09-30 |
| `NANOBOT_VERSION` | `0.3.5` — изменению не подлежит |
| MCP SDK | `mcp==1.27.1` (приходит как зависимость nanobot) |
| Python | 3.14.2, Windows 11 |
| Отношение к `master` | master — предок, `0 behind / 15 ahead` |

## Baseline тестов агента

```bash
python -m pytest -q
```

```
4 failed, 4057 passed, 39 skipped, 1 xpassed in 134.03s
```

4101 тест собирается. Из них **4 падают уже на чистом baseline** — это
предсуществующий долг, а не регресс миграции:

| Тест | Статус до миграции |
|---|---|
| `tests/benchmarks/test_acceptance_matrix.py::test_acceptance_matrix_required_modules_exist` | ❌ падает |
| `tests/test_information_preservation.py::test_info_preservation_e2e_mock_passes_keywords` | ❌ падает |
| `tests/test_information_preservation.py::test_info_preservation_e2e_partial_summary_below_threshold` | ❌ падает |
| `tests/test_resume_scenarios.py::test_resume_integration_run_writes_manifest` | ❌ падает |

**Правило:** после любой фазы миграции допустимы ровно эти же 4 падения.
Появление пятого — регресс, фаза не закрыта.

### Известный флак: `test_running_marker_arrives_before_run_completes`

`tests/test_legal_summarizer_running_subprocess.py::test_running_marker_arrives_before_run_completes`
падает не всегда, а под нагрузкой полного прогона. Проверено 2026-09-30:
в полном прогоне — `5 failed`, в изоляции файл проходит 5 запусков подряд.

Тест проверяет, что маркер «процесс запущен» приходит **до** завершения
подпроцесса, то есть гонку с таймингом. Под нагрузкой машины окно между
запуском и завершением успевает сжаться.

**Как отличить флак от регресса:** прогнать файл изолированно. Зелёный —
значит падение не связано с изменениями фазы. В список допустимых падений он
не входит: при возврате к baseline исчезает сам.

## Дельта фазы 1 — снятие протокола аренды задач

Корневой прогон: **`4 failed, 3999 passed, 31 skipped, 1 xpassed` за 116 с**.
Падения — те же четыре предсуществующих, ни одного нового.

| | Фаза 0 (после) | Фаза 1 (после) | Дельта |
|---|---|---|---|
| Сбор тестов | 4 080 | 4 035 | −45 |
| passed | 4 036 | 3 999 | −37 |
| skipped | 39 | 31 | −8 |

`skipped` уменьшился ровно на 8 — это удалённые opt-in интеграционные тесты
worker-пула (`NANOBOT_INTEGRATION=1` / `NANOBOT_LIVE_E2E=1`), которые и так не
выполнялись. Уменьшение `passed` на 37 объясняется полностью:

| Файл | Было | Стало | Дельта |
|---|---|---|---|
| `tests/test_parallel_modes.py` | 12 | 4 | −8 |
| `tests/test_postgres_channel_static_audit.py` (удалён) | 8 | 0 | −8 |
| `tests/integration/test_worker_pool_concurrency.py` (удалён) | 5 | 0 | −5 |
| `tests/test_single_mode_audit.py` | 14 | 9 | −5 |
| `tests/test_postgres_channel.py` | 69 | 65 | −4 |
| `tests/integration/test_worker_pool_real_bot.py` (удалён) | 3 | 0 | −3 |
| `tests/test_config_keys.py` | 116 | 113 | −3 |
| `tests/test_project_settings.py` | 62 | 61 | −1 |
| **Сумма** | | | **−37** |

Оставшиеся 8 из 45 дают параметризованные гарды, чьи наборы параметров
сократились вместе с удалёнными объектами (в первую очередь
`test_dependency_direction.py`, `test_single_cache_interface.py`,
`test_storage_hybridization.py`, `test_remove_vector_index_store_guards.py`).
Пофайловую сверку снимали сравнением сборки в распакованном снимке `bf3ab48`;
абсолютные числа такой снимки непригодны как база — в архив не попадают
неотслеживаемые файлы, по которым параметризуется
`test_unified_event_logging_pipeline.py` (+156 кейсов артефакта).

**Доказательство, что поведение single-режима не изменилось.** SQL захвата
извлечён из AST и сравнён: текст в нынешнем `_claim_one` **побайтово равен**
прежнему `_claim_one_single` (нормализация — только подстановки
`{self._fq_table}` и `{priority_clause}`).

**Прочие проверки:** `tools/architecture_guard.py` — exit 0; `mcp-platform` —
`53 passed, 1 skipped`; `ruff` по изменённым файлам `lib/` — те же 4
предсуществующих находки (`F821` в `postgres_channel.py`, `UP037`, `UP035`,
`F401`), ни одной новой; `config.py` загружается, три удалённых ключа
в `channels.postgres` отсутствуют.

## Дельта фазы 1.5–1.7 — бенчмарки, Streamlit, шаблон tool'а

Корневой прогон: **`4 failed, 3664 passed, 31 skipped, 1 xpassed` за 171 с**
(без `tests/test_legal_summarizer_running_subprocess.py` — см. ниже).
Падения — те же четыре предсуществующих, ни одного нового.

| | После 1.1–1.4 | После 1.5–1.7 | Дельта |
|---|---|---|---|
| Сбор тестов | 4 035 | 3 605 в `tests/` | −430 |
| passed | 3 999 | 3 664 | −335 |

Что ушло: 8 файлов `tests/test_benchmarks_*.py`, `tests/test_streamlit_app.py`,
`tests/test_subprocess_manager.py`; секция «D.3 Streamlit invocation tests» из
`test_profile_lifecycle.py` (5 тестов), `TestStreamlitEnabled` из
`test_gateway.py`, `test_example_tool_is_optional` из
`test_runtime_inventory.py`, плюс −2 параметра у
`test_dependency_direction`/`test_single_cache_interface`-style гардей из-за
удалённых `lib/`, `workspace/` и `tools/` модулей.

**Осторожно с полным прогоном.** `tests/test_legal_summarizer_running_subprocess.py`
не только флакует, но и **подвешивает** прогон: родительский pytest ждёт
дочерний процесс, который не завершается. В этом сеансе он дважды
останавливал полный прогон. Обход при верификации:
`--ignore=tests/test_legal_summarizer_running_subprocess.py`, а сам файл
гнать изолированно (проходит). Это предсуществующий дефект, не регрессия
миграции; `pytest-timeout` в проекте не установлен, поэтому страховки нет.

**Ещё найдено, не исправлено:** `tools/generate_comments_sql.py` падает на
чистом клоне — читает `workspace/skills/audit_analyzer/cache/schema.json`,
а каталог `cache/` игнорится git. `sql/comments/apply_all_comments.sql`
поэтому правился вручную.

## Baseline платформы

```bash
cd mcp-platform && python -m pytest -q
```

```
53 passed, 1 skipped
```

Число выросло с 39: добавлены проверки правил 8 и 9 — владение разделяемыми
ресурсами и запрет SQL на поверхности агента, каждая со своим тестом на
заведомо плохом коде.

## Дельта фазы 0 — удаление мёртвого и сломанного кода

После фазы 0 корневой прогон: `4 failed, 4036 passed, 39 skipped, 1 xpassed`
за 116 с. Падения — те же четыре предсуществующих, флак
`test_running_marker_arrives_before_run_completes` в этом прогоне не повторился.

Сбор **4080** тестов вместо 4101. Разница полностью объяснена:

| Что | Тестов | Почему |
|---|---|---|
| `tests/test_table_utils.py` удалён | −7 | прямые тесты мёртвой функции |
| Параметризованные стражи | −12 | `table_utils.py` / `structure_cache.py` были параметрами: `test_dependency_direction.py`, `test_single_cache_interface.py`, `test_storage_hybridization.py`, `test_unified_event_logging_pipeline.py`, `test_remove_vector_index_store_guards.py` |
| `CORE_SERVICES` в `test_core_infrastructure_independence.py` | −2 | `table_utils.py` был живым параметром в `test_no_routing` и `test_no_audit_identifiers`; запись убрана вместе с файлом (`pg_duckdb_sync_service.py` там же была, но уже отфильтровывалась по `.exists()`) |
| **Итого** | **−21** | |

Необъяснённой потери нет: 4101 − 7 − 12 − 2 = 4080.

**Поправка к предпосылке плана:** приёмка фазы 0 требовала «`ruff check` —
чисто». Главный проект не был ruff-чистым и до фазы: `ruff check lib workspace`
даёт **218** ошибок. Удалённые файлы приносили ровно 2 из них
(`E402`, `E501`), то есть изменение дало **−2 ошибки, ни одной новой**.
Расчистка 218 ошибок — отдельная задача и с миграцией не смешивается.
`ruff check mcp-platform` — действительно чисто.

**Не выполнено из-за политики удаления:** восстановимое удаление недоступно
(`mavis-trash` не вызывается), постоянное запрещено. Требуют ручного удаления:
`workspace/skills/audit_analyzer/err1.log` и 82 черновика
`workspace/data_store/cache/**/*.py` — файлы не отслеживаются git, удалить их
коммитом нельзя.

## Решение по снимку DuckDB (на baseline не влияет)

Числа тестов в этом документе не менялись: коммит правил только план. Код агента
(`lib/`, `workspace/`, `sql/`, `requirements.txt`, `project.json`) не тронут.

Предыдущая редакция change `enterprise-mcp-platform` выписывала локальный снимок
DuckDB из проекта и считала это ~2 600 удаляемых строк и 10 удаляемых тестовых
модулей. Решение изменено: снимок остаётся и переходит под владение capability
`data`, поэтому вклад в baseline нулевой по всем трём статьям:

* кластер `duckdb_cache_store.py` + `cache_load_service.py` +
  `cache_provider.py` + исполнитель запросов из `duckdb_query.py` меняет место
  жительства, а не исчезает — **0 строк**;
* 10 тестовых модулей кэша переезжают в `mcp-platform` стражами capability
  `data`, а не удаляются — **0 тестов**;
* `duckdb` и `pyarrow` остаются зависимостями: они уходят из требований агента и
  переходят в манифест сервера.

Пересборка baseline остаётся запланированной на фазе 5, но теперь по причине
переезда тестов вместе с кодом, а не удаления подсистемы.

## Изоляция

`mcp-platform/` добавлена в репозиторий, но **не входит** в корневой прогон:
в `pyproject.toml` агента `testpaths = ["tests", "workspace/skills/audit_analyzer/tests"]`.
Проверено после создания папки — корневой прогон по-прежнему собирает
ровно 4101 тест.

## Известный шум окружения

`pip check` показывает неудовлетворённые зависимости пакета
`flashinfer-python` (CUDA/tvm-библиотеки). К проекту отношения не имеет,
до миграции существовал, на тесты не влияет.

## Что запрещено на платформенных фазах

Соблюдено для коммитов `b982c70` … `761cc8f`: изменений в `lib/`, `workspace/`,
`sql/`, `config.json`, `requirements.txt` нет, `nanobot` не патчился.
Фаза 0 — первая фаза, которая трогает код агента, и только удалением:
правок поведения в ней нет ни одного файла.

