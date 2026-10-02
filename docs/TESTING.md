# 🧪 Тестирование

> Навигационный индекс каталога `docs/` — в [`README.md`](README.md). Этот документ —
> самодостаточное описание подсистемы.

## Два независимых pytest-корня

Наборы не смешаны: у агента и у платформы свои `pyproject.toml`, свой
`testpaths` и своя кодовая база. Корневой `testpaths = ["tests"]` не видит
`mcp-platform/tests`, а платформенный — не видит `tests/`. Запускать их нужно
из своих каталогов.

| | Агент | Платформа |
|---|---|---|
| Корень pytest | `.` (корень репозитория) | `mcp-platform/` |
| Набор | `tests/` — 136 файлов | `mcp-platform/tests/` — 189 файлов |
| `testpaths` | `["tests"]` | `["tests"]` |
| `python_files` | `["test_*.py"]` | `["test_*.py"]` |
| `pythonpath` | `["."`, "workspace"]` | `["."]` |

Следствие общего шаблона имён: файл с **ведущим** подчёркиванием
(`_test_*.py`) не собирается ни одним из двух наборов. Хелперы, которые
pytest не должен собирать, называются без префикса `test_`
(например `mcp-platform/tests/audit_lib_fakes.py`).

## Команды

Все команды ниже проверены `Test-Path` на текущем дереве.

```bash
# ── Полные наборы ────────────────────────────────────────────────────────────
# Агент: из корня репозитория
python -m pytest tests -q

# Платформа: обязательно из mcp-platform, иначе подхватится корневой conftest
cd mcp-platform && python -m pytest tests -q

# Только сборка имён тестов, без исполнения (быстрая проверка коллекции)
python -m pytest --collect-only -q
cd mcp-platform && python -m pytest --collect-only -q

# ── Агент: точечные наборы (все файлы существуют) ────────────────────────────
# Сервисный слой, без БД
python -m pytest tests/test_config_service.py tests/test_session_storage.py \
                    tests/test_runtime_patcher.py tests/test_channel_factory.py \
                    tests/test_db_logging_service.py tests/test_hooks_database_logging.py \
                    tests/test_bus_factory.py tests/test_agent_factory.py \
                    tests/test_gateway_runner.py tests/test_shutdown_coordinator.py \
                    tests/test_console_loop.py tests/test_application_context.py -q

# Пул соединений (mock psycopg2, БД не нужна)
python -m pytest tests/test_utils_db.py -q

# Сессии поверх PostgreSQL
python -m pytest tests/test_pg_session_manager.py -q

# Чтение офисных файлов
python -m pytest tests/test_office_files.py -q
```

> Модулей `test_transcription_service.py`, `test_subprocess_manager.py`,
> `test_preload_service.py`, `test_cache_store.py` и `test_sync_service.py` в
> дереве нет: голос разбирает базовый класс `nanobot`, а локальный кэш и
> загрузчик снимка снесены (кэш принадлежит capability `data` платформы).
> Скилл `audit_analyzer` обезличен, поэтому прежний
> `workspace/skills/audit_analyzer/tests/e2e_test.py` тоже не существует.

## Маркеры и их env-гейты

Объявлены в `pyproject.toml` обоих корней (словари одинаковые). Фактически
маркеры применяет **только** агентский набор: в `mcp-platform/tests` вхождений
маркеров нет, платформенные тесты фильтруются только по пути.

| Маркер | Гейт | Кто применяет |
|---|---|---|
| `live` | `NANOBOT_LIVE_E2E=1` (плюс живой БД/провайдер) | `tests/test_startup_schema_validation_live.py` |
| `integration` | `NANOBOT_INTEGRATION=1` + `DATABASE_URL` | `tests/integration/test_postgres_channel_lifecycle_stress.py` |
| `contract` | не гейтится, выполняется всегда | 20 файлов совместимости с `nanobot-ai` |
| `benchmark` | опт-ин через `-m benchmark` | `tests/test_history_search_benchmark.py` |

```bash
# Живой e2e: реальный gateway + живая БД + живой LLM.
# Пишет в изолированную таблицу public.agent_conversation_messages_e2e —
# боевая очередь не трогается.
$env:NANOBOT_LIVE_E2E="1"; python -m pytest tests/test_gateway_live_media_e2e.py -q

# Integration: пул воркеров на реальной БД
$env:NANOBOT_INTEGRATION="1"; $env:DATABASE_URL="postgresql://..."
python -m pytest tests/integration -q

# Без живых гейтов: снять opt-in-метки и opt-in-бенчмарк
python -m pytest tests -q -m "not live and not integration and not benchmark"

# Только контракты совместимости
python -m pytest tests -q -m contract

# Бенчмарк по требованию
python -m pytest -m benchmark tests/test_history_search_benchmark.py -v
```

Маркер, объявленный, но не применённый, — мёртвый фильтр: он создаёт
впечатление, что что-то отсекается, и ничего не отсекает. Новый маркер имеет
смысл только вместе с местом, которое его ставит.

---

> **Стандарт качества тестов (QA-чистка 2026-08-18).** Набор проревизован —
> each test должен давать реальную проверку, а не «галочку». Не оставляем:
> smoke-тесты без `assert` (одно «не должно упасть»), тесты, пересказывающие
> дефолты датаклассов/конструкторов, и тесты, мокающие саму тестируемую
> функцию. Удалено 42 таких теста, исправлен `assert ... if False else True`.
> Паттерн `pytest.skip` без DuckDB-кэша (портабельный guard интеграционных
> тестов, не заглушка) применялся в удалённом `test_db_loader.py` и остался
> образцом для новых интеграционных тестов.
>
> **Уточнение 2026-10-02 (чистка тестовых наборов).** `pytest.skip` внутри
> стража — не этот паттерн и исключение из него не делает. Если охраняемый
> файл исчез, страж обязан падать, а не выключать себя: иначе пропавший
> файл тихо убирается из-под охраны, а прогон остаётся зелёным. Поэтому в
> `tests/test_unified_event_logging_pipeline.py` `pytest.skip` на
> «файл исчез» и «файл не разбирается» заменены на `pytest.fail`; чтение
> охраняемых файлов переведено на `utf-8-sig`, потому что BOM в начале файла
> ронял `ast.parse`, и два теста (`test_text_utils.py`,
> `test_question_run_write.py`) до этого молча выпадали из проверки.
