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

