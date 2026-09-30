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

## Baseline платформы

```bash
cd mcp-platform && python -m pytest -q
```

```
39 passed in 1.92s
```

## Изоляция

`mcp-platform/` добавлена в репозиторий, но **не входит** в корневой прогон:
в `pyproject.toml` агента `testpaths = ["tests", "workspace/skills/audit_analyzer/tests"]`.
Проверено после создания папки — корневой прогон по-прежнему собирает
ровно 4101 тест.

## Известный шум окружения

`pip check` показывает неудовлетворённые зависимости пакета
`flashinfer-python` (CUDA/tvm-библиотеки). К проекту отношения не имеет,
до миграции существовал, на тесты не влияет.

## Что запрещено на этой фазе

Соблюдено. В коммите нет изменений в `lib/`, `workspace/`, `sql/`,
`config.json`, `requirements.txt`. `nanobot` не патчился.
