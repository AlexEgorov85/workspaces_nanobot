# Система бенчмарков nanobot

Автоматическая оценка качества работы агента nanobot через набор тестовых заданий (benchmark items).

---

## 1. Архитектура

```
benchmarks/
├── __init__.py           # Версия пакета (0.1.0)
├── .gitignore            # Игнорирование results/runs/*, __pycache__
├── db.py                 # Сохранение результатов в PostgreSQL
├── evaluator.py          # Логика проверки ответа агента
├── hooks.py              # Сбор метрик выполнения агента
├── loader.py             # Загрузка YAML-файлов с заданиями
├── models.py             # Dataclass-модели всех сущностей
├── reporter.py           # Генерация отчётов (JSON, Markdown)
├── runner.py             # CLI-точка входа для запуска
├── scorer.py             # Взвешенный подсчёт баллов
├── items/                # YAML-файлы с заданиями
│   ├── _template.yaml    # Шаблон для новых заданий
│   ├── simple.yaml       # Простые (сложность 1-3)
│   ├── medium.yaml       # Средние (сложность 4-7)
│   └── hard.yaml         # Сложные (сложность 8-10)
├── sql/                  # DDL для таблиц БД
│   ├── create_benchmark_tables.sql      # PostgreSQL
│   └── create_benchmark_tables_gp.sql   # Greenplum 6.25
├── results/runs/         # Результаты прогонов (авто-генерация)
│   ├── .gitkeep
│   └── YYYY-MM-DD_HH-MM-SS/  # Каждый прогон — отдельная папка
│       ├── summary.json
│       ├── summary.md
│       └── detail/<item_id>.json
```

### Поток выполнения

```
runner.py (CLI)
  │
  ├── load_benchmark(path)        → BenchSuite (loader.py)
  ├── _filter_items(...)          → отфильтрованный BenchSuite
  ├── _run_suite()                → для каждого item:
  │     ├── _run_single()         →   single-задание
  │     │     ├── bot.run()       →     агент + BenchmarkHook
  │     │     └── evaluate()      →     оценка (evaluator.py)
  │     └── _run_multi_step()     →   multi_step-задание
  │           ├── bot.run() × N   →     шаги в одной сессии
  │           └── evaluate() × N  →     оценка каждого шага
  │
  ├── save_json_report()          → JSON-отчёт (reporter.py)
  ├── save_markdown_report()      → Markdown-отчёт (reporter.py)
  ├── BenchmarkDB.save_run()      → сохранение в PostgreSQL (db.py)
  └── _print_summary()            → консольный вывод
```

---

## 2. Модели данных (models.py)

### BenchItem — одно задание
| Поле | Тип | Описание |
|------|-----|----------|
| `id` | str | Уникальный ID задания |
| `name` | str | Человекочитаемое название |
| `difficulty` | int | Сложность 1–10 (1-3=simple, 4-7=medium, 8-10=hard) |
| `category` | str | Категория: basic, audit_analyzer, coding, research, data_analysis, git |
| `type` | str | `"single"` или `"multi_step"` |
| `new_session` | bool | true = свежая сессия, false = продолжение |
| `question` | str | Текст вопроса (для single) |
| `steps` | list[BenchStep] | Шаги (для multi_step) |
| `expect` | BenchExpect | Ожидания (для single) |
| `context_files` | list[str] | Файлы, предоставляемые агенту |
| `max_iterations` | int | Максимум итераций агента |
| `timeout` | int | Таймаут в секундах |

### BenchStep — шаг многошагового задания
| Поле | Тип | Описание |
|------|-----|----------|
| `step` | int | Номер шага |
| `question` | str | Вопрос на этом шаге |
| `weight` | float | Вес шага в итоговой оценке (0.0–1.0) |
| `expect` | BenchExpect | Ожидания для шага |

### BenchExpect — критерии оценки
| Поле | Тип | Описание |
|------|-----|----------|
| `tools` | list[str] | Какие инструменты должен использовать агент |
| `skills` | list[str] | Какие навыки должен активировать |
| `keywords_include` | list[str] | Обязательные слова в ответе |
| `keywords_exclude` | list[str] | Запрещённые слова в ответе |
| `max_iterations` | int | Лимит итераций |
| `match_type` | str | `"keyword"` / `"llm_judge"` |
| `check_file` | str | Путь к файлу, который должен существовать |
| `check_file_content` | str | Содержимое, которое должно быть в файле |



