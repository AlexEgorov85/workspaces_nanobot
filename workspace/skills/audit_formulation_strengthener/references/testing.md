# Тестирование skill'а `audit_formulation_strengthener`

## Запуск

```bash
# Из корня репозитория:
pytest workspace/skills/audit_formulation_strengthener/

# Только один файл:
pytest workspace/skills/audit_formulation_strengthener/tests/test_mode_analyze.py

# С coverage:
pytest workspace/skills/audit_formulation_strengthener/ --cov=scripts --cov-report=term-missing

# Verbose:
pytest workspace/skills/audit_formulation_strengthener/ -v
```

## Стратегия

**Unit-тесты с моками.** Никаких реальных LLM-вызовов, никакого реального PDF-парсинга.

Преимущества:
- Детерминированность (тесты не падают на fluke-ответах LLM).
- Скорость (42 теста за <1 сек).
- Изоляция (каждый тест проверяет ровно один кусок логики).
- Без зависимости от Ollama / PostgreSQL / интернета.

## Фикстуры (`tests/conftest.py`)

### LLM-моки

| Фикстура | Описание | Возвращает |
|----------|----------|------------|
| `mock_llm_analyze` | Анализ формулировки | Полный canned-ответ analyze |
| `mock_llm_search_map` | Map-фаза search | Разные scores по содержимому excerpt |
| `mock_llm_synthesize` | Синтез отчёта | Полный canned-ответ synthesize |
| `mock_llm_all` | Все 3 операции | Роутинг по `operation` |

**Пример использования:**

```python
def test_analyze_success(mock_llm_analyze):
    with pytest.MonkeyPatch.context() as m:
        m.setattr(analyze, "call_llm_json", mock_llm_analyze)
        result, _ = analyze.run(violation="...")
    assert result["data"]["severity"] == "высокая"
```

### VND-моки

| Фикстура | Описание |
|----------|----------|
| `sample_vnd_chunks` | 3 подготовленных VndChunk-подобных dicts (с разным содержимым для проверки фильтрации) |
| `mock_prepare_vnd` | Подмена `vnd_io.prepare_vnd` — возвращает `sample_vnd_chunks` |
| `tmp_vnd_files` | 2 temp `.txt` файла с реальным содержимым (для тестов с реальным I/O) |

### Path-хелперы

| Фикстура | Описание |
|----------|----------|
| `scripts_dir` | Абсолютный путь к `scripts/` |
| `cli_path` | Абсолютный путь к `scripts/cli.py` |
| `REPO_ROOT` | Корень репо (через `_find_repo_root`) |

## Структура тестов

```
tests/
├── conftest.py                  # 11 фикстур
├── test_helpers.py              # 11 тестов: vnd_io, prompts, output
├── test_mode_analyze.py         #  8 тестов: analyze-фаза
├── test_mode_search.py          #  8 тестов: search-фаза
├── test_mode_synthesize.py      #  9 тестов: synthesize-фаза
└── test_cli.py                  #  6 тестов: CLI-обёртка
```

**Всего: 42 теста.**

## Что покрыто

### analyze (8 тестов)
- ✅ Пустая/whitespace формулировка → `empty_violation`
- ✅ `--estimate-only` без LLM
- ✅ Полный успешный прогон
- ✅ Нестандартный severity → нормализация
- ✅ `JsonParseError` после всех retry → `json_parse_failed`
- ✅ Dict без `normalized` → `schema_mismatch`
- ✅ Доп. поля LLM → `extras`

### search (8 тестов)
- ✅ Пустой список ВНД → `no_vnd`
- ✅ Несуществующий файл → `vnd_not_found`
- ✅ `--estimate-only` без LLM
- ✅ Полный map-reduce: фильтрация, re-rank, top-K
- ✅ `analyze_result` (in-memory) как нормализованная формулировка
- ✅ `analyze_result_path` (из файла)
- ✅ Graceful degradation при ошибке одного чанка
- ✅ Невалидный `relation_type` → `контекст`

### synthesize (9 тестов)
- ✅ `--estimate-only` без LLM
- ✅ Полный синтез → markdown-рендер
- ✅ Нет ВНД-findings → работает
- ✅ Нет analyze → fallback severity = "средняя"
- ✅ Сохранение в `.md`
- ✅ Сохранение в `.txt` (с strip-markdown)
- ✅ Сохранение в `.docx` (через python-docx)
- ✅ Загрузка analyze/search из файлов
- ✅ Невалидный `relation_type` в citations → `контекст`

### CLI (6 тестов)
- ✅ `--mode analyze` → JSON без search
- ✅ `--estimate-only` → 0 LLM-вызовов
- ✅ Нет `--vnd` → exit-code 2
- ✅ Пустой `--violation` → `empty_violation`
- ✅ `--mode all --output <path>` → сохраняет файл
- ✅ Default mode = `all`

### vnd_io / prompts / output (11 тестов)
- ✅ `build_cache_key` детерминированный, hex SHA-256
- ✅ `prepare_vnd` бросает `VndInputError` для пустых/несуществующих путей
- ✅ `load_prompt` для существующих/несуществующих
- ✅ `render_prompt` подстановка, `None`-handling
- ✅ `make_error` с доп. kwargs

## Паттерны

### Подмена LLM на уровне модуля

```python
with pytest.MonkeyPatch.context() as m:
    m.setattr(target_module, "call_llm_json", mock_function)
    result, _ = target_module.run(...)
```

**Почему не `unittest.mock.patch`?** `monkeypatch` автоматически
откатывает изменения после теста, не нужно оборачивать в `with`.

### Подмена нескольких LLM одновременно

```python
@pytest.fixture
def mock_all(monkeypatch):
    monkeypatch.setattr(modes.analyze, "call_llm_json", mock_a)
    monkeypatch.setattr(modes.search, "call_llm_json", mock_s)
    monkeypatch.setattr(modes.synthesize, "call_llm_json", mock_syn)
```

### CLI-тесты без subprocess

```python
import importlib.util
spec = importlib.util.spec_from_file_location("cli", str(CLI_PATH))
cli_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli_mod)  # загрузка с import side-effects

with redirect_stdout(io.StringIO()) as buf:
    rc = cli_mod.main(["--violation", "...", "--vnd", "x.txt"])
```

**Преимущества:** нет накладных расходов на subprocess, моки применяются напрямую.

## Как добавить новый тест

1. **Определить, что тестируем:** новый режим, edge case в analyze/search/synthesize, новый helper.
2. **Выбрать фикстуру:** `mock_llm_*` или написать inline-мок.
3. **Паттерн:**
   ```python
   def test_<scenario>(mock_llm_<phase>):
       with pytest.MonkeyPatch.context() as m:
           m.setattr(target_module, "call_llm_json", mock_llm_<phase>)
           result, report_text = target_module.run(...)
       assert result["status"] == "success"
       assert ...
   ```

## Известные ограничения

- Тесты **не проверяют** реальное качество LLM-ответов (только структуру и нормализацию).
- Тесты **не проверяют** реальный PDF-парсинг (замокан через `mock_prepare_vnd`).
- Для smoke-test реального pipeline — запуск CLI с реальными `--vnd`-файлами
  вне pytest (требует Ollama).
