# 🛠 Разработка и внутренняя архитектура (Development)

Этот документ — **навигационный хаб** для контрибьюторов и разработчиков ядра `nanobot`.
Глубокая техническая документация перенесена в каталог [`docs/`](docs/) для удобства чтения,
рендеринга и поддержки (исходный `DEVELOPMENT.md` превышал 220 КБ).

> Для установки, запуска и базовых настроек — вернитесь в [README.md](README.md).
> Подробная навигация по `docs/` — в [docs/README.md](docs/README.md).

## 📚 Техническая документация

| Раздел | Файл | Описание |
|--------|------|----------|
| 🏗 Архитектура и сервисный слой | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | `ApplicationContext`, поток инициализации, `lib/core`, `lib/services`, `MessageExchange`, LLM-клиент, утилиты, дерево `Структура проекта` |
| 🗃 База данных и SQL | [docs/DATABASE.md](docs/DATABASE.md) | Единый пул соединений (`workspace/utils/db.py`), универсальный слой `lib/services`, конфигурация навыка, DDL-скрипты, границы P0 |
| 🔍 Векторная индексация | [docs/VECTOR_INDEXES.md](docs/VECTOR_INDEXES.md) | FAISS, Ollama, `tools/build_vectors.py`, lifecycle кеша, edge-cases, тюнинг |
| ⚙️ Внутренний API и конфигурация | [docs/INTERNAL_API.md](docs/INTERNAL_API.md) | `tools.exec`, кастомные `workspace/tools/*.py`, CLI-режимы, `tools/`, добавление настроек |
| 🧪 Тестирование | [docs/TESTING.md](docs/TESTING.md) | Запуск тестов, контрактные тесты nanobot API, live e2e |
| 📝 Миграции | [docs/MIGRATION.md](docs/MIGRATION.md) | Ручные действия между релизами, breaking changes |

Смежные документы в `docs/`: [table-registry.md](docs/table-registry.md),
[skill-tool-architecture.md](docs/skill-tool-architecture.md),
[skill-tool-inventory.md](docs/skill-tool-inventory.md),
[TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md),
[runtime_patches.md](docs/runtime_patches.md),
[refactor_baseline.md](docs/refactor_baseline.md).

## 📐 Нормативная архитектура (TARGET)

[TARGET_ARCHITECTURE.md](TARGET_ARCHITECTURE.md) — **архитектурный контракт**: принципы,
invariant'ы, anti-patterns, decision-чеклист и правила зависимостей. Это «как должно быть»,
а **не** описание текущей реализации. Каждое существенное изменение сверяется с ним
(см. разделы §30–§31).

> **Разделение ответственности, чтобы не дублировать:**
> - `TARGET_ARCHITECTURE.md` — *норма* (правила, цель, contract). Не содержит описания «as-is».
> - `docs/ARCHITECTURE.md`, `docs/DATABASE.md`, `docs/INTERNAL_API.md` — *текущая реализация*
>   (что и как работает сейчас). Ссылаются на `TARGET_ARCHITECTURE.md §N` за правилами.
> - Где документы пересекаются по теме — детали реализации только в `docs/*`, правила только в `TARGET_ARCHITECTURE.md`.

## 🚀 Быстрый старт для разработчика

1. Установите зависимости: `pip install -r requirements.txt`
2. Запустите тесты без БД: `pytest tests/ -q`
3. Запустите gateway / CLI и проверьте прогон: `python gateway.py` или `python cli_agent.py -P`
4. Перед коммитом убедитесь, что проверки документации (CI `docs-lint`) проходят.

## 📐 Конвенции правки документации

- Любое изменение поведения, API или конфигурации сопровождается правкой
  соответствующего файла в `docs/` **в том же изменении**.
- Перекрёстные ссылки между документами — относительные (`docs/ARCHITECTURE.md`),
  внутри одного файла — якоря (`#структура-проекта`).
- Этот хаб держите компактным (≤ ~120 строк); детали — в `docs/`.