---

## 3. Файлы с вопросами (benchmarks/items/)

### 3.1. Структура файлов

```
benchmarks/items/
├── _template.yaml        # Шаблон-заготовка (игнорируется загрузчиком)
├── simple.yaml           # Простые вопросы, сложность 1–3
├── medium.yaml           # Средние вопросы, сложность 4–7
└── hard.yaml             # Сложные вопросы, сложность 8–10
```

Файлы сортируются по алфавиту и загружаются последовательно. Все записи из всех файлов собираются в общий список. Разделение по файлам нужно только для удобства навигации — можно хранить всё в одном файле или в любом количестве файлов.

**Файлы, начинающиеся с `_`** (например `_template.yaml`), игнорируются загрузчиком (`loader.py:48`). Используйте их как черновики или шаблоны.

### 3.2. Формат YAML

Каждый YAML-файл может содержать:
- **Список** (`---\n- id: ...\n- id: ...`) — каждый элемент это вопрос
- **Словарь с ключом `benchmarks:` или `items:`** — для группировки с метаданными

```yaml
# Формат 1: простой список
- id: "q1"
  name: "Вопрос 1"
  ...
- id: "q2"
  name: "Вопрос 2"
  ...

# Формат 2: словарь с именем и тегами
name: "my-suite"
tags: ["custom", "experimental"]
items:
  - id: "q1"
    ...
```

### 3.3. Описание полей

#### Обязательные поля (всегда)

| Поле | Тип | Описание |
|------|-----|----------|
| `id` | str | **Уникальный идентификатор** во всех YAML-файлах. Используется в отчётах, БД, для `--compare`. Рекомендуется формат: `сложность-категория-смысл` (например `medium-schema-relations`). |
| `name` | str | Человекочитаемое название (до 30 символов — обрезается в Markdown-отчёте) |
| `difficulty` | int | Целое число **от 1 до 10**. Определяет группу: 1–3 = `simple`, 4–7 = `medium`, 8–10 = `hard` |
| `category` | str | Категория для фильтрации `--category`. Может быть любой строкой: `basic`, `coding`, `audit_analyzer`, `research`, `data_analysis`, `git` |
| `type` | str | `"single"` или `"multi_step"` |

#### Опциональные поля

| Поле | Тип | По умолч. | Описание |
|------|-----|-----------|----------|
| `new_session` | bool | `true` | `true` — перед вопросом создаётся новая сессия агента (чистый контекст). `false` — все шаги multi_step выполняются в одной сессии (агент помнит предыдущие шаги) |
| `question` | str | `null` | Текст вопроса. **Обязателен для `single`**. Для `multi_step` задаётся внутри `steps[].question` |
| `steps` | list | `[]` | Список шагов. **Обязателен для `multi_step`**, иначе `ValueError` |
| `context_files` | list[str] | `[]` | Файлы, которые предоставляются агенту в workspace перед выполнением. Пока не реализовано в раннере |
| `max_iterations` | int | `30` | Максимальное количество итераций агента на задание (или на шаг). Влияет на проверку `iterations` |
| `timeout` | int | `60` | Таймаут выполнения в секундах. Для multi_step рекомендуется 180–300 |

#### Поле `expect:` (критерии оценки)

Применяется к single-заданию целиком или к каждому шагу multi_step отдельно.

| Поле | Тип | По умолч. | Описание |
|------|-----|-----------|----------|
| `tools` | list[str] | `[]` | Инструменты, которые **обязан** вызвать агент. Если хоть один не вызван — проверка не пройдена. Сверка в `evaluator.py:_check_tools()` (сравнение с `hook.tools_used`). Значения: `"read_file"`, `"exec"`, `"write_file"`, `"find_files"`, `"glob"` и т.д. |
| `skills` | list[str] | `[]` | Навыки, которые **обязан** активировать агент. Проверка по `hook.skills` |
| `keywords_include` | list[str] | `[]` | Слова, которые **обязаны** присутствовать в текстовом ответе агента. Поиск регистронезависимый (`evaluator.py:129`) |
| `keywords_exclude` | list[str] | `[]` | Слова, которые **запрещены** в ответе агента. Регистронезависимый поиск |
| `max_iterations` | int | `30` | Переопределение лимита итераций для данной проверки |
| `match_type` | str | `"keyword"` | `"keyword"` — стандартная проверка по ключевым словам. `"llm_judge"` — оценка LLM-судьёй (пока заглушка, возвращает 0.5) |
| `check_file` | str | `null` | Относительный или абсолютный путь к файлу, который **должен существовать** после работы агента. Путь разрешается относительно workspace агента |
| `check_file_content` | str | `null` | Фрагмент текста, который **должен содержаться** в указанном файле. Поиск регистронезависимый |

