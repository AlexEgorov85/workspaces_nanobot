# Vector Indexes (Векторные индексы)

## Назначение

Определение логической модели для векторных индексов: источник конфигурации, lifecycle индекса, доступ Skills/сервисов и поведение при ошибке. Векторные индексы управляются централизованно и предоставляются через `CacheProvider.search_vector`.

## Ответственность

Vector Indexes отвечают за:
- определение источника конфигурации векторных индексов
- управление lifecycle векторных индексов (создание, сборка, загрузка)
- предоставление единого API для vector search через CacheProvider
- обеспечение FAISS-backed хранения

## Граница

### Владеет
- конфигурацией векторных индексов в project.json
- сборкой и хранением FAISS индексов
- предоставлением vector search API через CacheProvider.search_vector

### Не владеет
- прямым доступом к FAISS файлам извне CacheProvider
- бизнес-логикой Skills
- альтернативными vector storage backends

### Может зависеть от
- project.json (конфигурация индексов)
- FAISS library
- PostgreSQL (хранение embeddings)
- CacheProvider (доступ к индексу)

### Не должен зависеть от
- конкретной реализации Skills
- legacy vector_index_config таблиц
- других vector storage implementations

## Публичный контракт

VectorIndexService предоставляет:
- загрузку конфигурации индексов из project.json
- сборку FAISS индексов через build_vectors.py
- поиск по векторному сходству через CacheProvider.search_vector

## Требования

### Требование: Единый источник конфигурации

Система ДОЛЖНА читать конфигурацию векторного индекса только из `gateway.vector.index.indexes.*` в `project.json`.

#### Сценарий: Конфигурация индекса

- **КОГДА** векторный индекс добавлен или изменён
- **ТОГДА** его декларация ДОЛЖНА находиться под `gateway.vector.index.indexes.<name>` в `project.json`

### Требование: Storage table зарегистрирован через infra API

Система ДОЛЖНА сохранять векторные embeddings в таблице, зарегистрированной через `lib.core.infra_registration.register_vector_storage`.

#### Сценарий: Таблица векторного хранилища

- **КОГДА** `gateway.vector.index.storage_table` установлен
- **ТОГДА** эта таблица ДОЛЖНА быть зарегистрирована через `register_vector_storage`, чтобы `TableRegistry` знал о ней для синхронизации

### Требование: FAISS-backed

Система ДОЛЖНА строить векторные индексы используя FAISS, вызываемый через `tools/build_vectors.py` и `lib/services/vector_index_service.py`.

#### Сценарий: Сборка индекса

- **КОГДА** векторный индекс строится
- **ТОГДА** FAISS индекс ДОЛЖЕН быть сохранён под `<gateway.vector.index.default_root>/<index_name>` и ДОЛЖЕН загружаться по требованию при query time

### Требование: Единый путь доступа

Система ДОЛЖНА предоставлять vector search исключительно через `CacheProvider.search_vector`.

#### Сценарий: Skill выполняет vector search

- **КОГДА** Skill нуждается в vector similarity query
- **ТОГДА** он ДОЛЖЕН вызвать `CacheProvider.search_vector` и НЕ ДОЛЖЕН загружать FAISS индексы напрямую

## Запрещённое поведение

Система НЕ ДОЛЖНА:

- читать конфигурацию векторного индекса из legacy таблицы `public.agent_vector_index_config` (сохранена только для исторической справки; не authoritative)
- создавать второй vector storage backend рядом с FAISS без явного OpenSpec change
- молча fallback на non-FAISS backend при ошибках FAISS
- bypass `CacheProvider.search_vector` из Skill кода
- читать legacy ключ конфигурации `gateway.vector_index.*` (удалён; runtime-mute если присутствует)

## Зависимости

- `docs/TARGET_ARCHITECTURE.md` — глобальные архитектурные принципы
- `lib/services/vector_index_service.py:VectorIndexService` — реализация
- `tools/build_vectors.py` — сборка индексов
- FAISS library — vector index engine
- `lib/services/cache_provider.py:CacheProvider` — доступ к поиску

## Конфигурация

```json
{
  "gateway": {
    "vector": {
      "index": {
        "default_root": "/path/to/faiss/indexes",
        "storage_table": "vector_embeddings",
        "indexes": {
          "audit_patterns": {
            "dimension": 768,
            "metric": "cosine"
          },
          "legal_docs": {
            "dimension": 768,
            "metric": "cosine"
          }
        }
      }
    }
  }
}
```

## Жизненный цикл

1. **Конфигурация**: индексы определяются в project.json
2. **Сборка**: build_vectors.py строит FAISS индексы из данных
3. **Публикация**: индексы сохраняются в default_root
4. **Загрузка**: индексы загружаются по demand при query
5. **Обновление**: periodic rebuild по расписанию или событию

## Состояние

VectorIndexService хранит:
- конфигурацию индексов
- пути к FAISS файлам
- статус последней сборки

## Инварианты

- Конфигурация читается только из project.json
- Все индексы FAISS-backed
- Единый access path через CacheProvider.search_vector
- Нет legacy table reads

## Поведение при ошибке

- FAISS build error → явная ошибка, нет silent fallback
- Index not found → ошибка возвращается потребителю
- Config missing → fail fast при старте

## Потребители

- AuditAnalyzer — vector search для паттернов
- LegalSummarizer — поиск юридических документов
- Skills — general vector queries

## Реализация

Основная реализация:
- `lib/services/vector_index_service.py:VectorIndexService`

Связанные компоненты:
- `tools/build_vectors.py` — сборка индексов
- `lib/services/cache_provider.py:CacheProvider` — search_vector API
- `lib/core/infra_registration.py:register_vector_storage` — регистрация таблицы

## Проверка

Валидация включает:
1. Проверка отсутствия прямого доступа к FAISS из Skills (code review)
2. Проверка конфигурации только из project.json (тесты)
3. Проверка отсутствия legacy table reads (grep, тесты)
