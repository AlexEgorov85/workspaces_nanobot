# 🧪 Тестирование

> Перенесено из `DEVELOPMENT.md`. Навигационный хаб — в корне `DEVELOPMENT.md`.

## 🧪 Тестирование

Полный текущий набор — через `pytest -q` (см. `tests/`).
Разбивка по категориям и командам — в `README.md` (раздел «Тестирование»),
там же список test-файлов.

```bash
# Юнит-тесты сервисного слоя (не требуют БД)
python -m pytest tests/test_config_service.py tests/test_session_storage.py \
                    tests/test_runtime_patcher.py tests/test_transcription_service.py \
                    tests/test_channel_factory.py tests/test_subprocess_manager.py \
                    tests/test_preload_service.py tests/test_db_logging_service.py \
                    tests/test_hooks_database_logging.py tests/test_bus_factory.py \
                    tests/test_agent_factory.py tests/test_gateway_runner.py \
                    tests/test_shutdown_coordinator.py tests/test_console_loop.py \
                    tests/test_application_context.py -q

# Пул соединений (mock psycopg2, БД не нужна)
python -m pytest tests/test_utils_db.py -q

# Тесты воркеров (некоторые требуют БД)
python -m pytest tests/test_pg_session_manager.py -q

# Юнит-тесты audit/кэша (sync+memory)
python -m pytest tests/test_cache_store.py tests/test_sync_service.py -q

# Полный набор (без БД)
python -m pytest tests -q

# Сквозной тест навыка (требует живого PostgreSQL)
python workspace/skills/audit_analyzer/tests/e2e_test.py

# Live e2e media-фикса (реальный gateway + живая БД + живой LLM)
# Опт-ин: без NANOBOT_LIVE_E2E=1 тест пропускается. Пишет в изолированную
# таблицу public.agent_conversation_messages_e2e (боевая очередь не трогается).
$env:NANOBOT_LIVE_E2E="1"; python -m pytest tests/test_gateway_live_media_e2e.py -q
```

E2E проверяет все режимы: predefined (реальный SQL по шаблонам), sql
(LLM → EXPLAIN → выполнение), vector (FAISS + Ollama embedding), а также
резолв параметров через семантический поиск.

> **Стандарт качества тестов (QA-чистка 2026-08-18).** Набор проревизован —
> each test должен давать реальную проверку, а не «галочку». Не оставляем:
> smoke-тесты без `assert` (одно «не должно упасть»), тесты, пересказывающие
> дефолты датаклассов/конструкторов, и тесты, мокающие саму тестируемую
> функцию. Удалено 42 таких теста, исправлен `assert ... if False else True`.
> `test_db_loader.py` намеренно использует `pytest.skip` без DuckDB-кэша —
> это портабельный guard интеграционных тестов, не заглушка.

---