### 3.4. Поле `steps[]` (для multi_step)

Каждый шаг — это отдельный `question`, который подаётся агенту последовательно в рамках одной сессии.

| Поле | Тип | По умолч. | Описание |
|------|-----|-----------|----------|
| `step` | int | номер по порядку | Номер шага. Если не указан, присваивается автоматически (1, 2, 3...) |
| `question` | str | — | **Обязательное поле.** Текст вопроса на этом шаге |
| `weight` | float | `1.0` | Вес шага в итоговом балле. Все веса нормализуются: итоговый балл шага = `weight / sum(all_weights)`. Может быть дробным (например `0.3`) |
| `expect` | dict | `{}` | Критерии оценки для данного шага. Те же поля, что и в `expect` для single (см. выше) |

### 3.5. Полный пример

```yaml
- id: "medium-example-query"
  name: "Пример запроса"
  difficulty: 5
  category: "data_analysis"
  type: "single"
  new_session: true
  question: "Прочитай файл data.csv и посчитай количество строк"
  context_files: ["data.csv"]
  max_iterations: 15
  timeout: 60
  expect:
    tools: ["read_file"]
    keywords_include: ["строк", "найдено"]
    keywords_exclude: ["ошибка"]
    check_file: null
    match_type: "keyword"

- id: "hard-example-pipeline"
  name: "Конвейер обработки"
  difficulty: 9
  category: "data_analysis"
  type: "multi_step"
  new_session: false
  max_iterations: 40
  timeout: 300
  steps:
    - step: 1
      weight: 0.3
      question: "Найди все CSV файлы в директории data/"
      expect:
        tools: ["find_files", "glob"]
        keywords_include: [".csv"]
    - step: 2
      weight: 0.7
      question: "Прочитай data/sales.csv и вычисли общую сумму продаж"
      expect:
        tools: ["read_file"]
        keywords_include: ["сумма", "продаж"]
        check_file: "report.txt"
```

### 3.6. Что будет, если ошибиться в YAML

Все ошибки загрузки YAML перехватываются раннером и выводятся в понятном виде:

```
======================================================================
  ERROR: Missing required field in benchmark YAML
======================================================================
  Missing field: 'id'

  Every item must have at least: id, name, difficulty, category, type
  For single items: question is required
  For multi_step items: steps is required

  Check your YAML file and add the missing field.
  See benchmarks/items/_template.yaml for reference.
======================================================================
```

Возможные типы ошибок и их сообщения:

| Тип ошибки | Что выводит runner | Типичная причина |
|-----------|--------------------|-----------------|
| `FileNotFoundError` | `ERROR: Benchmark file(s) not found` | Нет файла/директории, расширение `.yml` вместо `.yaml` |
| `ValueError` | `ERROR: Invalid benchmark definition` | multi_step без steps |
| `KeyError` | `ERROR: Missing required field in benchmark YAML` | Нет поля `id` |
| `yaml.ScannerError` / `yaml.ParserError` | `ERROR: Failed to load benchmark` + тип ошибки | Сбиты отступы, неверный синтаксис |
| Любая другая | `ERROR: Failed to load benchmark` + тип и сообщение | Прочие проблемы |

После сообщения об ошибке runner сразу показывает hint с действием:
- Для синтаксических ошибок — команду `python -c "import yaml; yaml.safe_load(...)"` для проверки
- Для отсутствующих полей — ссылку на `_template.yaml`
- Для FileNotFound — правильный путь к `items/`

#### Примеры проблемных YAML и как они выглядят

**Сбиты отступы:**
```yaml
- id: "test"
  name: "Test"
  expect:
  tools: ["read_file"]     # ← отступ не совпадает с expect
    keywords_include: ["тест"]  # ← отступ не совпадает с tools
```
Вывод:
```
ERROR: Failed to load benchmark
yaml.scanner.ParserError: while parsing a block mapping
  in "file.yaml", line 5, column 5
```

**Пропущен `id`:**
```yaml
- name: "Без id"           # ← нет поля id
  difficulty: 1
```
Вывод:
```
ERROR: Missing required field in benchmark YAML
Missing field: 'id'
```

**multi_step без steps:**
```yaml
- id: "test"
  type: "multi_step"       # ← нет steps
```
Вывод:
```
ERROR: Invalid benchmark definition
Item 'test' has type multi_step but no steps defined
```

**Файл с именем `_` игнорируется без ошибки** — вопросы не попадут в прогон, и вы этого не заметите. Проверяйте имена файлов.

**Дубликаты `id`** — загрузчик их не проверяет. Валидация предупредит:
```
Warnings:
  ! DUPLICATE ID 'my-id' — will be overwritten in reports/DB
```

#### Ошибки выполнения (не вызывают падение)

Эти ошибки НЕ останавливают прогон — они видны в результатах:

| Симптом | Где видно | Причина |
|---------|-----------|---------|
| `FAIL`, score = 0 | в строке вопроса, в summary | Агент не справился или все проверки завалены |
| `[tools✗]` | рядом с FAIL в строке вопроса | Агент не вызвал ожидаемый инструмент |
| `[keywords_include✗]` | рядом с FAIL | В ответе нет обязательных слов |
| `[file_exists✗]` | рядом с FAIL | Агент не создал ожидаемый файл |

### 3.7. Диагностика проблем

#### Шаг 1. Сухой прогон (`--dry-run`)

Всегда начинайте с `--dry-run`. Он загружает YAML и показывает:
- Какие вопросы будут запущены
- Тип вопроса (SINGLE / MULTI) и сложность
- Текст вопроса
- Все expect-проверки (tools, keywords, check_file, match_type)

```bash
python benchmarks/runner.py --dry-run
python benchmarks/runner.py --items benchmarks/items/simple.yaml --dry-run
```

Пример вывода `--dry-run`:
```
DRY RUN: items
Total items: 18

  [SINGLE] d=1 simple-greeting (Приветствие)
           Q: Поздоровайся
           expect: kw_in=['привет'], kw_ex=['error']

  [MULTI]  d=8 hard-code-test-fix (Написать, протестировать и исправить)
           Step 1 (w=0.3): Напиши Python скрипт fibonacci.py...
           Step 2 (w=0.3): Запусти fibonacci.py с аргументом 11...
           Step 3 (w=0.4): Добавь валидацию ввода...
```

#### Шаг 2. Отдельная проверка YAML

```bash
python -c "import yaml; yaml.safe_load(open('benchmarks/items/simple.yaml'))"
```

Никакого вывода = файл валиден. При ошибке — `ScannerError` / `ParserError` с номером строки.

#### Шаг 3. Валидация перед запуском

Перед фактическим запуском runner проверяет все items и выводит предупреждения:
```
Warnings:
  ! DUPLICATE ID 'my-id' — will be overwritten in reports/DB
  ! Item 'test' is single but has no question
  ! Item 'test' has difficulty=0, expected 1-10
```

#### Шаг 4. Запуск и live-вывод

Во время прогона вы видите для каждого вопроса:
- Для **single**: `[1/N] item-id -> PASS/FAIL score=X iter=X dur=X`
- Для **multi_step**: каждый шаг отдельно с результатом
- Для FAIL сразу видно, какие проверки завалены: `[tools✗ keywords_include✗]`

#### Шаг 5. После прогона — итоговая сводка

Сводка показывает:
- Общую статистику (passed/total, avg score, duration)
- По каждому вопросу: PASS/FAIL, сложность, балл, итерации, длительность
- Для FAIL — какие проверки завалены: `[tools✗]`, `[keywords_include✗]` и т.д.

#### Шаг 6. Детальные отчёты

- `summary.md` — Markdown с таблицами и разбором каждого вопроса
- `detail/<id>.json` — JSON с каждой проверкой, баллами и описанием

### 3.8. Типичные ошибки и как их избежать

| Ошибка | Причина | Решение |
|--------|---------|---------|
| `KeyError: 'id'` | В YAML-блоке нет поля `id` | Поле `id` обязательно. Добавьте `id: "my-unique-id"` |
| `id не уникален` | Два вопроса с одинаковым `id` в разных YAML-файлах | Используйте уникальные `id`. Рекомендуемый формат: `сложность-назначение-суть` (например `medium-schema-relations`) |
| `ValueError: Item 'X' has type multi_step but no steps defined` | У `type: "multi_step"` не указан `steps: [...]` | Добавьте блок `steps:` с хотя бы одним шагом, либо смените `type` на "single" |
| `FileNotFoundError: No YAML files found` | В директории нет `.yaml` файлов (кроме `_`-файлов) | Убедитесь, что файлы имеют расширение `.yaml`, а не `.yml`, и не начинаются с `_` |
| `yaml.scanner.ScannerError` / `ParserError` | Ошибка синтаксиса YAML | Проверьте отступы. Используйте `python -c "import yaml; yaml.safe_load(open(...))"` |
| `question` отсутствует у single | У single-задания нет поля `question` | Для `type: "single"` поле `question` обязательно |
| `check_file` указан, но файл не создаётся | Агент не создал ожидаемый файл | Убедитесь, что инструмент `write_file` (или `exec`) создаёт файл по указанному пути. Проверьте права на запись |
| `keywords_include` не срабатывает | Агент ответил, но без ожидаемых слов | Проверьте регистр — поиск регистронезависимый. Убедитесь, что слова действительно есть в ответе |
| `keywords_exclude` не срабатывает | В ответе есть запрещённые слова | Убедитесь, что агент не использует эти слова |
| `tools` не срабатывает | Агент не вызвал ожидаемый инструмент | Проверьте точное название инструмента. Сверка точная: `"exec"` ≠ `"exeс"` |
| `multi_step: шаг упал, но не пройдено` | Для прохождения нужны **все** шаги | Если шаг опционален — вынесите в отдельное single-задание |
| `Бенчмарк выполняется слишком долго` | `max_iterations` или `timeout` завышены | Для простых вопросов 5–10 итераций, для сложных multi_step 30–45 |
| `match_type: llm_judge` всегда даёт 0.5 | Заглушка, LLM-судья не реализован | Пока используйте `"keyword"` |
| `Оценка слишком низкая/высокая` | Веса проверок не сбалансированы | Отредактируйте `scorer.py:CHECK_WEIGHTS` или уберите лишние проверки из `expect` |
| `context_files` не работают | Поле зарезервировано, но не реализовано | Файлы пока не копируются в workspace автоматически |
| `new_session: false` не сохраняет контекст | Контекст сохраняется только между шагами одного multi_step | Для single всегда `true`. Для multi_step контекст живёт внутри прогона `_run_multi_step` |
| `Все вопросы FAIL` | Агент не может ответить или проблемы с подключением | Проверьте, работает ли агент: `python -c "from nanobot import Nanobot; print('OK')"` |
| `Файл .yaml игнорируется без ошибки` | Имя начинается с `_` | Уберите `_` из начала имени файла |
| `Ошибки в консоли забивают экран` | Не включён `--verbose` | По умолчанию вывод краткий. Добавьте `--verbose` для деталей |

### 3.9. Рекомендации по составлению вопросов

1. **Формулируйте question чётко и однозначно** — агент должен понять, что именно от него требуется
2. **Указывайте в question ожидаемые действия** — например "Прочитай файл X и скажи Y" (тогда агент вызовет `read_file`)
3. **Не добавляйте лишних проверок в expect** — каждая проверка снижает итоговый балл, если не пройдена. Добавляйте только то, что действительно критично
4. **Балансируйте difficulty и max_iterations** — для сложности 1 ставьте `max_iterations: 5`, для 8-10 ставьте `30-45`
5. **Для multi_step используйте `new_session: false`** — иначе каждый шаг начинается с чистого листа
6. **Проверяйте `keywords_include` на покрытие разных форм слов** — агент может ответить "найдено 5 строк" или "количество строк — 5". Лучше указывать основу слова: `["строк"]` покроет оба варианта
7. **Используйте шаблон `_template.yaml`** — скопируйте его и заполните свои значения, чтобы не забыть ни одного поля

---

## 4. Оценка и баллы (scorer.py + evaluator.py)

### 4.1. Типы проверок и их веса

| Проверка | Вес | Что проверяет |
|----------|-----|---------------|
| `tools` | 0.20 | Использовал ли агент ожидаемые инструменты |
| `llm_judge` | 0.20 | Оценка LLM-судьёй (пока заглушка) |
| `keywords_include` | 0.15 | Наличие обязательных слов в ответе |
| `iterations` | 0.15 | Уложился ли в лимит итераций |
| `file_exists` | 0.15 | Существует ли ожидаемый файл |
| `file_content` | 0.15 | Содержит ли файл ожидаемый текст |
| `keywords_exclude` | 0.10 | Отсутствие запрещённых слов |
| `skills` | 0.10 | Активировал ли ожидаемые навыки |

Веса заданы в `scorer.py:CHECK_WEIGHTS`. Итоговый балл — взвешенное среднее.

### 4.2. Критерии прохождения

Задание считается пройденным (`passed = true`), если:
1. `total_score >= 0.5` (средний балл)
2. Все **критические** проверки пройдены: `tools`, `keywords_include`, `file_exists`, `file_content`, `llm_judge`

Критические проверки перечислены в `evaluator.py:_critical_checks()`.

### 4.3. Для multi_step

Итоговый балл = `80% × взвешенная_сумма_шагов + 20% × доля_пройденных_шагов`

Задание пройдено, если **все шаги пройдены** И итоговый балл >= 0.5.

### 4.4. Как подправить оценку

- **Изменить веса проверок** → отредактировать `scorer.py:CHECK_WEIGHTS`
- **Изменить порог прохождения** → `evaluator.py:54` (строка `total_score >= 0.5`)
- **Изменить состав критических проверок** → `evaluator.py:_critical_checks()`
- **Изменить расчёт multi_step** → `scorer.py:score_multi_step()` (строка `final_score * 0.8 + completeness * 0.2`)
- **Поправить критерии в конкретном вопросе** → отредактировать `expect:` в YAML
- **Если проверка не нужна** — просто не указывайте её в `expect` (она не будет добавлена)

---

## 5. Как запускать

### 5.1. Базовый запуск

```bash
python benchmarks/runner.py
```

Запустит все задания из `benchmarks/items/` (simple + medium + hard).

### 5.2. Фильтрация

```bash
# По сложности (теги)
python benchmarks/runner.py --tags simple
python benchmarks/runner.py --tags medium hard
python benchmarks/runner.py --tags simple medium

# По диапазону сложности
python benchmarks/runner.py --difficulty 1-3
python benchmarks/runner.py --difficulty 8-10

# По категории
python benchmarks/runner.py --category coding
python benchmarks/runner.py --category basic audit_analyzer

# По типу
python benchmarks/runner.py --mode single
python benchmarks/runner.py --mode multi_step

# Комбинированно
python benchmarks/runner.py --tags hard --mode multi_step --category coding
```

### 5.3. Конкретный файл

```bash
python benchmarks/runner.py --items benchmarks/items/simple.yaml
python benchmarks/runner.py --items benchmarks/items/hard.yaml
```

### 5.4. Сухой прогон (показывает вопросы без выполнения)

```bash
python benchmarks/runner.py --dry-run
python benchmarks/runner.py --tags hard --dry-run
```

### 5.5. Подробный вывод

```bash
python benchmarks/runner.py --verbose
```

### 5.6. Переопределение модели

```bash
python benchmarks/runner.py --model phi4:latest
python benchmarks/runner.py --model qwen3:4b
```

### 5.7. Сохранение в PostgreSQL

```bash
python benchmarks/runner.py --db postgresql://user:pass@localhost/dbname
```

### 5.8. Сравнение двух прогонов

```bash
python benchmarks/runner.py --compare results/runs/2026-06-09_17-19-00 results/runs/2026-06-10_08-12-38
```

### 5.9. Указание директории для отчётов

```bash
python benchmarks/runner.py --output my_reports/run1
```

### 5.10. Указание конфига

```bash
python benchmarks/runner.py --config my_config.json
```

### 5.11. Обратная связь при запуске

При запуске `runner.py` вы получаете многоуровневую обратную связь:

#### Уровень 1: Ошибки загрузки YAML

Если YAML-файлы содержат ошибки, runner **не падает с raw traceback**, а выводит понятное сообщение:

```
======================================================================
  ERROR: Missing required field in benchmark YAML
======================================================================
  Missing field: 'id'
  ...
  See benchmarks/items/_template.yaml for reference.
```

Виды ошибок: файл не найден, невалидный YAML (синтаксис), пропущены обязательные поля, multi_step без steps.

#### Уровень 2: Валидация перед запуском

Перед запуском агента runner проверяет все вопросы и предупреждает:
```
Warnings:
  ! DUPLICATE ID 'my-id' — will be overwritten in reports/DB
  ! Item 'test' is single but has no question
```

#### Уровень 3: Live-вывод single-заданий

Для каждого single-задания:
```
[1/6] simple-greeting (difficulty=1)
  -> PASS score=100.00% iter=3 dur=5.2s

[2/6] simple-schema-tables (difficulty=2)
  -> FAIL score=33.33% iter=10 dur=30.1s  [tools✗ keywords_include✗]

[3/6] medium-violations-query (difficulty=6)
  -> FAIL score=33.33% iter=20 dur=45.0s  [tools✗]
       ERROR: Connection refused
```

Значки:
- `PASS` — задание пройдено
- `FAIL` — задание не пройдено
- `[tools✗]` — не использован ожидаемый инструмент
- `[keywords_include✗]` — в ответе нет обязательных слов
- `[keywords_exclude✗]` — в ответе есть запрещённые слова
- `[file_exists✗]` — не создан ожидаемый файл
- `[file_content✗]` — в файле нет ожидаемого содержимого
- `[skills✗]` — не активирован ожидаемый навык
- `[llm_judge✗]` — LLM-судья не одобрил (пока заглушка)

#### Уровень 4: Live-вывод multi_step-заданий

Каждый шаг выводится отдельно с результатом:
```
[4/6] hard-code-test-fix (difficulty=8)
    Step 1/3: Напиши Python скрипт fibonacci.py...
      -> PASS score=100.00% iter=3 dur=5.2s
    Step 2/3: Запусти fibonacci.py с аргументом 11...
      -> FAIL score=33.33% iter=2 dur=3.1s  [keywords_include✗]
    Step 3/3: Добавь валидацию ввода...
      -> PASS score=75.00% iter=4 dur=6.0s
  -> FAIL score=50.29% iter=9 dur=14.3s  [keywords_include✗]
```

#### Уровень 5: Итоговая сводка

После завершения всех вопросов:
```
======================================================================
  BENCHMARK COMPLETE: items
======================================================================
  Items:    6
  Passed:   3 / 6 (50.0%)
  Avg Score: 61.7%
  Duration:  180.5s
----------------------------------------------------------------------
  [PASS] [S] simple-greeting                   score=100.00%  iter=3   dur=5.2s
  [PASS] [S] simple-date                       score=100.00%  iter=2   dur=3.1s
  [FAIL] [S] simple-schema-tables              score=33.33%   iter=10  dur=30.1s  [tools✗ keywords_include✗]
  [FAIL] [M] medium-predefined-run             score=40.00%   iter=15  dur=45.0s  [tools✗]
  [PASS] [M] medium-schema-relations           score=100.00%  iter=5   dur=12.5s
  [FAIL] [H] hard-code-test-fix                score=50.29%   iter=9   dur=14.3s  [keywords_include✗]
======================================================================
  3 item(s) FAILED.
  See detail/<id>.json for per-check breakdown.
  Hint: run with --verbose to see full agent responses.
```

#### Уровень 6: Детальные отчёты

Автоматически генерируются `summary.json` и `summary.md` с полным разбором:
- Таблица всех результатов
- Для каждого вопроса — все проверки с баллами и описанием
- Для multi_step — результаты по каждому шагу

#### Что даёт `--verbose`

Режим `--verbose` включает DEBUG-логирование:
- Полный ответ агента на каждый вопрос
- Все вызовы инструментов с параметрами
- Iteration-дампы выполнения

---

## 6. Результаты

После каждого прогона создаётся папка `benchmarks/results/runs/YYYY-MM-DD_HH-MM-SS/` с:

### summary.json
Сводка по всему прогону: имя набора, число заданий, пройдено, баллы, длительность, массив результатов.

### detail/<item_id>.json
Детальный результат по каждому заданию: проверки, инструменты, итерации, ошибки.

### summary.md
Markdown-отчёт с:
- таблицей сводки
- группировкой по сложности
- таблицей всех результатов
- детальным разбором каждого задания (проверки, шаги, баллы)

---

## 7. PostgreSQL / Greenplum (db.py)

### Таблицы

- **benchmark_runs** — мета-информация о прогоне (имя, кол-во заданий, баллы, время)
- **benchmark_results** — результаты по каждому вопросу

### Настройка

В `project.json` в секции `benchmark`:
```json
{
  "benchmark": {
    "db_schema": "public",
    "runs_table": "benchmark_runs",
    "results_table": "benchmark_results"
  }
}
```

### Методы BenchmarkDB

| Метод | Описание |
|-------|----------|
| `ensure_tables()` | Создаёт таблицы (автоопределение PG или GP) |
| `save_run(suite_result)` | Сохраняет прогон |
| `get_history(suite_name, limit=10)` | История прогонов |
| `compare_runs(run_id_1, run_id_2)` | Сравнение двух прогонов |

### SQL-файлы

- `sql/benchmarks/create_benchmark_tables.sql` — для PostgreSQL 9.4+ (uuid-ossp)
- `sql/benchmarks/create_benchmark_tables_gp.sql` — для Greenplum 6.25 (pgcrypto, DISTRIBUTED BY)

Выбор SQL-файла происходит автоматически через `_is_greenplum()`.

---

## 8. Хук сбора метрик (hooks.py)

`BenchmarkHook` перехватывает выполнение агента и собирает:

- `tool_calls` — все вызовы инструментов с параметрами
- `iterations` — число итераций агента
- `skills` — какие навыки были активированы
- `usage` — статистика использования (токены и т.п.)
- `start_time` / `end_time` — время выполнения
- `tools_used` (property) — список уникальных инструментов

---

## 9. Где и что править в типовых сценариях

| Сценарий | Что править |
|----------|-------------|
| **Добавить вопрос** | `benchmarks/items/<file>.yaml` — добавить блок |
| **Убрать вопрос** | Удалить блок из YAML или закомментировать |
| **Изменить сложность** | Поле `difficulty` в YAML |
| **Изменить вес проверок** | `benchmarks/scorer.py:CHECK_WEIGHTS` |
| **Изменить порог прохождения** | `benchmarks/evaluator.py:54` (`total_score >= 0.5`) |
| **Добавить новую проверку** | 1) Добавить метод в `evaluator.py`; 2) Добавить поле в `BenchExpect` в `models.py`; 3) Добавить вес в `scorer.py:CHECK_WEIGHTS`; 4) Вызвать проверку в `evaluate()` |
| **Изменить формат отчёта** | `benchmarks/reporter.py` — функции `save_json_report`, `save_markdown_report` |
| **Добавить фильтр** | `benchmarks/runner.py:_filter_items()` |
| **Изменить расчёт multi_step** | `benchmarks/scorer.py:score_multi_step()` |
| **Сменить БД** | `benchmarks/db.py` + `sql/benchmarks/` |
| **Добавить поддержку новой БД** | 1) SQL-файл в `sql/benchmarks/`; 2) `_is_<db>()` в `db.py`; 3) выбор файла в `ensure_tables()` |
| **Реализовать LLM-судью** | `benchmarks/evaluator.py:_check_llm_judge()` — заменить заглушку |
| **Добавить CLI-аргумент** | `benchmarks/runner.py:_parse_args()` + обработка в `main_async()` |
| **Очистить результаты** | Удалить папки в `benchmarks/results/runs/` (кроме `.gitkeep`) |

---

## 10. Зависимости

- `nanobot` — ядро агента
- `PyYAML` — загрузка YAML
- `loguru` — логирование
- `psycopg2` / `psycopg2-binary` — PostgreSQL (опционально, для сохранения в БД)
- `json` (stdlib) — отчёты
- `argparse` (stdlib) — CLI
- `asyncio` (stdlib) — асинхронный запуск
- `dataclasses` (stdlib) — модели данных
